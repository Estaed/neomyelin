"""gardener: a proposal still pending 30 days after its last activity is closed as reject.

The pure function runs on a fixed date; the `candidates` command runs on a temporary
Evolution.md with dates relative to the real today. No embedder, no real vault.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, receipt

import gardener
import knowledge_audit

TODAY = dt.date(2026, 10, 10)


def candidate(fp: str, last: str, decision: str = 'pending', type_: str | None = None,
              note: str | None = None) -> str:
    """A block in the gardener's own format; `last` is the newest case's day."""
    block = gardener._candidate(fp, [
        {'created_at': '2025-12-01T09:00:00+00:00', 'category': 'workflow',
         'symptom': 'a friction', 'workaround': 'a workaround', 'fingerprint': fp},
        {'created_at': f'{last}T10:26:34+09:30', 'category': 'workflow',
         'symptom': 'a friction', 'workaround': 'a workaround', 'fingerprint': fp}])
    if decision != 'pending':
        block = block.replace('- Decision: pending', f'- Decision: {decision}')
    if type_ is not None:
        block = block.replace(block.splitlines()[2], f'- Type: {type_}')
    if note is not None:
        block = block.replace(block.splitlines()[3], f'- Decision note: {note}')
    return block + '\n'


def idea(marker: str, source: str = '', decision: str = 'pending') -> str:
    """A block in the knowledge audit's format; it has no date, the gardener records when it first saw it."""
    lines = [f'### Idea {marker}', f'- Decision: {decision}', '- Proposal: try something',
             '- Reason: a learning pointed at it']
    if source:
        lines.append(f'- Source: {source}')
    return '\n'.join(lines) + '\n\n'


def block(text: str, heading: str) -> str:
    start = text.index(heading)
    ends = [index for index in (text.find('\n### ', start + 1), text.find('\n## ', start + 1))
            if index != -1]
    return text[start:min(ends) if ends else len(text)]


