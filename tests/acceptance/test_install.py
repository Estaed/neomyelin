"""Acceptance tests for install.py (Task-00), written by the orchestrator, not the builder.

The installer writes into the user's harness settings, a path that can lose data, so these tests
are kept apart from the code's author (Blueprint -> Constraints). They only use the fixed
interface in tasks/Task-00.md and never touch the real home directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], cwd=cwd, env=ENV, capture_output=True, text=True,
        encoding="utf-8", timeout=600,
    )


def fake_cli(bin_dir: Path, name: str, script: Path) -> None:
    """Put a fake model CLI `name` on a PATH directory that runs a Python stand-in.

    Windows finds `name.cmd`; elsewhere an executable `name` script. Without it on Linux the test
    reached the real CLI on PATH and spent a real model call (measured in WSL, 2026-10-01).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{script}"\r\n', encoding="utf-8")
    else:
        path = bin_dir / name
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8")
        path.chmod(0o755)


def snapshot(*dirs: Path) -> dict[str, str]:
    out = {}
    for base in dirs:
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and ".state" not in p.parts:
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def hook_commands(settings: dict) -> list[str]:
    cmds = []
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmds.append(hook.get("command", ""))
    return cmds


class InstallAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.vault = base / "vault"
        cls.home = base / "home"
        cls.home.mkdir()
        made = run("scripts/make_test_vault.py", str(cls.vault))
        if made.returncode != 0:
            raise AssertionError(f"make_test_vault failed:\n{made.stdout}\n{made.stderr}")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def install(self, home: Path) -> subprocess.CompletedProcess:
        return run("install.py", "--vault", str(self.vault), "--home", str(home))

    def setUp(self):
        # Install registers a harness only when its command is on PATH; these tests read
        # Claude's settings (test_uninstall and test_hub cover install with stand-in CLIs).
        if shutil.which("claude") is None:
            self.skipTest("claude is not on PATH, so install registers no Claude hooks here")

    def test_1_fresh_install_registers_claude_hooks(self):
        res = self.install(self.home)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue((self.vault / ".brain" / "scripts" / "memory_context.py").is_file())
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        cmds = hook_commands(settings)
        self.assertTrue(any("memory_context" in c or ".brain" in c for c in cmds), cmds)

    def test_2_second_run_changes_nothing(self):
        self.assertEqual(self.install(self.home).returncode, 0)
        before = snapshot(self.vault, self.home)
        res = self.install(self.home)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(before, snapshot(self.vault, self.home))

    def test_3_existing_hooks_kept_and_backed_up(self):
        home = Path(self.tmp.name) / "home-existing"
        settings_path = home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        original = {
            "model": "keep-me",
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo user-own-hook"}]}]},
        }
        raw = json.dumps(original, indent=2)
        settings_path.write_text(raw, encoding="utf-8")
        res = self.install(home)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        after = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(after.get("model"), "keep-me")
        self.assertIn("echo user-own-hook", hook_commands(after))
        backups = [p for p in settings_path.parent.iterdir() if p != settings_path and p.is_file()]
        self.assertTrue(any(p.read_text(encoding="utf-8") == raw for p in backups), backups)

    def test_3b_other_scripts_in_vault_brain_kept(self):
        # Task-00 review: an earlier build dropped any hook that ran a script under <vault>/.brain,
        # which on a vault that already runs another brain would have removed that brain's hooks.
        home = Path(self.tmp.name) / "home-brain"
        settings_path = home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        scripts = (self.vault / ".brain" / "scripts").as_posix()
        own = [f'python "{scripts}/prompt_recall.py"', f'python "{scripts}/memory_context_old.py"']
        original = {"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": own[0]}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": own[1]}]}],
        }}
        settings_path.write_text(json.dumps(original, indent=2), encoding="utf-8")
        res = self.install(home)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        cmds = hook_commands(json.loads(settings_path.read_text(encoding="utf-8")))
        for c in own:
            self.assertIn(c, cmds)

    def test_4_recall_finds_a_vault_note(self):
        self.assertEqual(self.install(self.home).returncode, 0)
        note = self.vault / "knowledge" / "000-acceptance-note.md"
        note.parent.mkdir(exist_ok=True)
        note.write_text("# Kelp forest\n\nThe sea otter wraps itself in kelp to sleep.\n", encoding="utf-8")
        try:
            # BM25 is the path every user has; Ollama on this machine must not hide a broken fallback.
            res = subprocess.run(
                [sys.executable, str(self.vault / ".brain" / "scripts" / "recall.py"),
                 "otter sleeping in kelp"],
                cwd=self.vault, env={**ENV, "NEOMYELIN_RECALL": "bm25"}, capture_output=True,
                text=True, encoding="utf-8", timeout=600,
            )
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("000-acceptance-note", res.stdout)
        finally:
            note.unlink()

    def test_5_real_home_untouched(self):
        real = Path.home() / ".claude" / "settings.json"
        if not real.exists():
            self.skipTest("no real Claude settings on this machine")
        before = real.read_bytes()
        self.assertEqual(self.install(Path(self.tmp.name) / "home-other").returncode, 0)
        self.assertEqual(before, real.read_bytes())


if __name__ == "__main__":
    unittest.main()
