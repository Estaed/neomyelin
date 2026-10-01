"""Session-start health lines: nightly job errors and the last doctor --save reach the user."""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'brain' / 'scripts'))
import memory_context  # noqa: E402


class HealthLines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        self.state = self.vault / '.brain' / '.state'
        self.state.mkdir(parents=True)
        self.today = dt.date.today()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, value: dict) -> None:
        (self.state / name).write_text(json.dumps(value), encoding='utf-8')

    def test_quiet_when_nothing_is_recorded(self):
        self.assertEqual(memory_context._health_lines(self.vault, self.today), [])

    def test_recent_job_error_and_doctor_warning_are_shown(self):
        self.write('health.json', {'ts': int(time.time()), 'component': 'stage', 'error': 'model CLI unavailable'})
        self.write('doctor.json', {'error': '', 'warnings': ['Recall index: missing', 'Daily: old']})
        lines = memory_context._health_lines(self.vault, self.today)
        self.assertEqual(len(lines), 2)
        self.assertIn('model CLI unavailable', lines[0])
        self.assertIn('2 warning(s)', lines[1])
        self.assertIn('Recall index: missing', lines[1])

    def test_old_job_error_is_dropped_but_fresh_warning_kept(self):
        old = int(time.time()) - 10 * 86400
        self.write('health.json', {'ts': old, 'error': 'stale failure',
                                   'warnings': [f'{self.today.isoformat()} patterns: model changed a file']})
        lines = memory_context._health_lines(self.vault, self.today)
        self.assertEqual(lines, ['Health: patterns: model changed a file'])

    def test_unreadable_files_do_not_break_the_session_start(self):
        (self.state / 'health.json').write_text('{not json', encoding='utf-8')
        (self.state / 'doctor.json').write_text('[]', encoding='utf-8')
        self.assertEqual(memory_context._health_lines(self.vault, self.today), [])


class DecisionAndEvolutionLines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.companion = Path(self.tmp.name)
        self.today = dt.date(2026, 3, 1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_due_open_decisions_are_shown_and_the_quoted_example_never(self):
        (self.companion / 'Decisions.md').write_text(
            '# Decisions\n\n> ## Decision: Example\n> **Check:** 2020-01-01\n\n'
            '## Decision: Due one\n**Check:** 2026-02-20\n\n'
            '## Decision: Done one\n**Check:** 2026-02-01\n**Outcome:** held\n\n'
            '## Decision: Later one\n**Check:** 2026-05-01\n', encoding='utf-8')
        lines = memory_context._decision_lines(self.companion, self.today)
        self.assertEqual(len(lines), 1)
        self.assertIn('Due one', lines[0])

    def test_pending_evolution_candidates_are_counted(self):
        (self.companion / 'Evolution.md').write_text(
            '# Evolution\n\n## Candidates\n\n### Candidate aaa\n- Decision: pending\n\n'
            '### Candidate bbb\n- Decision: rule\n\n### Idea ccc\n- Decision: pending\n', encoding='utf-8')
        self.assertIn('2 proposed fix', memory_context._evolution_line(self.companion))

    def test_no_files_no_lines(self):
        self.assertEqual(memory_context._decision_lines(self.companion, self.today), [])
        self.assertEqual(memory_context._evolution_line(self.companion), '')
        self.assertEqual(memory_context._decision_lines(None, self.today), [])


if __name__ == '__main__':
    unittest.main()
