from __future__ import annotations

from datetime import datetime, timedelta, timezone
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("neomyelin_limit", ROOT / "brain/scripts/limit.py")
limit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = limit
SPEC.loader.exec_module(limit)
SL_SPEC = importlib.util.spec_from_file_location("neomyelin_statusline_for_limit", ROOT / "brain/scripts/statusline.py")
statusline = importlib.util.module_from_spec(SL_SPEC)
sys.modules[SL_SPEC.name] = statusline
SL_SPEC.loader.exec_module(statusline)

# A stand-in agy: `--version`, and `-p /usage --output-format json` answered as FAKE_AGY_MODE says.
# Every call is appended to FAKE_AGY_RECORD (its arguments, working folder and stdin).
FAKE_AGY = r'''
import json, os, sys, time
mode = os.environ.get("FAKE_AGY_MODE", "ok")
args = sys.argv[1:]
with open(os.environ["FAKE_AGY_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"args": args, "cwd": os.getcwd(), "stdin": sys.stdin.read()}) + "\n")
if args == ["--version"]:
    print("1.1.10" if mode == "old" else "1.2.14")
    sys.exit(0)
if "--log-file" in args:
    with open(args[args.index("--log-file") + 1], "w", encoding="utf-8") as log:
        log.write("signed in as the account\n")
reset = os.environ["FAKE_AGY_RESET"]
if mode == "ok":
    print(json.dumps({"status": "SUCCESS", "num_turns": 0, "command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "description": "d", "buckets": [
            {"id": "gemini-weekly", "name": "Weekly Limit Remaining", "window": "weekly",
             "remaining_fraction": 0.75, "reset_time": reset},
            {"id": "gemini-5h", "name": "Five Hour Limit Remaining", "window": "5h",
             "remaining_fraction": 1, "reset_time": reset}]},
        {"name": "Claude and GPT models", "buckets": [
            {"id": "3p-weekly", "name": "Weekly Limit Remaining", "window": "weekly",
             "remaining_fraction": 0, "reset_time": reset},
            {"id": "3p-5h", "name": "Five Hour Limit Remaining", "window": "5h",
             "remaining_fraction": "n/a", "reset_time": reset}]}]}}}))
elif mode == "nojson":
    print("Please sign in first")
    sys.exit(1)
elif mode == "error":
    print(json.dumps({"status": "ERROR", "command": {}}))
elif mode == "nogroups":
    print(json.dumps({"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": []}}}))
elif mode == "hang":
    time.sleep(2)
'''


