from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
from unittest.mock import patch
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("neomyelin_statusline", ROOT / "brain/scripts/statusline.py")
statusline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = statusline
SPEC.loader.exec_module(statusline)


class StatusLineTests(unittest.TestCase):
    def test_emits_two_lines_and_records_session_quota(self):
        payload = {
            "session_id": "invented-session",
            "model": {"display_name": "Example"},
            "context_window": {"used_percentage": 42},
            "rate_limits": {
                "five_hour": {"used_percentage": 17},
                "seven_day": {"used_percentage": 28},
            },
        }
        mock_limit = types.ModuleType("limit")
        mock_limit.read_codex = lambda: {"windows": [], "age_minutes": None}
        mock_limit._fetch_claude_live_detailed = lambda **_kwargs: (None, "unavailable")
        mock_limit.model_windows = lambda _usage: []
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(statusline, "SCRIPT_DIR", Path(directory)), \
             patch.object(statusline, "run", return_value=""), \
             patch.object(statusline.tempfile, "tempdir", directory), \
             patch.dict(os.environ, {"TMPDIR": directory}), \
             patch.dict(sys.modules, {"limit": mock_limit}):
            output = statusline.build(payload)
        self.assertEqual(len(output.splitlines()), 2)
        self.assertIn("cl5h", output)

    def test_invalid_or_missing_usage_does_not_render_as_zero(self):
        self.assertEqual(statusline.floor_pct({}, "missing"), -1)
        self.assertEqual(statusline.floor_pct({"n": False}, "n"), -1)


if __name__ == "__main__":
    unittest.main()
