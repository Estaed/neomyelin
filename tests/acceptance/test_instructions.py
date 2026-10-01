"""Acceptance tests for the user-level instruction block (Task-10).

Written by the orchestrator, not the builder (Blueprint -> Constraints): install writes into the
user's own CLAUDE.md, Codex AGENTS.md and agy instruction file, so every line outside the block
must survive. Never touches the real home directory.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from test_install import run, snapshot

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English",
         "harnesses": ["claude", "codex", "agy"]}
FILES = {"claude": ".claude/CLAUDE.md", "codex": ".codex/AGENTS.md", "agy": ".gemini/GEMINI.md"}
START, END = "<!-- neomyelin-instructions:start -->", "<!-- neomyelin-instructions:end -->"
OWN = "# My own rules\n\nAlways answer briefly.\n"


class InstructionsAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.vault = cls.base / "vault"
        made = run("scripts/make_test_vault.py", str(cls.vault))
        if made.returncode != 0:
            raise AssertionError(made.stdout + made.stderr)
        cls.config = cls.base / "config.json"
        cls.config.write_text(json.dumps(NAMES), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def install(self, home: Path, *flags: str):
        return run("install.py", "--vault", str(self.vault), "--config", str(self.config),
                   "--home", str(home), "--no-skills", *flags)

    def test_1_block_written_own_lines_kept_backed_up_idempotent(self):
        installed = [h for h in FILES if shutil.which(h)]
        if not installed:
            self.skipTest("no harness CLI on PATH")
        home = self.base / "home"
        claude_md = home / FILES["claude"]
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text(OWN, encoding="utf-8")
        res = self.install(home)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for harness in installed:
            text = (home / FILES[harness]).read_text(encoding="utf-8")
            self.assertEqual(text.count(START), 1, harness)
            self.assertEqual(text.count(END), 1, harness)
            block = text[text.index(START):text.index(END)]
            if harness == "claude":
                # Task-17: Claude imports the vault's copy; the text itself lives in the vault.
                self.assertIn("@", block)
                self.assertIn(".brain/instructions/claude.md", block.replace("\\ ", " "))
                block = (self.vault / ".brain" / "instructions" / "claude.md").read_text(encoding="utf-8")
            self.assertIn("Echo", block)
            self.assertIn("Sam", block)
            self.assertIn("recall.py", block)
            self.assertIn("gardener.py", block)
        if "claude" in installed:
            text = claude_md.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(OWN.rstrip("\n")), "the user's own CLAUDE.md lines moved or changed")
            backups = [p for p in claude_md.parent.iterdir() if p.is_file() and p != claude_md
                       and p.name.startswith("CLAUDE.md")]
            self.assertTrue(any(p.read_text(encoding="utf-8") == OWN for p in backups), backups)
            settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings.get("env", {}).get("PYTHONUTF8"), "1")

        before = snapshot(self.vault, home)
        again = self.install(home)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(before, snapshot(self.vault, home))

    def test_2_opt_out_and_project_scope_write_no_instruction_file(self):
        for name, flags in (("home-none", ("--no-instructions",)), ("home-project", ("--scope", "project"))):
            home = self.base / name
            res = self.install(home, *flags)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            for relative in FILES.values():
                self.assertFalse((home / relative).exists(), f"{name}: {relative}")


if __name__ == "__main__":
    unittest.main()
