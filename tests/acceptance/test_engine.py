"""Acceptance tests for the own engine, brain.py (Task-15), written by the orchestrator.

Receipts and tasks are the user's record, a path that can lose data, so these tests are kept
apart from the code's author (Blueprint -> Constraints). They use only the interface in
tasks/Task-15.md and the file formats of the owner's real receipts, tasks and daily view.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from test_install import ENV, run

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English",
         "harnesses": ["claude"]}
RECEIPT_KEYS = {"kind", "event_id", "harness", "refs", "visibility", "created_at", "session"}
TASK_KEYS = {"id", "title", "status", "owner", "project", "next_action", "kind", "revision"}
SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), path
    _, head, body = text.split("---", 2)
    return json.loads(head), body


class EngineAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.vault = base / "vault"
        cls.vault.mkdir()
        config = base / "config.json"
        config.write_text(json.dumps(NAMES), encoding="utf-8")
        res = run("install.py", "--vault", str(cls.vault), "--config", str(config),
                  "--home", str(base / "home"))
        if res.returncode != 0:
            raise AssertionError(f"install from an empty folder failed:\n{res.stdout}\n{res.stderr}")
        (cls.vault / "demo-note.md").write_text("# Demo\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def brain(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "brain.py", *args], cwd=self.vault, env=ENV,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def write_json(self, name: str, data: dict) -> str:
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def result(self, res: subprocess.CompletedProcess) -> dict:
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return json.loads(res.stdout)

    def test_0_installed(self):
        self.assertTrue((self.vault / "brain.py").is_file())
        self.assertTrue((self.vault / "850-Companion 🔮" / "Rules.md").is_file())

    def test_1_receipt_lands_in_the_daily_view(self):
        event = "2026-10-01-demo-engine"
        summary = ("[Demo] Engine smoke (Model: test)\n**Done**\n- The engine wrote this receipt.\n"
                   f"- token {SECRET} must not survive.")
        src = self.write_json("r1.json", {"event_id": event, "summary": summary,
                                          "refs": ["demo-note.md"], "session": "s-accept"})
        out = self.result(self.brain("receipt", "--file", src, "--harness", "claude"))
        self.assertGreaterEqual(out.get("secrets_redacted", 0), 1, out)

        name = hashlib.sha256(event.encode("utf-8")).hexdigest() + ".md"
        receipt = self.vault / "receipts" / name
        self.assertTrue(receipt.is_file(), list((self.vault / "receipts").glob("*")))
        meta, body = frontmatter(receipt)
        self.assertTrue(RECEIPT_KEYS <= set(meta), meta)
        self.assertEqual(meta["event_id"], event)
        self.assertEqual(meta["harness"], "claude")
        self.assertEqual(meta["refs"], ["demo-note.md"])
        self.assertEqual(meta["session"], "s-accept")
        self.assertIn("The engine wrote this receipt.", body)
        self.assertNotIn(SECRET, receipt.read_text(encoding="utf-8"))

        day = datetime.fromisoformat(meta["created_at"]).astimezone().strftime("%Y-%m-%d")
        daily = (self.vault / "daily" / f"{day}.md").read_text(encoding="utf-8")
        self.assertIn(f"# Daily: {day}", daily)
        self.assertTrue(any(l.startswith("### ") and "[Demo] Engine smoke" in l for l in daily.splitlines()),
                        daily)
        self.assertIn(f"[[receipts/{name}", daily)
        self.assertNotIn(SECRET, daily)

        before = receipt.read_bytes()
        again = self.brain("receipt", "--file", src, "--harness", "claude")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(len(list((self.vault / "receipts").glob("*.md"))), 1)

    def test_2_receipt_refs_are_checked(self):
        for i, refs in enumerate(([], ["no/such-note.md"])):
            src = self.write_json(f"bad{i}.json", {"event_id": f"2026-10-01-bad-{i}",
                                                   "summary": "[Demo] bad", "refs": refs})
            res = self.brain("receipt", "--file", src, "--harness", "claude")
            refused = res.returncode != 0 or json.loads(res.stdout or "{}").get("status") != "succeeded"
            self.assertTrue(refused, f"refs {refs} accepted: {res.stdout}{res.stderr}")
            name = hashlib.sha256(f"2026-10-01-bad-{i}".encode("utf-8")).hexdigest() + ".md"
            self.assertFalse((self.vault / "receipts" / name).exists())

    def test_3_task_create_update_history(self):
        meta = {"id": "demo-task", "title": "Demo task", "status": "active", "owner": "Sam",
                "project": "Demo", "next_action": "check the engine"}
        created = self.brain("task-create", "--file", self.write_json("t.json", {
            "source": "tasks/demo-task.md", "text": "Context line.", "metadata": meta}))
        self.result(created)
        task = self.vault / "tasks" / "demo-task.md"
        fm, body = frontmatter(task)
        self.assertTrue(TASK_KEYS <= set(fm), fm)
        self.assertIn("Context line.", body)
        rev = fm["revision"]

        ok = self.brain("task-update", "--file", self.write_json("u1.json", {
            "id": "demo-task", "expected_revision": rev, "changes": {"status": "done"}}))
        self.result(ok)
        fm2, _ = frontmatter(task)
        self.assertEqual(fm2["status"], "done")
        self.assertGreater(fm2["revision"], rev)

        stale_before = task.read_bytes()
        stale = self.brain("task-update", "--file", self.write_json("u2.json", {
            "id": "demo-task", "expected_revision": rev, "changes": {"status": "active"}}))
        refused = stale.returncode != 0 or json.loads(stale.stdout or "{}").get("status") != "succeeded"
        self.assertTrue(refused, f"stale revision accepted: {stale.stdout}{stale.stderr}")
        self.assertEqual(task.read_bytes(), stale_before)

        hist = self.brain("history", "tasks/demo-task.md")
        self.assertEqual(hist.returncode, 0, hist.stdout + hist.stderr)
        self.assertIn("done", hist.stdout)


if __name__ == "__main__":
    unittest.main()
