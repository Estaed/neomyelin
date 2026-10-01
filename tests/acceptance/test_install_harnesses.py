"""Acceptance tests for Codex and agy registration and the installer's refusals (Task-01).

Written by the orchestrator, not the builder (Blueprint -> Constraints). They use only the
install.py interface and never touch the real home directory.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from test_install import ENV, hook_commands, run, snapshot  # noqa: F401  (ENV keeps UTF-8)

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English",
         "harnesses": ["claude", "codex", "agy"]}


def agy_commands(data: dict, key: str) -> list[str]:
    return [h.get("command", "") for groups in data.get(key, {}).values() for h in groups]


class HarnessAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.vault = cls.base / "vault"
        made = run("scripts/make_test_vault.py", str(cls.vault))
        if made.returncode != 0:
            raise AssertionError(f"make_test_vault failed:\n{made.stdout}\n{made.stderr}")
        cls.config = cls.base / "config.json"
        cls.config.write_text(json.dumps(NAMES), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def install(self, home: Path, vault: Path | None = None, config: Path | None = None):
        return run("install.py", "--vault", str(vault or self.vault),
                   "--config", str(config or self.config), "--home", str(home))

    def need(self, cli: str):
        if not shutil.which(cli):
            self.skipTest(f"{cli} is not on PATH")

    def test_1_codex_and_agy_registered_and_foreign_entries_kept(self):
        self.need("codex")
        self.need("agy")
        home = self.base / "home-harness"
        codex = home / ".codex" / "hooks.json"
        agy = home / ".gemini" / "config" / "hooks.json"
        codex.parent.mkdir(parents=True)
        agy.parent.mkdir(parents=True)
        codex_raw = json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "echo codex-own-hook"}]}]}}, indent=2)
        agy_raw = json.dumps({"other-brain": {"PreInvocation": [
            {"type": "command", "command": "echo agy-own-hook"}]}}, indent=2)
        codex.write_text(codex_raw, encoding="utf-8")
        agy.write_text(agy_raw, encoding="utf-8")

        res = self.install(home)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        codex_cmds = hook_commands(json.loads(codex.read_text(encoding="utf-8")))
        self.assertIn("echo codex-own-hook", codex_cmds)
        self.assertTrue(any(".brain" in c for c in codex_cmds if c != "echo codex-own-hook"), codex_cmds)
        agy_data = json.loads(agy.read_text(encoding="utf-8"))
        self.assertEqual(agy_commands(agy_data, "other-brain"), ["echo agy-own-hook"])
        self.assertTrue(any(".brain" in c for c in agy_commands(agy_data, "neomyelin")), agy_data)

        for path, raw in ((codex, codex_raw), (agy, agy_raw)):
            backups = [p for p in path.parent.iterdir() if p != path and p.is_file()]
            self.assertTrue(any(p.read_text(encoding="utf-8") == raw for p in backups), backups)

        before = snapshot(self.vault, home)
        again = self.install(home)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(before, snapshot(self.vault, home))

    def test_1b_claude_turn_hooks_registered(self):
        # Task-02: recall, receipt reminder and the two gates hang on these events.
        self.need("claude")
        home = self.base / "home-events"
        res = self.install(home)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        hooks = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"):
            cmds = [h.get("command", "") for g in hooks.get(event, []) for h in g.get("hooks", [])]
            self.assertTrue(any(".brain" in c for c in cmds), f"{event}: {cmds}")
        pre = " ".join(h.get("command", "") for g in hooks["PreToolUse"] for h in g.get("hooks", []))
        self.assertIn("git_gate", pre)
        self.assertIn("heredoc_gate", pre)

    def test_2_foreign_brain_folder_refused_untouched(self):
        vault = self.base / "vault-foreign"
        made = run("scripts/make_test_vault.py", str(vault))
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        foreign = vault / ".brain" / "scripts" / "memory_context.py"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("# another brain's own script\n", encoding="utf-8")
        digest = hashlib.sha256(foreign.read_bytes()).hexdigest()
        home = self.base / "home-foreign"
        home.mkdir()

        res = self.install(home, vault=vault)
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(digest, hashlib.sha256(foreign.read_bytes()).hexdigest())
        self.assertEqual(sorted(p.name for p in (vault / ".brain").iterdir()), ["scripts"])
        self.assertEqual(list(home.iterdir()), [])

    def test_3_example_names_refused(self):
        example = json.loads((Path(__file__).resolve().parents[2] / "templates" / "config.example.json")
                             .read_text(encoding="utf-8"))
        cfg = self.base / "example-names.json"
        cfg.write_text(json.dumps({**NAMES, "user_name": example["user_name"],
                                   "assistant_name": example["assistant_name"]}), encoding="utf-8")
        home = self.base / "home-example"
        home.mkdir()
        res = self.install(home, config=cfg)
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(list(home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
