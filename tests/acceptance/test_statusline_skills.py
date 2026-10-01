"""Acceptance tests for the status line option and the limit skill copy (Task-08).

Written by the orchestrator, not the builder (Blueprint -> Constraints): both write into the
user's Claude settings and skill folders. Never touches the real home directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_install import run, snapshot

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English", "harnesses": ["claude"]}
MINE = {"type": "command", "command": "echo my-own-status"}


class StatuslineSkillsAcceptance(unittest.TestCase):
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
                   "--home", str(home), *flags)

    def test_1_user_status_line_kept_without_the_flag_and_skill_copied(self):
        home = self.base / "home-keep"
        settings = home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"statusLine": MINE}, indent=2), encoding="utf-8")
        res = self.install(home)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["statusLine"], MINE)
        self.assertTrue((home / ".claude" / "skills" / "limit" / "SKILL.md").is_file())

    def test_2_flag_sets_ours_with_backup_and_is_idempotent(self):
        home = self.base / "home-flag"
        settings = home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        raw = json.dumps({"statusLine": MINE}, indent=2)
        settings.write_text(raw, encoding="utf-8")
        res = self.install(home, "--statusline")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        line = json.loads(settings.read_text(encoding="utf-8"))["statusLine"]
        self.assertIn("statusline.py", line.get("command", ""))
        backups = [p for p in settings.parent.iterdir() if p != settings and p.is_file()]
        self.assertTrue(any(p.read_text(encoding="utf-8") == raw for p in backups), backups)
        before = snapshot(self.vault, home)
        again = self.install(home, "--statusline")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(before, snapshot(self.vault, home))

    def test_2b_a_linked_or_foreign_skill_is_never_written_through(self):
        # On a machine that already runs another brain, ~/.claude/skills/limit is a junction into
        # that brain; writing through it would replace the other brain's skill.
        other = self.base / "other-brain" / "limit"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("other brain's skill\n", encoding="utf-8")
        home = self.base / "home-linked"
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(skills / "limit"), str(other)],
                                  capture_output=True, text=True)
            if made.returncode != 0:
                self.skipTest(f"cannot create a junction here: {made.stdout} {made.stderr}")
        else:
            os.symlink(other, skills / "limit", target_is_directory=True)
        res = self.install(home)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual((other / "SKILL.md").read_text(encoding="utf-8"), "other brain's skill\n")

        foreign = self.base / "home-foreign" / ".claude" / "skills" / "limit" / "SKILL.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("a skill the user wrote\n", encoding="utf-8")
        res = self.install(self.base / "home-foreign")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(foreign.read_text(encoding="utf-8"), "a skill the user wrote\n")

    def test_2c_project_scope_writes_no_user_skill(self):
        home = self.base / "home-project"
        res = self.install(home, "--scope", "project")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse((home / ".claude" / "skills").exists())

    def test_3_no_skills_copies_no_skill(self):
        home = self.base / "home-noskills"
        res = self.install(home, "--no-skills")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse((home / ".claude" / "skills").exists())


if __name__ == "__main__":
    unittest.main()
