"""Acceptance test for project cards at session start (Task-16), written by the orchestrator.

A session opened in a project's folder must know which project it is in: the card under
`500-Projects 🏰/` names the folder (`path:`) and the label (`label:`); receipts belong to the
label by their first line `[<label>]`, tasks by their `project` field. A folder without a card is
no project, so no project's record leaks into it (as the original brain does).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from test_install import ENV, run

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English", "harnesses": ["claude"]}


class ProjectCardsAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(cls.tmp.name)
        cls.vault = base / "vault"
        cls.vault.mkdir()
        cls.project = base / "work" / "walrus-app"
        cls.project.mkdir(parents=True)
        cls.elsewhere = base / "work" / "no-card-here"
        cls.elsewhere.mkdir(parents=True)
        config = base / "config.json"
        config.write_text(json.dumps(NAMES), encoding="utf-8")
        res = run("install.py", "--vault", str(cls.vault), "--config", str(config), "--home", str(base / "home"))
        if res.returncode != 0:
            raise AssertionError(res.stdout + res.stderr)
        # Mark today's nightly run as done so the session start does not launch it (and a model call).
        state = cls.vault / ".brain" / ".state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "nightly.last-run").write_text(datetime.now().astimezone().isoformat(), encoding="utf-8")

        card = cls.vault / "500-Projects 🏰" / "Walrus.md"
        card.write_text(
            "---\ntitle: Walrus\ntype: project\nstatus: active\n"
            f"path: {cls.project.as_posix()}\nlabel: Walrus\n---\n# Walrus\n\nA demo project.\n",
            encoding="utf-8")
        ref = "500-Projects 🏰/Walrus.md"
        cls.brain("receipt", "--harness", "claude", json_file={
            "event_id": "2026-10-01-walrus", "refs": [ref],
            "summary": "[Walrus] Tusk polishing shipped (Model: test)\n- walrus work"})
        cls.brain("receipt", "--harness", "claude", json_file={
            "event_id": "2026-10-01-otter", "refs": [ref],
            "summary": "[Otter] River survey filed (Model: test)\n- otter work, newer"})
        for tid, title, project in (("walrus-next", "Walrus: weigh the tusks", "Walrus"),
                                    ("otter-next", "Otter: count the pebbles", "Otter")):
            cls.brain("task-create", json_file={
                "source": f"tasks/{tid}.md", "text": "Context.",
                "metadata": {"id": tid, "title": title, "status": "active", "owner": "Sam",
                             "project": project, "next_action": title.split(": ")[1]}})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def brain(cls, *args: str, json_file: dict) -> None:
        path = Path(cls.tmp.name) / "in.json"
        path.write_text(json.dumps(json_file, ensure_ascii=False), encoding="utf-8")
        res = subprocess.run([sys.executable, "brain.py", *args, "--file", str(path)], cwd=cls.vault,
                             env=ENV, capture_output=True, text=True, encoding="utf-8", timeout=120)
        if res.returncode != 0:
            raise AssertionError(res.stdout + res.stderr)

    def session_start(self, cwd: Path) -> str:
        payload = json.dumps({"cwd": str(cwd), "session_id": "acc-" + cwd.name, "hook_event_name": "SessionStart"})
        res = subprocess.run([sys.executable, str(self.vault / ".brain" / "scripts" / "memory_context.py"),
                              "--session-start"], cwd=cwd, input=payload,
                             env=ENV, capture_output=True,
                             text=True, encoding="utf-8", timeout=120)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        return data.get("hookSpecificOutput", data).get("additionalContext", "")

    def test_1_project_folder_shows_its_project(self):
        text = self.session_start(self.project)
        self.assertIn("Tusk polishing shipped", text)
        self.assertIn("weigh the tusks", text)
        self.assertNotIn("count the pebbles", text)

    def test_2_folder_without_card_gets_no_other_projects_record(self):
        # As the original brain: an unknown folder is no project, so no project's receipt or task leaks in.
        text = self.session_start(self.elsewhere)
        self.assertIn("[Memory: Identity]", text)
        for other in ("Tusk polishing shipped", "weigh the tusks", "count the pebbles"):
            self.assertNotIn(other, text)


if __name__ == "__main__":
    unittest.main()
