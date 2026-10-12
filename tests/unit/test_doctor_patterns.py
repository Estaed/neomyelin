"""doctor.check_patterns: seven pattern runs in a row that produce nothing, while observations
wait, are a warning; a fresh install stays clean."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, receipt, write

import doctor
import patterns
import stage

NOW = dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.timezone.utc)
EMPTY = 'Patterns: 0 added, 0 evidence updates, 0 rejected, 0 sleeping, 0 waking.'
EVIDENCE = 'Patterns: 0 added, 2 evidence updates, 0 rejected, 0 sleeping, 0 waking.'
ADDED = 'Patterns: 1 added, 0 evidence updates, 0 rejected, 0 sleeping, 0 waking.'
FAILED = 'Pattern evolution failed: model CLI unavailable: claude'
WAITING = 'Patterns: waiting, observations cover 2 of 3 days; no model call.'


class CheckPatternsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / '.brain' / '.state'
        self.state.mkdir(parents=True)
        (self.root / '.brain' / 'patterns.md').write_bytes(
            patterns.advance_scan(patterns.EMPTY, dt.date(2026, 10, 9)).encode('utf-8'))

    def log(self, *summaries: str, newline: str = '\n') -> None:
        lines = [f'2026-10-{index % 28 + 1:02d}T21:00:00+00:00 {summary}'
                 for index, summary in enumerate(summaries)]
        (self.state / patterns.LOG).write_bytes((newline.join(lines) + newline).encode('utf-8'))

    def pending(self, count: int = 1, day: str = '2026-10-10') -> None:
        write(self.root / 'daily' / f'{day}.md',
              ''.join(f'- OBSERVATION: the user said "a waiting phrase number {index}"\n'
                      for index in range(count)))

    def check(self) -> tuple[str, str]:
        return doctor.check_patterns(self.root, NOW)

    def test_a_fresh_install_with_no_log_and_nothing_waiting_is_ok(self):
        self.assertEqual(self.check()[0], doctor.OK)

    def test_no_log_while_observations_from_finished_days_wait_warns(self):
        self.pending(2)
        status, detail = self.check()
        self.assertEqual(status, doctor.WARNING)
        self.assertIn('no pattern run is logged while 2 observation(s)', detail)

    def test_no_log_with_only_todays_observations_is_ok(self):
        self.pending(day='2026-10-11')
        self.assertEqual(self.check()[0], doctor.OK)

    def test_seven_empty_runs_while_observations_wait_warn_and_point_at_the_reply(self):
        self.log(*[EMPTY] * 7)
        self.pending(2)
        status, detail = self.check()
        self.assertEqual(status, doctor.WARNING)
        self.assertIn('the last 7 pattern runs produced nothing while 2 observation(s) wait', detail)
        self.assertIn('.brain/.state/patterns-last-reply.md', detail)

    def test_a_waiting_observation_in_a_receipt_counts(self):
        self.log(*[EMPTY] * 7)
        receipt(self.root, 'r', '- REACTION: objected: "a receipt phrase waiting"',
                created_at='2026-10-10T12:00:00+00:00')
        self.assertEqual(self.check()[0], doctor.WARNING)

    def test_todays_observation_counts_once_runs_are_logged(self):
        self.log(*[EMPTY] * 7)
        self.pending(day='2026-10-11')
        self.assertEqual(self.check()[0], doctor.WARNING)

    def test_six_empty_runs_are_ok(self):
        self.log(*[EMPTY] * 6)
        self.pending()
        status, detail = self.check()
        self.assertEqual(status, doctor.OK)
        self.assertIn('0 of the last 6 pattern runs', detail)

    def test_seven_empty_runs_with_nothing_waiting_are_ok(self):
        self.log(*[EMPTY] * 7)
        self.pending(day='2026-10-09')  # the last scan day: already read
        self.assertEqual(self.check()[0], doctor.OK)

    def test_only_the_last_seven_runs_count(self):
        self.log(ADDED, *[EMPTY] * 7)
        self.pending()
        self.assertEqual(self.check()[0], doctor.WARNING)

    def test_one_productive_run_in_the_last_seven_is_ok(self):
        for productive in (EVIDENCE, ADDED):
            with self.subTest(productive=productive):
                self.log(*[EMPTY] * 6, productive)
                self.pending()
                status, detail = self.check()
                self.assertEqual(status, doctor.OK)
                self.assertIn('1 of the last 7 pattern runs added a pattern or evidence', detail)

    def test_ten_is_not_zero(self):
        self.pending()
        self.log(*[EMPTY] * 6, EMPTY.replace('0 added', '10 added'))
        self.assertEqual(self.check()[0], doctor.OK)
        self.log(*[EMPTY] * 6, EMPTY.replace('0 evidence updates', '10 evidence updates'))
        self.assertEqual(self.check()[0], doctor.OK)

    def test_other_lines_are_ignored_and_a_crlf_log_is_read(self):
        lines = []
        for _ in range(7):
            lines += [EMPTY, 'WARNING: some other line', WAITING]
        self.log(*lines, newline='\r\n')
        self.pending()
        self.assertEqual(self.check()[0], doctor.WARNING)

    def test_failed_runs_produced_nothing(self):
        self.log(*[FAILED] * 7)
        self.pending()
        self.assertEqual(self.check()[0], doctor.WARNING)
        self.log(FAILED, FAILED, FAILED, ADDED)
        status, detail = self.check()
        self.assertEqual(status, doctor.OK)
        self.assertIn('1 of the last 4', detail)

    def test_waiting_lines_are_not_runs(self):
        self.log(*[WAITING] * 7)
        self.pending()
        status, detail = self.check()
        self.assertEqual(status, doctor.OK)
        self.assertIn('no pattern run with a model call yet', detail)

    def test_the_check_is_part_of_the_report(self):
        self.log(*[EMPTY] * 7)
        self.pending()
        checks = {check.name: check for check in doctor.run_checks(self.root, NOW, home=self.root / 'home')}
        self.assertIn('Pattern evolution', checks)
        self.assertEqual(checks['Pattern evolution'].status, doctor.WARNING)


class RealRunTests(unittest.TestCase):
    """The doctor reads the lines patterns.run really writes, not a copy of their format."""

    def test_seven_real_empty_runs_warn_and_one_with_evidence_clears_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault, cfg = make_vault(Path(temporary))
            start = dt.date(2026, 10, 1)
            for offset in range(10):
                day = start + dt.timedelta(days=offset)
                write(vault / 'daily' / f'{day}.md',
                      f'- OBSERVATION: the user said "a phrase from day {offset}"\n')
            reply = '[]'

            def answer(_prompt, **_kwargs):
                return reply

            with mock.patch.object(stage.engine, 'ask', side_effect=answer):
                for offset in range(3, 10):  # seven nightly runs, each with a new finished day
                    self.assertEqual(patterns.run(cfg, today=start + dt.timedelta(days=offset)), 0)
                today = dt.datetime.combine(start + dt.timedelta(days=9), dt.time(12)).astimezone()
                status, detail = doctor.check_patterns(vault, today)
                self.assertEqual(status, doctor.WARNING, detail)
                self.assertIn('1 observation(s) wait', detail)  # today's line, after the last scan
                ledger = (vault / '.brain' / 'patterns.md').read_text(encoding='utf-8')
                self.assertEqual(patterns.last_scan(ledger), start + dt.timedelta(days=8))

                write(vault / 'daily' / f'{start + dt.timedelta(days=9)}.md',
                      '- OBSERVATION: the user said "a phrase from day 9"\n'
                      '- OBSERVATION: the user said "one more phrase on day 9"\n')
                proofs = [{'day': str(start + dt.timedelta(days=offset)),
                           'ref': f'daily/{start + dt.timedelta(days=offset)}.md:1',
                           'quote': f'a phrase from day {offset}'} for offset in (7, 8, 9)]
                reply = json.dumps([{'title': 'Daily phrase', 'area': 'work', 'kind': 'pattern',
                                     'behavior': 'Says a phrase every day.', 'evidence': proofs}])
                self.assertEqual(patterns.run(cfg, today=start + dt.timedelta(days=10)), 0)
            later = dt.datetime.combine(start + dt.timedelta(days=10), dt.time(12)).astimezone()
            write(vault / 'daily' / f'{start + dt.timedelta(days=10)}.md',
                  '- OBSERVATION: the user said "a phrase from day 10"\n')
            status, detail = doctor.check_patterns(vault, later)
            self.assertEqual(status, doctor.OK, detail)
            self.assertIn('1 of the last 7 pattern runs added a pattern or evidence', detail)


if __name__ == '__main__':
    unittest.main()
