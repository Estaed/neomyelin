"""Acceptance tests for personality evolution on a real installed vault (Task-04).

Written by the orchestrator, not the builder (Blueprint -> Constraints): Personality.md is the
user's file. The model is replaced by a fake `claude` command on PATH that prints a canned reply,
so the whole path runs (receipts -> engine -> evidence gate -> files) without a model call.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_install import ENV, fake_cli, run

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English", "harnesses": ["claude"],
         "engine": "auto"}
QUOTE = "one recommendation, not a list of options"
MANUAL = "- My own line: answer in short sentences.\n"
TITLE = "One recommendation"


class PersonalityAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.vault = base / "vault"
        made = run("scripts/make_test_vault.py", str(cls.vault))
        if made.returncode != 0:
            raise AssertionError(made.stdout + made.stderr)
        cfg = base / "config.json"
        cfg.write_text(json.dumps(NAMES), encoding="utf-8")
        res = run("install.py", "--vault", str(cls.vault), "--config", str(cfg), "--home", str(base / "home"))
        if res.returncode != 0:
            raise AssertionError(res.stdout + res.stderr)
        cls.personality = cls.vault / "850-Companion 🔮" / "Personality.md"
        with cls.personality.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(MANUAL)
        cls.bin = base / "bin"
        cls.bin.mkdir()
        fake_cli(cls.bin, "claude", Path(__file__).resolve().parent / "fake_reply.py")
        cls.reply = base / "reply.json"
        cls.receipts = cls.vault / "receipts"
        cls.receipts.mkdir(exist_ok=True)
        cls.refs: list[dict] = []

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def add_receipt(self, days_ago: int, name: str) -> None:
        day = dt.date.today() - dt.timedelta(days=days_ago)
        created = dt.datetime.combine(day, dt.time(12, 0)).astimezone().isoformat()
        meta = {"kind": "receipt", "event_id": name, "refs": ["AGENTS.md"], "created_at": created}
        lines = ["---", json.dumps(meta), "---", "[Test] session",
                 f'OBSERVATION: Sam asked for "{QUOTE}" again.']
        path = self.receipts / f"{name}.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.refs.append({"day": day.isoformat(), "ref": f"receipts/{path.name}:5", "quote": QUOTE})

    def kos(self, proposal: list) -> subprocess.CompletedProcess:
        self.reply.write_text(json.dumps(proposal), encoding="utf-8")
        env = {**ENV, "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
               "NEOMYELIN_FAKE_REPLY": str(self.reply)}
        return subprocess.run([sys.executable, str(self.vault / ".brain" / "scripts" / "patterns.py"), "run"],
                              cwd=self.vault, env=env, capture_output=True, text=True, encoding="utf-8",
                              timeout=600)

    def proposal(self, evidence: list) -> list:
        return [{"title": TITLE, "area": "answers", "behavior": "Give one recommendation and the next step.",
                 "evidence": evidence}]

    def test_1_one_day_makes_no_line(self):
        self.add_receipt(5, "r-day1")
        before = self.personality.read_bytes()
        res = self.kos(self.proposal(self.refs * 3))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.personality.read_bytes(), before)

    def test_2_invented_quote_makes_no_line(self):
        self.add_receipt(4, "r-day2")
        self.add_receipt(3, "r-day3")
        before = self.personality.read_bytes()
        invented = [{**ref, "quote": "always wants long detailed lists"} for ref in self.refs]
        res = self.kos(self.proposal(invented))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.personality.read_bytes(), before)

    def test_3_three_days_add_one_line_keeping_the_users(self):
        before = self.personality.read_bytes()
        res = self.kos(self.proposal(self.refs))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        after = self.personality.read_bytes()
        self.assertTrue(after.startswith(before), "existing Personality text was changed")
        added = after[len(before):].decode("utf-8")
        self.assertIn(TITLE, added)
        self.assertEqual(added.count("\n"), 1, added)
        evidence = (self.vault / ".brain" / "patterns.md").read_text(encoding="utf-8")
        for ref in self.refs:
            self.assertIn(ref["ref"], evidence)

        again = self.kos(self.proposal(self.refs))
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.personality.read_bytes(), after, "the same pattern was added twice")

    def test_4_veto_removes_only_the_generated_line_and_sticks(self):
        res = subprocess.run([sys.executable, str(self.vault / ".brain" / "scripts" / "patterns.py"),
                              "veto", TITLE], cwd=self.vault, env=ENV, capture_output=True, text=True,
                             encoding="utf-8", timeout=600)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        text = self.personality.read_text(encoding="utf-8")
        self.assertNotIn(f"{TITLE}:", text)
        self.assertIn(MANUAL, text)
        vetoed = self.personality.read_bytes()
        again = self.kos(self.proposal(self.refs))
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.personality.read_bytes(), vetoed, "a vetoed pattern came back")


if __name__ == "__main__":
    unittest.main()
