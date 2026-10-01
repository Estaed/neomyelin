"""Acceptance tests for the friction ledger, Evolution.md and the knowledge audit (Task-05, Task-07).

Written by the orchestrator, not the builder (Blueprint -> Constraints): Evolution.md and the
knowledge notes are user files the nightly run writes into, so nothing it did not write may
change. Runs on a real installed vault. BM25 recall, so a local Ollama cannot hide a broken
default; the knowledge judge is tests/acceptance/fake_judge.py behind a fake `claude` on PATH.
Behaviour follows the original scripts: the gardener counts distinct days, the audit reads the
last 48 hours.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_install import ENV, ROOT, fake_cli, run

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English", "harnesses": ["claude"]}
USER_LINE = "- Decision (mine, keep): always ask before deleting a folder.\n"
USER_NOTE = "# Otters\n\nMy own note about otters; the audit must never rewrite it.\n"


class EvolutionAcceptance(unittest.TestCase):
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
        companion = cls.vault / "850-Companion 🔮"
        cls.evolution = companion / "Evolution.md"
        if not cls.evolution.exists():  # the packaging wave makes install.py place it
            shutil.copyfile(ROOT / "templates" / "Evolution.md", cls.evolution)
        with cls.evolution.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(USER_LINE)
        cls.bin = base / "bin"
        cls.bin.mkdir()
        fake_cli(cls.bin, "claude", Path(__file__).resolve().parent / "fake_judge.py")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def brain(self, script: str, *args: str, fake_model: bool = False) -> subprocess.CompletedProcess:
        env = {**ENV, "NEOMYELIN_RECALL": "bm25"}
        if fake_model:
            env["PATH"] = str(self.bin) + os.pathsep + os.environ.get("PATH", "")
        return subprocess.run(
            [sys.executable, str(self.vault / ".brain" / "scripts" / script), *args],
            cwd=self.vault, env=env, capture_output=True, text=True, encoding="utf-8", timeout=600,
        )

    def record(self, symptom: str, *extra: str) -> None:
        res = self.brain("gardener.py", "record", "--category", "git", "--symptom", symptom,
                         "--evidence", "session log", "--workaround", "committed from the main loop",
                         "--proposal", "let the orchestrator commit for sandboxed bees", *extra)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def ledger(self) -> Path:
        found = list((self.vault / ".brain").rglob("agent-experience.jsonl"))
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_1_friction_on_two_days_makes_one_candidate_append_only(self):
        start = self.evolution.read_bytes()

        self.record("git commit failed in the sandbox with index.lock permission denied")
        one = self.brain("gardener.py", "candidates")
        self.assertEqual(one.returncode, 0, one.stdout + one.stderr)
        self.assertEqual(self.evolution.read_bytes(), start, "one entry must not make a candidate")

        # Move the first record two days back: the original counts distinct calendar days.
        path = self.ledger()
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        earlier = (dt.datetime.now().astimezone() - dt.timedelta(days=2)).isoformat(timespec="seconds")
        rows[0]["created_at"] = earlier
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

        self.record("sandbox refused the git commit: permission denied on index.lock",
                    "--same", rows[0]["fingerprint"])
        two = self.brain("gardener.py", "candidates")
        self.assertEqual(two.returncode, 0, two.stdout + two.stderr)
        after = self.evolution.read_bytes()
        self.assertTrue(after.startswith(start), "existing Evolution.md text was changed")
        self.assertGreater(len(after), len(start), "friction on two days made no candidate")
        self.assertIn(USER_LINE.encode("utf-8"), after)

        again = self.brain("gardener.py", "candidates")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(self.evolution.read_bytes(), after, "the same candidate was written twice")

    def test_2_knowledge_audit_files_the_missing_note_once(self):
        concepts = self.vault / "knowledge" / "concepts"
        concepts.mkdir(parents=True, exist_ok=True)
        user_note = concepts / "otters.md"
        user_note.write_text(USER_NOTE, encoding="utf-8")
        receipts = self.vault / "receipts"
        receipts.mkdir(exist_ok=True)
        meta = {"kind": "receipt", "event_id": "2026-01-02-test", "refs": ["AGENTS.md"],
                "created_at": dt.datetime.now().astimezone().isoformat()}
        (receipts / "acceptance-r1.md").write_text(
            "---\n" + json.dumps(meta) + "\n---\n[Test] lesson\n**Learning**\n"
            "- Sea otters hold hands while sleeping so they do not drift apart.\n",
            encoding="utf-8")

        res = self.brain("knowledge_audit.py", fake_model=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        written = [p for p in concepts.glob("*.md") if p != user_note]
        self.assertEqual(len(written), 1, res.stdout + res.stderr)
        self.assertIn("Sea otters hold hands while sleeping.", written[0].read_text(encoding="utf-8"))
        self.assertEqual(user_note.read_text(encoding="utf-8"), USER_NOTE)
        first = {p.name: p.read_bytes() for p in concepts.glob("*.md")}

        again = self.brain("knowledge_audit.py", fake_model=True)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in concepts.glob("*.md")}, first,
                         "the audit filed the same lesson twice or touched a note")


if __name__ == "__main__":
    unittest.main()