class CloseStaleTests(unittest.TestCase):
    def close(self, text: str, seen: dict[str, str] | None = None) -> tuple[str, list[str], list[str]]:
        return gardener._close_stale(text, TODAY, seen)

    def test_exactly_30_days_closes_with_a_dated_reason(self):
        text = '## Candidates\n\n' + candidate('a' * 12, '2026-09-10')
        new, closed, undated = self.close(text)
        self.assertEqual((closed, undated), (['Candidate ' + 'a' * 12], []))
        found = block(new, '### Candidate')
        self.assertIn('- Decision: reject\n', found)
        self.assertIn('- Type: reject\n', found)
        self.assertIn('- Decision note: 2026-10-10 - no decision within 30 days of its last activity '
                      'on 2026-09-10; closed as reject by the gardener\n', found)
        self.assertNotIn('pending', found)
        self.assertNotIn('TODO(agent)', found)
        self.assertIn('(skill-gardener: ' + 'a' * 12, found)  # the block is kept

    def test_29_days_is_untouched(self):
        text = '## Candidates\n\n' + candidate('b' * 12, '2026-09-11')
        self.assertEqual(self.close(text), (text, [], []))

    def test_an_idea_is_dated_by_the_day_it_was_first_seen_30_days_closes_29_does_not(self):
        seen = {'Idea ' + '1' * 12: '2026-09-10', 'Idea ' + '2' * 12: '2026-09-11'}
        text = '## Candidates\n\n' + idea('1' * 12, 'receipts/old.md') + idea('2' * 12, 'receipts/young.md')
        new, closed, undated = self.close(text, seen)
        self.assertEqual((closed, undated), (['Idea ' + '1' * 12], []))
        self.assertIn('- Decision: reject', block(new, '### Idea ' + '1' * 12))
        self.assertIn('last activity on 2026-09-10', block(new, '### Idea ' + '1' * 12))
        self.assertEqual(block(new, '### Idea ' + '2' * 12), block(text, '### Idea ' + '2' * 12))
        self.assertEqual(seen, {'Idea ' + '2' * 12: '2026-09-11'})  # the closed idea leaves the record

    def test_a_new_idea_starts_its_30_days_today_however_old_its_receipt(self):
        # The audit carried the item for weeks before filing it as an idea: the receipt's own
        # day (in its name or its record) is not when the idea appeared, so it is never read.
        text = idea('3' * 12, 'receipts/2026-08-01-carried.md')
        seen: dict[str, str] = {}
        self.assertEqual(self.close(text, seen), (text, [], []))
        self.assertEqual(seen, {'Idea ' + '3' * 12: '2026-10-10'})
        later = gardener._close_stale(text, TODAY + dt.timedelta(days=29), seen)
        self.assertEqual(later, (text, [], []))
        new, closed, _ = gardener._close_stale(text, TODAY + dt.timedelta(days=30), seen)
        self.assertEqual(closed, ['Idea ' + '3' * 12])
        self.assertIn('last activity on 2026-10-10', new)
        self.assertEqual(seen, {})

    def test_decided_blocks_are_untouched_even_when_old(self):
        text = (candidate('d' * 12, '2026-01-01', decision='tool', type_='tool',
                          note='2026-02-01 - a tool was written')
                + candidate('e' * 12, '2026-01-01', decision='reject', type_='reject',
                            note='2026-02-01 - rejected by hand')
                + idea('4' * 12, 'receipts/decided.md', decision='placed'))
        seen = {'Idea ' + '4' * 12: '2026-01-01'}
        self.assertEqual(self.close(text, seen), (text, [], []))
        self.assertEqual(seen, {})  # a decided idea leaves the record

    def test_a_pending_block_with_no_readable_date_is_left_alone_and_reported(self):
        text = ('### Candidate ffffffffffff\n- Decision: pending\n- Type: TODO\n- Decision note: TODO\n\n'
                + idea('5' * 12)                                  # no source line: not an audit idea
                + '### Candidate written-by-hand\n- Decision: pending\n\n')
        seen: dict[str, str] = {}
        new, closed, undated = self.close(text, seen)
        self.assertEqual(new, text)
        self.assertEqual(closed, [])
        self.assertEqual(undated, ['Candidate ffffffffffff', 'Idea ' + '5' * 12, 'Candidate written-by-hand'])
        self.assertEqual(seen, {})
        # With no record kept at all, an audit idea has no date either.
        audit_idea = idea('6' * 12, 'receipts/source.md')
        self.assertEqual(self.close(audit_idea), (audit_idea, [], ['Idea ' + '6' * 12]))

    def test_invalid_dates_are_unknown_age_not_a_crash(self):
        text = candidate('1' * 12, '2026-02-30') + idea('7' * 12, 'receipts/bad.md')
        seen = {'Idea ' + '7' * 12: 'yesterday morning'}
        new, closed, undated = self.close(text, seen)
        self.assertEqual((new, closed, undated), (text, [], ['Candidate ' + '1' * 12]))
        # An unreadable recorded day restarts the idea's clock: it can close late, never early.
        self.assertEqual(seen, {'Idea ' + '7' * 12: '2026-10-10'})

    def test_a_decision_key_in_another_case_is_closed_once(self):
        text = ('### Candidate 1234567890ab\n- decision: Pending\n- type: TODO\n'
                '- decision note: TODO(agent) - dated decision and location\n'
                '  *(skill-gardener: 1234567890ab, 2 cases, 2026-08-01)*\n')
        new, closed, _ = self.close(text)
        self.assertEqual(closed, ['Candidate 1234567890ab'])
        self.assertEqual(new.splitlines()[1:4], [
            '- Decision: reject', '- Type: reject',
            '- Decision note: 2026-10-10 - no decision within 30 days of its last activity on 2026-08-01; '
            'closed as reject by the gardener'])
        # Later nights find nothing pending: no second note, no false "closed" line.
        for days in (1, 2):
            self.assertEqual(gardener._close_stale(new, TODAY + dt.timedelta(days=days)), (new, [], []))

    def test_a_future_date_is_untouched(self):
        text = candidate('2' * 12, '2026-11-01')
        self.assertEqual(self.close(text), (text, [], []))

    def test_only_three_lines_change_and_the_line_count_is_kept(self):
        text = ('# Evolution\n\nIntro paragraph.\n\n## Candidates\n\n'
                + candidate('3' * 12, '2026-08-01')
                + candidate('4' * 12, '2026-10-05')
                + '## Notes\n\nWritten by hand.\n- Type: mine\n- Decision: pending\n- Decision note: mine\n')
        new, closed, _ = self.close(text)
        self.assertEqual(closed, ['Candidate ' + '3' * 12])
        old_lines, new_lines = text.splitlines(), new.splitlines()
        self.assertEqual(len(old_lines), len(new_lines))
        self.assertEqual(len([1 for a, b in zip(old_lines, new_lines) if a != b]), 3)
        # Lines under a later heading are outside every proposal.
        self.assertTrue(new.endswith('- Type: mine\n- Decision: pending\n- Decision note: mine\n'))
        self.assertIn('- Decision: pending', block(new, '### Candidate ' + '4' * 12))

    def test_a_block_without_a_decision_note_gets_one(self):
        # Each case: the block, and the index of the line the note must follow.
        cases = {
            'after the type': ('### Candidate 5555555555aa\n- Decision: pending\n- Type: TODO\n'
                               '  *(skill-gardener: 5555555555aa, 2 cases, 2026-08-01)*\n', 2),
            'after the decision': ('### Candidate 5555555555bb\n- Decision: pending\n'
                                   '  *(skill-gardener: 5555555555bb, 2 cases, 2026-08-01)*\n', 1),
            'after a last line with no newline': (
                '### Candidate 5555555555cc\n  *(skill-gardener: 5555555555cc, 2 cases, 2026-08-01)*\n'
                '- Decision: pending', 2),
        }
        for label, (text, at) in cases.items():
            with self.subTest(label):
                new, closed, _ = self.close(text)
                self.assertEqual(len(closed), 1)
                lines = new.splitlines()
                self.assertEqual(len(lines), len(text.splitlines()) + 1)
                self.assertTrue(lines[at + 1].startswith('- Decision note: 2026-10-10 - no decision within 30 days'),
                                lines)
                self.assertNotIn('pending', ''.join(lines[:at + 1]))

    def test_a_decision_note_the_user_wrote_is_kept_and_the_gardeners_follows_it(self):
        written = '2026-08-20 - waiting to see if it repeats'
        mine = f'- Decision note: {written}'
        cases = {
            'in the template order': candidate('9' * 12, '2026-08-01', note=written),
            'in crlf': candidate('9' * 12, '2026-08-01', note=written).replace('\n', '\r\n'),
            'as a last line with no newline': (
                '### Candidate 9999999999dd\n- Decision: pending\n'
                '  *(skill-gardener: 9999999999dd, 2 cases, 2026-08-01)*\n' + mine),
        }
        for label, text in cases.items():
            with self.subTest(label):
                new, closed, _ = self.close(text)
                self.assertEqual(len(closed), 1)
                self.assertNotIn('pending', new)
                lines = new.splitlines()
                self.assertEqual(len(lines), len(text.splitlines()) + 1)
                at = lines.index(mine)
                self.assertTrue(lines[at + 1].startswith('- Decision note: 2026-10-10 - no decision within 30 days'),
                                lines)
                self.assertEqual(new.count('\r\n'), new.count('\n') if '\r\n' in text else 0)
                self.assertEqual(self.close(new), (new, [], []))

    def test_closing_is_idempotent(self):
        text = candidate('6' * 12, '2026-08-01') + idea('8' * 12, 'receipts/once.md')
        seen = {'Idea ' + '8' * 12: '2026-08-01'}
        once, closed, _ = self.close(text, seen)
        self.assertEqual(len(closed), 2)
        self.assertEqual(self.close(once, seen), (once, [], []))

    def test_crlf_lines_keep_crlf(self):
        text = (candidate('7' * 12, '2026-08-01') + candidate('8' * 12, '2026-10-09')).replace('\n', '\r\n')
        new, closed, _ = self.close(text)
        self.assertEqual(closed, ['Candidate ' + '7' * 12])
        self.assertEqual(new.count('\r\n'), text.count('\r\n'))
        self.assertEqual(new.count('\n'), text.count('\n'))
        self.assertIn('- Decision: reject\r\n', new)
        self.assertEqual(new.count('- Decision: pending'), 1)


class CandidatesCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault, self.cfg = make_vault(Path(temporary.name))
        self.cfg['companion_dir'] = 'companion'
        self.evolution = self.vault / 'companion' / 'Evolution.md'
        today = dt.date.today()
        self.old = (today - dt.timedelta(days=45)).isoformat()
        self.boundary = (today - dt.timedelta(days=30)).isoformat()
        self.young = (today - dt.timedelta(days=29)).isoformat()
        self.first_seen = (today - dt.timedelta(days=31)).isoformat()
        self.seen = self.vault / '.brain' / '.state' / 'evolution-ideas.json'
        # The idea's receipt is older than 30 days, as when the audit carried the item for weeks;
        # only the day the gardener first saw the idea counts.
        stamp = (dt.datetime.combine(today - dt.timedelta(days=45), dt.time(12)).astimezone()
                 .isoformat(timespec='seconds'))
        receipt(self.vault, 'idea-source', '[Test] a session', created_at=stamp)
        self.text = ('# Evolution\n\nIntro.\n\n## Candidates\n\nA paragraph.\n\n'
                     + candidate('a1' * 6, self.old)
                     + candidate('b2' * 6, self.boundary)
                     + candidate('c3' * 6, self.young)
                     + candidate('d4' * 6, self.old, decision='rule', type_='rule',
                                 note='2026-01-01 - rule written')
                     + '### Candidate written-by-hand\n- Decision: pending\n')
        for name, value in (('load', mock.patch.object(gardener.config, 'load', return_value=self.cfg)),
                            ('groups', mock.patch.object(gardener, '_semantic_groups', gardener._groups))):
            value.start()
            self.addCleanup(value.stop)

    def write(self, text: str, seen: bool = True) -> None:
        """Evolution.md plus the audit's idea; with `seen`, the gardener first saw it 31 days ago."""
        self.evolution.parent.mkdir(parents=True, exist_ok=True)
        self.evolution.write_bytes(text.encode('utf-8'))
        # The idea comes from the knowledge audit's own writer, in its own format.
        knowledge_audit._write_ideas(self.evolution, [{
            'id': 'idea-source:1', 'proposal': 'check it first', 'reason': 'it went wrong twice',
            'receipt': 'receipts/idea-source.md'}])
        self.idea = next(line for line in self.evolution.read_text(encoding='utf-8').splitlines()
                         if line.startswith('### Idea '))
        if seen:
            self.seen.parent.mkdir(parents=True, exist_ok=True)
            self.seen.write_text(json.dumps({self.idea.removeprefix('### '): self.first_seen}),
                                 encoding='utf-8')

    def recorded(self) -> dict[str, str]:
        return json.loads(self.seen.read_text(encoding='utf-8'))

    def run_command(self, *extra: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = gardener.main(['candidates', *extra])
        return code, output.getvalue()

    def test_a_dry_run_reports_and_closes_nothing(self):
        self.write(self.text)
        before, record = self.evolution.read_bytes(), self.seen.read_bytes()
        code, output = self.run_command('--dry-run')
        self.assertEqual(code, 0)
        self.assertEqual(self.evolution.read_bytes(), before)
        self.assertEqual(self.seen.read_bytes(), record)
        self.assertIn('3 proposal(s) with no decision within 30 days of their last activity would be closed '
                      'as reject (dry run, nothing closed)', output)
        self.assertIn('dry run: no file written', output)

    def test_an_idea_filed_from_an_old_receipt_gets_30_days_from_first_sight(self):
        self.write(self.text, seen=False)  # the audit filed the idea last night
        code, output = self.run_command('--dry-run')
        self.assertEqual(code, 0)
        self.assertFalse(self.seen.exists())  # a dry run records nothing either
        code, output = self.run_command()
        self.assertEqual(code, 0, output)
        self.assertIn('2 proposal(s) with no decision within 30 days', output)
        self.assertIn('- Decision: pending\n', block(self.evolution.read_text(encoding='utf-8'), self.idea))
        self.assertEqual(self.recorded(), {self.idea.removeprefix('### '): dt.date.today().isoformat()})

    def test_closes_old_and_boundary_keeps_young_decided_and_undated(self):
        self.write(self.text)
        before = self.evolution.read_text(encoding='utf-8')
        code, output = self.run_command()
        self.assertEqual(code, 0, output)
        self.assertIn('3 proposal(s) with no decision within 30 days of their last activity closed as reject',
                      output)
        self.assertIn('1 pending proposal(s) have no readable date and stay open: '
                      'Candidate written-by-hand', output)
        new = self.evolution.read_text(encoding='utf-8')
        self.assertIn('- Decision: reject\n', block(new, '### Candidate ' + 'a1' * 6))
        self.assertIn('- Decision: reject\n', block(new, '### Candidate ' + 'b2' * 6))
        self.assertIn('- Decision: reject\n', block(new, self.idea))
        self.assertIn('- Decision: pending\n', block(new, '### Candidate ' + 'c3' * 6))
        self.assertIn('- Decision: pending\n', block(new, '### Candidate written-by-hand'))
        self.assertEqual(block(new, '### Candidate ' + 'd4' * 6), block(before, '### Candidate ' + 'd4' * 6))
        # The idea had no decision note line: it gains one, the rest keep their line count.
        self.assertEqual(len(new.splitlines()), len(before.splitlines()) + 1)
        self.assertEqual(self.recorded(), {})  # the closed idea leaves the record

    def test_a_second_run_changes_nothing(self):
        self.write(self.text)
        self.run_command()
        first, record = self.evolution.read_bytes(), self.seen.read_bytes()
        code, output = self.run_command()
        self.assertEqual(code, 0)
        self.assertEqual(self.evolution.read_bytes(), first)
        self.assertEqual(self.seen.read_bytes(), record)
        self.assertIn('no new evolution candidates or correction results', output)

    def test_a_dry_run_with_nothing_to_close_records_nothing(self):
        self.write('# Evolution\n\n## Candidates\n\n' + candidate('c3' * 6, self.young), seen=False)
        code, output = self.run_command('--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('no new evolution candidates or correction results', output)
        self.assertFalse(self.seen.exists())

    def test_another_evolution_file_leaves_the_vaults_record_alone(self):
        self.write(self.text)
        record = self.seen.read_bytes()
        other = self.vault.parent / 'other' / 'Evolution.md'
        other.parent.mkdir()
        other.write_text('# Evolution\n\n## Candidates\n\n' + candidate('e5' * 6, self.old)
                         + idea('9' * 12, 'receipts/idea-source.md'), encoding='utf-8')
        for _ in range(2):  # the first run closes a candidate, the second has nothing to do
            code, output = self.run_command('--evolution', str(other))
            self.assertEqual(code, 0, output)
            self.assertIn('1 pending proposal(s) have no readable date and stay open: Idea ' + '9' * 12,
                          output)
            self.assertEqual(self.seen.read_bytes(), record)
        new = other.read_text(encoding='utf-8')
        self.assertIn('- Decision: reject\n', block(new, '### Candidate ' + 'e5' * 6))
        self.assertIn('- Decision: pending\n', block(new, '### Idea ' + '9' * 12))
        # The vault's idea kept its first-seen day, so its own run still closes it.
        code, output = self.run_command()
        self.assertEqual(code, 0, output)
        self.assertIn('- Decision: reject\n', block(self.evolution.read_text(encoding='utf-8'), self.idea))

    def test_a_crlf_file_stays_crlf(self):
        self.write(self.text.replace('\n', '\r\n'))
        before = self.evolution.read_bytes()
        code, _ = self.run_command()
        self.assertEqual(code, 0)
        after = self.evolution.read_bytes()
        self.assertEqual(after.count(b'\r\n'), before.count(b'\r\n') + 1)  # plus the idea's note
        self.assertEqual(after.count(b'\n'), after.count(b'\r\n'))
        new = after.decode('utf-8')
        self.assertIn('- Decision: reject\r\n', block(new, '### Candidate ' + 'a1' * 6))
        self.assertIn('- Decision: pending\r\n', block(new, '### Candidate ' + 'c3' * 6))


if __name__ == '__main__':
    unittest.main()