class LimitTests(unittest.TestCase):
    def test_missing_usage_is_unknown_not_zero(self):
        rendered = limit.render(
            {"windows": [{"name": "5 hour", "percent": None}], "warnings": []},
            {"windows": [], "warnings": []},
        )
        self.assertIn("unknown", rendered)
        self.assertNotIn("0%", rendered)

    def test_pool_has_original_public_keys(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(limit, "_home", return_value=Path(directory)), \
             patch.dict(os.environ, {"APPDATA": directory}):
            pool = limit.read_codex(scan_limit=0)
        self.assertTrue({"source", "windows", "warnings"} <= pool.keys())
        self.assertIn("unknown", " ".join(pool["warnings"]))
        for window in pool["windows"]:
            self.assertTrue({"name", "percent", "resets_at", "remaining", "expired"} <= window.keys())


class AgyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        script = self.base / "fake_agy.py"
        script.write_text(FAKE_AGY, encoding="utf-8")
        if os.name == "nt":
            self.exe = self.base / "agy.cmd"
            self.exe.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            self.exe = self.base / "agy"
            self.exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            self.exe.chmod(0o755)
        self.record = self.base / "calls.jsonl"
        self.cache = self.base / "vault" / ".brain" / ".state" / "limit-agy.json"
        reset = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.env = {"FAKE_AGY_RECORD": str(self.record), "FAKE_AGY_RESET": reset}

    def read(self, mode: str = "ok", exe: object = "fake") -> dict:
        with patch.object(limit, "find_agy", return_value=str(self.exe) if exe == "fake" else exe), \
             patch.dict(os.environ, {**self.env, "FAKE_AGY_MODE": mode}):
            return limit.read_agy(self.cache)

    def calls(self) -> list[dict]:
        if not self.record.is_file():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def test_real_numbers_from_usage_and_a_cache(self):
        agy = self.read()
        self.assertEqual((agy["status"], agy["version"], agy["reason"]), ("ok", "1.2.14", None))
        windows = {window["name"]: window for window in agy["windows"]}
        weekly = windows["Gemini Models weekly"]
        self.assertEqual(weekly["percent"], 25.0)
        self.assertTrue(weekly["remaining"])
        self.assertFalse(weekly["not_started"])
        five = windows["Gemini Models 5 hour"]
        self.assertEqual((five["percent"], five["not_started"], five["resets_at"], five["remaining"]),
                         (0.0, True, None, None))
        self.assertEqual(windows["Claude and GPT models weekly"]["percent"], 100.0)
        self.assertIsNone(windows["Claude and GPT models 5 hour"]["percent"])  # unreadable: unknown, not 0

        usage = [call for call in self.calls() if call["args"][:2] == ["-p", "/usage"]]
        self.assertEqual(len(usage), 1, self.calls())
        args = usage[0]["args"]
        self.assertEqual(args[2:6], ["--output-format", "json", "--print-timeout", "30s"])
        log = Path(args[args.index("--log-file") + 1])
        self.assertFalse(log.parent.exists(), "the folder with agy's log (it holds the account email) is still there")
        self.assertEqual(Path(usage[0]["cwd"]).resolve(), log.parent.resolve())
        self.assertEqual(usage[0]["stdin"], "")

        cached = self.cache.read_text(encoding="utf-8")
        self.assertNotIn("account", cached)
        self.assertNotIn("description", cached)
        again = self.read()
        self.assertEqual(len([c for c in self.calls() if c["args"][:2] == ["-p", "/usage"]]), 1)
        self.assertEqual(again["source"], "agy -p /usage (cached)")
        self.assertEqual(again["windows"], agy["windows"])

        text = limit.render({"windows": [], "warnings": []}, {"windows": [], "warnings": []}, agy=agy)
        section = text[text.index("AGY"):]
        self.assertIn("agy 1.2.14", section)
        self.assertIn("25%", section)
        self.assertIn("not started", section)
        self.assertIn("resets in", section)
        self.assertGreater(text.index("AGY"), text.index("CODEX"))

    def test_missing_agy_is_unknown_with_a_reason(self):
        agy = self.read(exe=None)
        self.assertEqual((agy["status"], agy["windows"]), ("unknown", []))
        self.assertIn("not installed", agy["reason"])
        text = limit.render({"windows": [], "warnings": []}, {"windows": [], "warnings": []}, agy=agy)
        section = text[text.index("AGY"):]
        self.assertIn("usage: unknown (agy is not installed", section)
        self.assertNotIn("0%", section)
        self.assertFalse(self.cache.exists())

    def test_agy_older_than_1_1_11_is_never_sent_usage(self):
        agy = self.read("old")
        self.assertEqual(agy["status"], "unknown")
        self.assertIn("older than 1.1.11", agy["reason"])
        self.assertEqual([call["args"] for call in self.calls()], [["--version"]])

    def test_each_failure_is_unknown_with_its_reason(self):
        for mode, words in (("nojson", "no JSON"), ("error", "not SUCCESS"), ("nogroups", "no quota groups")):
            with self.subTest(mode=mode):
                agy = self.read(mode)
                self.assertEqual((agy["status"], agy["windows"]), ("unknown", []))
                self.assertIn(words, agy["reason"])
                self.assertNotIn("sign in first", agy["reason"])  # agy's own text is never echoed
                self.assertFalse(self.cache.exists())

    def test_a_hanging_agy_times_out_as_unknown(self):
        with patch.object(limit, "AGY_TIMEOUT", 1), contextlib.redirect_stderr(io.StringIO()):
            agy = self.read("hang")
        self.assertEqual(agy["status"], "unknown")
        self.assertIn("no answer within 1s", agy["reason"])

    def test_json_output_has_an_agy_key(self):
        empty = {"windows": [], "warnings": []}
        out = io.StringIO()
        with patch.object(limit, "read_claude", return_value=dict(empty)), \
             patch.object(limit, "read_codex", return_value=dict(empty)), \
             patch.object(limit, "read_codex_banked", return_value=([], None)), \
             patch.object(limit, "read_agy", return_value={**empty, "status": "unknown", "reason": "x"}), \
             contextlib.redirect_stdout(out):
            self.assertEqual(limit.main(["--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["agy"]["reason"], "x")

    def test_statusline_never_calls_agy(self):
        payload = {"session_id": "s", "model": {"display_name": "Example"}, "context_window": {"used_percentage": 1},
                   "rate_limits": {"five_hour": {"used_percentage": 1}, "seven_day": {"used_percentage": 2}}}
        spies = {name: MagicMock(side_effect=AssertionError(name))
                 for name in ("find_agy", "agy_version", "read_agy", "_agy_usage")}
        with tempfile.TemporaryDirectory() as directory, \
             patch.multiple(limit, **spies), \
             patch.object(limit, "read_codex", return_value={"windows": [], "age_minutes": None}), \
             patch.object(limit, "_fetch_claude_live_detailed", return_value=(None, "offline")), \
             patch.object(statusline, "SCRIPT_DIR", Path(directory)), \
             patch.object(statusline, "run", return_value=""), \
             patch.object(statusline.tempfile, "tempdir", directory), \
             patch.dict(sys.modules, {"limit": limit}):
            output = statusline.build(payload)
        self.assertEqual(len(output.splitlines()), 2)
        for name, spy in spies.items():
            self.assertFalse(spy.called, f"statusline called limit.{name}")


if __name__ == "__main__":
    unittest.main()
