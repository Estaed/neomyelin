"""Acceptance tests for the vault layout install writes (Task-09).

Written by the orchestrator, not the builder (Blueprint -> Constraints): install creates folders
and files in the user's vault and edits its AGENTS.md, so it must never lose what is there.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_install import ENV, run, snapshot

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English", "harnesses": ["claude"]}
FOLDERS = ("200-Goals ⚔️", "300-Education 🎓", "400-Work 💼", "500-Projects 🏰", "600-Life 🌿",
           "700-Private 🔐", "800-Arsenal 🛠️", "900-Archive 📦")
OWN_AGENTS = "# My vault\n\nMy own instruction line, kept by the installer.\n"


class LayoutAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.vault = base / "vault"
        made = run("scripts/make_test_vault.py", str(cls.vault))
        if made.returncode != 0:
            raise AssertionError(made.stdout + made.stderr)
        cls.user_note = cls.vault / "📥 000-Inbox" / "my-note.md"
        cls.user_note.parent.mkdir(exist_ok=True)
        cls.user_note.write_text("# Mine\n\nA note the user wrote before NeoMyelin.\n", encoding="utf-8")
        # Task-15: install works on an empty folder or an existing vault and never overwrites a note.
        (cls.vault / "AGENTS.md").write_text(OWN_AGENTS, encoding="utf-8")
        cls.config = base / "config.json"
        cls.config.write_text(json.dumps(NAMES), encoding="utf-8")
        cls.home = base / "home"
        res = cls.install()
        if res.returncode != 0:
            raise AssertionError(res.stdout + res.stderr)

    @classmethod
    def install(cls):
        return run("install.py", "--vault", str(cls.vault), "--config", str(cls.config),
                   "--home", str(cls.home), "--no-skills")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_1_structure_and_companion_files(self):
        for name in FOLDERS:
            folder = self.vault / name
            self.assertTrue(folder.is_dir(), name)
            self.assertTrue(any(folder.glob("*.md")), f"{name} has no note")
        companion = self.vault / "850-Companion 🔮"
        for name in ("Personality.md", "Evolution.md", "Decisions.md"):
            self.assertTrue((companion / name).is_file(), name)
        self.assertTrue((self.vault / ".brain" / "patterns.md").is_file())

    def test_2_agents_md_gets_our_block_and_keeps_the_users_text(self):
        after = (self.vault / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(OWN_AGENTS.strip(), after)
        self.assertEqual(after.count("<!-- neomyelin:start -->"), 1)
        self.assertEqual(after.count("<!-- neomyelin:end -->"), 1)
        self.assertIn("700-Private", after)

    def test_3_user_files_survive_and_second_run_changes_nothing(self):
        self.assertTrue(self.user_note.is_file(), "a user note in an existing folder was removed")
        evolution = self.vault / "850-Companion 🔮" / "Evolution.md"
        with evolution.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("- My own decision line.\n")
        before = snapshot(self.vault, self.home)
        res = self.install()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(before, snapshot(self.vault, self.home))
        self.assertIn("- My own decision line.", evolution.read_text(encoding="utf-8"))

    def test_4_recall_never_returns_private_or_archive(self):
        notes = {"700-Private 🔐": "secret", "900-Archive 📦": "archived", "600-Life 🌿": "visible"}
        for folder, word in notes.items():
            (self.vault / folder / f"quokka-{word}.md").write_text(
                f"# Quokka {word}\n\nThe quokka passport renewal note, {word} copy.\n", encoding="utf-8")
        res = subprocess.run([sys.executable, str(self.vault / ".brain" / "scripts" / "recall.py"),
                              "quokka passport renewal", "--k", "8"],
                             cwd=self.vault, env={**ENV, "NEOMYELIN_RECALL": "bm25"}, capture_output=True,
                             text=True, encoding="utf-8", timeout=600)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("quokka-visible", res.stdout)
        self.assertNotIn("quokka-secret", res.stdout)
        self.assertNotIn("quokka-archived", res.stdout)


if __name__ == "__main__":
    unittest.main()
