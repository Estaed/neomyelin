"""The brain's alarms reach the session start: failed nightly steps, reaction debt, health check
titles; alarms survive the five-line cap; a missed night is caught up; a handled correction stops
the doctor warning."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import COMPANION, make_vault, receipt, task, write

import doctor
import memory_context
import nightly


def local(*parts: int) -> dt.datetime:
    return dt.datetime(*parts).astimezone()


class NightlyStepTests(unittest.TestCase):
    def test_reactions_runs_right_after_habits(self):
        names = [name for name, _ in nightly.STEPS]
        self.assertEqual(names[names.index('habits') + 1], 'reactions')
        self.assertEqual(dict(nightly.STEPS)['reactions'], ('reactions.py',))
        self.assertTrue((Path(nightly.__file__).parent / 'reactions.py').is_file())


class AlarmLineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault, self.cfg = make_vault(Path(temporary.name))
        self.companion = self.vault / COMPANION
        self.state = self.vault / '.brain' / '.state'
        self.today = dt.date(2026, 10, 10)

    def log(self, *lines: str) -> None:
        write(self.state / 'nightly.log', '\n'.join(lines) + '\n')

    def debt(self, *quotes: str) -> None:
        write(self.state / 'reactions.json', json.dumps(
            {'last': '2026-10-09T21:00:00+00:00', 'closed': {},
             'open': [{'id': f'id{n}', 'quote': quote} for n, quote in enumerate(quotes)]}))

    # --- the nightly line ------------------------------------------------------------------

    def test_the_nightly_line_reads_only_the_last_run(self):
        older = ('2026-10-08T21:00:01+00:00 [START] nightly',
                 '2026-10-08T21:00:05+00:00 [ERROR] recall: index locked',
                 '2026-10-08T21:00:09+00:00 [DONE WITH ERRORS] nightly: 1 failed')
        self.log(*older,
                 '2026-10-09T21:00:01+00:00 [START] nightly',
                 '2026-10-09T21:00:02+00:00 [OK] habits: done',
                 '2026-10-09T21:00:03+00:00 [ERROR] reactions: model call failed: timeout',
                 '2026-10-09T21:00:04+00:00 [SKIP] evolution: patterns.py is not installed',
                 '2026-10-09T21:00:05+00:00 [ERROR] daily-commit: exit 1',
                 '2026-10-09T21:00:06+00:00 [ERROR] reactions: a second line for the same step',
                 '2026-10-09T21:00:09+00:00 [DONE WITH ERRORS] nightly: 3 failed',
                 '2026-10-09T22:00:00+00:00 [SKIP] nightly: lock exists: x')
        self.assertEqual(memory_context._nightly_line(self.vault),
                         'Nightly run: failed step(s) reactions, daily-commit (.brain/.state/nightly.log)')
        self.log(*older,
                 '2026-10-09T21:00:01+00:00 [START] nightly',
                 '2026-10-09T21:00:02+00:00 [OK] habits: done',
                 '2026-10-09T21:00:09+00:00 [DONE] nightly: 0 failed')
        self.assertEqual(memory_context._nightly_line(self.vault), '')

    def test_no_or_unreadable_log_is_no_line(self):
        self.assertEqual(memory_context._nightly_line(self.vault), '')
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'nightly.log').write_bytes(b'\xff\xfe [ERROR] recall: x\n')
        self.assertEqual(memory_context._nightly_line(self.vault), '')

    # --- the reaction debt line ------------------------------------------------------------

    def test_reaction_debt_counts_and_shortens_the_first_quote(self):
        self.debt('a' * 30, 'second quote')
        self.assertEqual(memory_context._reaction_debt_line(self.vault),
                         f'- [reaction debt] 2 praise/objection(s) not written down ("{"a" * 30}") — '
                         'python .brain/scripts/reactions.py list')
        self.debt('word ' * 20)
        line = memory_context._reaction_debt_line(self.vault)
        self.assertIn('1 praise/objection(s)', line)
        self.assertIn('…") —', line)
        self.assertLessEqual(len(line), 160)

    def test_no_debt_or_a_broken_state_is_no_line(self):
        self.assertEqual(memory_context._reaction_debt_line(self.vault), '')
        for raw in ('{"open": []}', '{"last": "2026-10-09T21:00:00+00:00"}', '{not json', '[]',
                    '{"open": "x"}', '{"open": ["x"]}'):
            with self.subTest(raw):
                write(self.state / 'reactions.json', raw)
                line = memory_context._reaction_debt_line(self.vault)
                if raw == '{"open": ["x"]}':
                    self.assertIn('("?")', line)
                else:
                    self.assertEqual(line, '')

    # --- health titles ---------------------------------------------------------------------

    def test_the_health_line_names_the_check_titles(self):
        write(self.state / 'doctor.json', json.dumps({
            'error': 'Hooks: claude has no user-level hooks ' + 'x' * 200,
            'warnings': ['Recall index: missing', 'Git: 3 unpushed'],
            'titles': ['Hooks', 'Recall index', 'Git']}))
        self.assertEqual(memory_context._health_lines(self.vault, self.today),
                         ['Health check: Hooks, Recall index, Git (python .brain/scripts/doctor.py)'])

    def test_an_old_doctor_json_without_titles_still_works(self):
        write(self.state / 'doctor.json', json.dumps({'error': '', 'warnings': ['Recall index: missing']}))
        self.assertEqual(memory_context._health_lines(self.vault, self.today),
                         ['Health check: 1 warning(s), first: Recall index: missing (python .brain/scripts/doctor.py)'])
        write(self.state / 'doctor.json', json.dumps({'error': 'Hooks: broken', 'warnings': [], 'titles': []}))
        self.assertEqual(memory_context._health_lines(self.vault, self.today), ['Health check error: Hooks: broken'])
        write(self.state / 'doctor.json', json.dumps({'error': '', 'warnings': [], 'titles': 'Hooks'}))
        self.assertEqual(memory_context._health_lines(self.vault, self.today), [])

    def test_doctor_save_writes_the_titles_of_failing_checks(self):
        checks = [doctor.Check('Hooks', doctor.ERROR, 'claude: missing'),
                  doctor.Check('Daily', doctor.OK, 'fine'),
                  doctor.Check('Recall index', doctor.WARNING, 'missing')]
        doctor.write_health(self.vault, checks)
        state = json.loads((self.state / 'doctor.json').read_text(encoding='utf-8'))
        self.assertEqual(state['titles'], ['Hooks', 'Recall index'])
        self.assertEqual(state['warnings'], ['Recall index: missing'])
        self.assertEqual(state['error'], 'Hooks: claude: missing')
        doctor.write_health(self.vault, [doctor.Check('Daily', doctor.OK, 'fine')])
        state = json.loads((self.state / 'doctor.json').read_text(encoding='utf-8'))
        self.assertEqual(state['titles'], [])
        self.assertEqual(memory_context._health_lines(self.vault, self.today), [])

    # --- the cap ---------------------------------------------------------------------------

    def alarms(self) -> list[str]:
        write(self.state / 'doctor.json', json.dumps({'error': 'Hooks: x', 'warnings': [], 'titles': ['Hooks']}))
        write(self.state / 'health.json', json.dumps({'ts': int(local(2026, 10, 10, 1, 0).timestamp()),
                                                      'component': 'stage', 'error': 'model CLI unavailable'}))
        self.log('2026-10-09T21:00:01+00:00 [START] nightly',
                 '2026-10-09T21:00:03+00:00 [ERROR] recall: exit 1',
                 '2026-10-09T21:00:09+00:00 [DONE WITH ERRORS] nightly: 1 failed')
        self.debt('never rename my files without asking')
        write(self.companion / 'Evolution.md', '# Evolution\n\n## Candidates\n\n### Candidate: shorter\n'
                                               '- Decision: pending\n')
        write(self.state / 'knowledge-audit.json', json.dumps({'status': 'updated', 'gaps': [{'id': 'r:1'}]}))
        return ['Health: stage error', 'Health check: Hooks', 'Nightly run:', '[reaction debt]',
                'Evolution:', '[knowledge debt]']

    def due_tasks(self, count: int) -> None:
        for n in range(count):
            task(self.vault, f'due-{n}', title=f'Due task {n}', status='active', project='Garden',
                 due_at=f'2026-10-0{n % 9 + 1}')

    def test_alarms_come_first_and_survive_the_cap_with_many_due_tasks(self):
        expected = self.alarms()
        self.due_tasks(7)
        warnings: list[str] = []
        lines = memory_context._reminders(self.vault, self.today, 'Garden', False, self.companion,
                                          warnings=warnings).splitlines()
        self.assertEqual(warnings, [])
        self.assertEqual(len(lines), len(expected))
        for line, start in zip(lines, expected):
            self.assertIn(start, line)
        self.assertFalse([line for line in lines if 'Due task' in line])

    def test_few_alarms_leave_the_rest_of_the_five_lines_to_tasks(self):
        self.debt('never rename my files without asking')
        self.due_tasks(7)
        lines = memory_context._reminders(self.vault, self.today, 'Garden', False, self.companion).splitlines()
        self.assertEqual(len(lines), 5)
        self.assertIn('[reaction debt]', lines[0])
        self.assertTrue(all('Due task' in line for line in lines[1:]))

    def test_in_the_vault_alarms_survive_and_hygiene_keeps_its_place(self):
        expected = self.alarms()
        self.due_tasks(7)
        write(self.vault / 'knowledge' / 'index.md', '# Index\n')
        write(self.vault / 'knowledge' / 'concepts' / 'lonely-note.md', '# Lonely\n')
        lines = memory_context._reminders(self.vault, self.today, 'vault', True, self.companion,
                                          cards=[], projects=None).splitlines()
        self.assertEqual(len(lines), len(expected) + 1)
        for line, start in zip(lines, expected):
            self.assertIn(start, line)
        self.assertIn('[hygiene]', lines[-1])


class CatchUpTests(unittest.TestCase):
    """nightly_at 21:00. Due when the last run is older than the latest 21:00; no stamp waits."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.vault, self.cfg = make_vault(base)
        self.cfg['nightly_at'] = '21:00'
        self.scripts = base / 'scripts'
        write(self.scripts / 'nightly.py', '# nightly\n')
        self.state = self.vault / '.brain' / '.state'
        self.state.mkdir(parents=True)

    def due(self, now: dt.datetime, stamp: str | None) -> bool:
        (self.state / 'nightly.starting').unlink(missing_ok=True)
        path = self.state / 'nightly.last-run'
        if stamp is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(stamp + '\n', encoding='utf-8')
        warnings: list[str] = []
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(memory_context, 'SCRIPT_DIR', self.scripts), \
                mock.patch.object(memory_context, 'local_now', return_value=now), \
                mock.patch.object(memory_context.subprocess, 'Popen') as spawn:
            memory_context._start_nightly(self.cfg, warnings)
        self.assertEqual(warnings, [])
        return spawn.called

    def test_catch_up_timing(self):
        morning, evening = local(2026, 10, 10, 8, 0), local(2026, 10, 10, 21, 5)
        cases = [
            ('no stamp, morning: waits for nightly_at', morning, None, False),
            ('no stamp, after nightly_at', evening, None, True),
            ('no stamp, one minute before', local(2026, 10, 10, 20, 59), None, False),
            ('stamp yesterday before 21:00, morning: the missed night', morning,
             local(2026, 10, 9, 20, 30).isoformat(), True),
            ('stamp after the last 21:00, morning', morning, local(2026, 10, 9, 21, 30).isoformat(), False),
            ('stamp after the last 21:00, just before tonight', local(2026, 10, 10, 20, 59),
             local(2026, 10, 9, 21, 30).isoformat(), False),
            ('a morning catch-up, then tonight', evening, local(2026, 10, 10, 8, 5).isoformat(), True),
            ('tonight already ran', local(2026, 10, 10, 23, 0), local(2026, 10, 10, 21, 10).isoformat(), False),
            ('exactly at nightly_at', local(2026, 10, 10, 21, 0), local(2026, 10, 9, 21, 30).isoformat(), True),
            ('two nights missed', morning, local(2026, 10, 8, 21, 30).isoformat(), True),
            ('a stamp without a zone, after the last 21:00', morning, '2026-10-09T21:30:00', False),
            ('a stamp without a zone, before it', morning, '2026-10-09T20:30:00', True),
            ('an unreadable stamp, morning', morning, 'yesterday', False),
            ('an unreadable stamp, evening', evening, 'yesterday', True),
        ]
        for name, now, stamp, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.due(now, stamp), expected)

    def test_a_lock_or_a_fresh_start_marker_blocks_the_catch_up(self):
        morning = local(2026, 10, 10, 8, 0)
        stamp = local(2026, 10, 9, 20, 30).isoformat()
        (self.state / 'nightly.lock').write_text('1\n', encoding='utf-8')
        self.assertFalse(self.due(morning, stamp))
        (self.state / 'nightly.lock').unlink()
        self.assertTrue(self.due(morning, stamp))


class HandledCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = local(2026, 10, 10, 12, 0)
        created = local(2026, 10, 9, 12, 0).isoformat()
        receipt(self.root, 'a1b2c3', '- CORRECTION: "do not guess the path"', event_id='2026-10-09-garden-paths',
                created_at=created)
        self.created = created
        # Outside git: the Rules check cannot run, so an open correction is a WARNING.
        run = mock.patch.object(doctor, '_run', return_value=mock.Mock(returncode=128, stdout='', stderr=''))
        run.start()
        self.addCleanup(run.stop)

    def check(self, line: str | None) -> str:
        if line is not None:
            receipt(self.root, 'later', f'Work.\n{line}', event_id='2026-10-10-garden-later',
                    created_at=self.created)
        return doctor.check_corrections(self.root, self.now)[0]

    def test_handled_by_event_id_or_file_name(self):
        for line in ('CORRECTION HANDLED: 2026-10-09-garden-paths -> Rules',
                     '- CORRECTION HANDLED: 2026-10-09-garden-paths -> hook receipt_gate',
                     '* CORRECTION HANDLED: 2026-10-09-garden-paths → skill:paths',
                     'CORRECTION HANDLED: 2026-10-09-garden-paths->Rules',
                     'CORRECTION HANDLED: a1b2c3 -> tasks/paths.md',
                     'CORRECTION HANDLED: a1b2c3.md -> Rules'):
            with self.subTest(line):
                self.assertEqual(self.check(line), doctor.OK)

    def test_unhandled_or_malformed_markers_keep_the_warning(self):
        for line in (None,
                     'CORRECTION HANDLED: 2026-10-09-garden-paths',
                     'CORRECTION HANDLED: 2026-10-09-garden-paths ->',
                     'CORRECTION HANDLED: 2026-10-09-garden-paths ->   ',
                     'CORRECTION HANDLED: -> Rules',
                     'CORRECTION HANDLED: 2026-10-09-garden-other -> Rules',
                     'The note says CORRECTION HANDLED: 2026-10-09-garden-paths -> Rules'):
            with self.subTest(line):
                self.assertEqual(self.check(line), doctor.WARNING)

    def test_a_marker_in_the_same_receipt_closes_it_and_is_no_correction_itself(self):
        receipt(self.root, 'a1b2c3', '- CORRECTION: "do not guess the path"\n'
                                     'CORRECTION HANDLED: a1b2c3 -> hook', event_id='2026-10-09-garden-paths',
                created_at=self.created)
        self.assertEqual(self.check(None), doctor.OK)
        (self.root / 'receipts' / 'a1b2c3.md').unlink()
        receipt(self.root, 'only', 'CORRECTION HANDLED: something-else -> Rules', event_id='x',
                created_at=self.created)
        self.assertEqual(doctor.check_corrections(self.root, self.now),
                         (doctor.OK, 'no recent corrections in receipts'))


if __name__ == '__main__':
    unittest.main()
