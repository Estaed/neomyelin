"""Pattern evolution's history window, the pending-day rule for new patterns, and the kept reply.

Temporary vaults only; the model is a stand-in for engine.ask, so the staged call, the raw
reply file and the evidence gate all run for real.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, receipt, write

import evidence
import patterns
import stage

LAST_SCAN = dt.date(2026, 10, 9)
TODAY = dt.date(2026, 10, 11)
FINISHED = TODAY - dt.timedelta(days=1)


def day_file(vault: Path, day: str, quote: str, crlf: bool = False) -> None:
    """Line 3 is the marked observation; the other lines must never become evidence."""
    lines = [f'# {day}', '', f'- OBSERVATION: the user said "{quote}" again.',
             '- Note: "an unmarked quoted line"', 'A plain line.']
    newline = '\r\n' if crlf else '\n'
    path = vault / 'daily' / f'{day}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((newline.join(lines) + newline).encode('utf-8'))


def receipt_file(vault: Path, name: str, day: str, quote: str) -> None:
    receipt(vault, name, f'[Test] session\n- REACTION: pushed back: "{quote}"',
            event_id=name, created_at=f'{day}T12:00:00+00:00')


def ledger_text(scan: str | None = LAST_SCAN.isoformat(), extra: str = '') -> str:
    text = patterns.EMPTY if scan is None else patterns.EMPTY.replace(
        '\n', f'\nLast scan: {scan}\n', 1)
    return text + extra


def names(rows: list[dict]) -> set[str]:
    return {row['ref'].rsplit(':', 1)[0] for row in rows}


class WindowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault, _ = make_vault(Path(temporary.name))
        # Pending: finished days after the last scan.
        day_file(self.vault, '2026-10-10', 'a pending day phrase here')
        receipt_file(self.vault, 'pending-receipt', '2026-10-10', 'a pending receipt phrase')
        # Today is not finished: it waits for tomorrow's run.
        day_file(self.vault, '2026-10-11', 'a phrase from today itself')
        # History: (last scan - 30 days, last scan].
        day_file(self.vault, '2026-10-09', 'the last scan day phrase')
        day_file(self.vault, '2026-09-10', 'the first day of the window')
        receipt_file(self.vault, 'history-receipt', '2026-09-15', 'a history receipt phrase')
        # Outside the window.
        day_file(self.vault, '2026-09-09', 'exactly thirty days before')
        day_file(self.vault, '2026-08-01', 'a very old phrase here')
        write(self.vault / 'daily' / 'notes.md', '- OBSERVATION: "an undated file phrase"\n')

    def window(self, scan=LAST_SCAN.isoformat(), days=patterns.HISTORY_DAYS):
        return patterns.observations(self.vault, ledger_text(scan), FINISHED, days)

    def test_pending_is_after_the_last_scan_through_yesterday_from_both_sources(self):
        pending, _ = self.window()
        self.assertEqual(names(pending), {'daily/2026-10-10.md', 'receipts/pending-receipt.md'})

    def test_history_is_the_thirty_days_up_to_and_including_the_last_scan(self):
        _, history = self.window()
        self.assertEqual(names(history), {'daily/2026-10-09.md', 'daily/2026-09-10.md',
                                          'receipts/history-receipt.md'})
        self.assertNotIn('daily/2026-09-09.md', names(history))

    def test_history_and_pending_are_disjoint_and_contiguous(self):
        pending, history = self.window()
        self.assertFalse(names(pending) & names(history))
        everything = evidence.all_sources(self.vault, through=FINISHED,
                                          after=LAST_SCAN - dt.timedelta(days=30))
        self.assertEqual(names(everything), names(pending) | names(history))

    def test_rows_carry_their_line_and_text_and_skip_unmarked_lines(self):
        _, history = self.window()
        row = next(item for item in history if item['ref'].startswith('daily/2026-10-09.md'))
        self.assertEqual(row['ref'], 'daily/2026-10-09.md:3')
        self.assertEqual(row['line'], '- OBSERVATION: the user said "the last scan day phrase" again.')
        self.assertEqual(len(history), 3)

    def test_a_one_day_window_is_the_last_scan_day_and_zero_is_none(self):
        self.assertEqual(names(self.window(days=1)[1]), {'daily/2026-10-09.md'})
        self.assertEqual(self.window(days=0)[1], [])

    def test_no_last_scan_means_no_history_and_every_finished_day_pending(self):
        pending, history = self.window(scan=None)
        self.assertEqual(history, [])
        self.assertIn('daily/2026-08-01.md', names(pending))
        self.assertNotIn('daily/2026-10-11.md', names(pending))
        self.assertEqual(len(pending), 7)

    def test_an_unreadable_last_scan_date_counts_as_no_scan(self):
        pending, history = self.window(scan='2026-13-45')
        self.assertEqual(history, [])
        self.assertEqual(len(pending), 7)

    def test_a_crlf_day_file_gives_text_without_a_carriage_return(self):
        day_file(self.vault, '2026-10-01', 'a crlf line phrase', crlf=True)
        _, history = self.window()
        row = next(item for item in history if item['ref'].startswith('daily/2026-10-01.md'))
        self.assertEqual(row['ref'], 'daily/2026-10-01.md:3')
        self.assertNotIn('\r', row['line'])


class PromptTests(unittest.TestCase):
    PENDING = [{'day': '2026-10-10', 'ref': 'daily/2026-10-10.md:3', 'line': 'OBSERVATION: "pending"'}]
    HISTORY = [{'day': '2026-09-20', 'ref': 'daily/2026-09-20.md:3', 'line': 'OBSERVATION: "older"'}]
    CFG = {'vault': str(Path(tempfile.gettempdir()) / 'no-such-vault'), 'language': 'English'}

    def test_history_follows_the_pending_lines_and_is_marked_as_context(self):
        text = patterns.prompt(self.PENDING, [], self.CFG, self.HISTORY)
        self.assertIn('Pending observations: [{"day": "2026-10-10", "ref": "daily/2026-10-10.md:3"', text)
        self.assertIn('Earlier observations', text)
        self.assertIn('"ref": "daily/2026-09-20.md:3"', text)
        self.assertLess(text.index('Pending observations:'), text.index('Earlier observations'))
        pending = text[text.index('Pending observations:'):text.index('Earlier observations')]
        self.assertNotIn('2026-09-20', pending)
        self.assertIn('at least one pending observation', text)

    def test_no_history_no_history_section(self):
        text = patterns.prompt(self.PENDING, [], self.CFG)
        self.assertNotIn('Earlier observations', text)
        self.assertNotIn('2026-09-20', text)


class RunTests(unittest.TestCase):
    """A new pattern needs a quote from a pending day; evidence for an existing one does not."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault, self.cfg = make_vault(Path(temporary.name))
        self.ledger, self.personality = stage.paths(self.cfg)
        self.state = self.vault / '.brain' / '.state'
        self.quotes = {
            'daily/2026-09-20.md': 'finish it in one pass now',
            'daily/2026-09-25.md': 'just finish the whole thing',
            'daily/2026-10-01.md': 'one pass and then we are done',
            'daily/2026-10-10.md': 'finish it today in one go',
            'daily/2026-08-01.md': 'an old line outside the window',
        }
        for relative, quote in self.quotes.items():
            day_file(self.vault, Path(relative).stem, quote)
        receipt_file(self.vault, 'older', '2026-09-15', 'no second round of review')
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_bytes(ledger_text().encode('utf-8'))
        self.prompts: list[str] = []
        self.staged: list[set[str]] = []

    def proof(self, name: str) -> dict:
        source = next(item for item in evidence.all_sources(self.vault, through=FINISHED)
                      if item['ref'].startswith(name + ':'))
        return {key: source[key] for key in ('day', 'ref', 'quote')}

    def proposal(self, title: str, *sources: str, kind: str = 'pattern') -> dict:
        return {'title': title, 'area': 'work', 'direction': 'negative', 'kind': kind,
                'behavior': f'{title}: wants the work finished in one pass.',
                'evidence': [self.proof(name) for name in sources]}

    def run_with(self, reply: str | Exception) -> int:
        def answer(prompt, *, cfg, **_kwargs):
            self.prompts.append(prompt)
            work = Path(cfg['vault'])
            self.staged.append({path.relative_to(work).as_posix() for path in work.rglob('*.md')})
            if isinstance(reply, Exception):
                raise reply
            return reply

        with mock.patch.object(stage.engine, 'ask', side_effect=answer):
            return patterns.run(self.cfg, today=TODAY)

    def ledger_now(self) -> str:
        return self.ledger.read_text(encoding='utf-8')

    def last_log(self) -> str:
        return (self.state / patterns.LOG).read_text(encoding='utf-8').splitlines()[-1]

    def kept_reply(self) -> str:
        return (self.state / patterns.LAST_REPLY).read_text(encoding='utf-8')

    def test_a_new_pattern_from_history_alone_is_not_written(self):
        item = self.proposal('Finish fast', 'daily/2026-09-20.md', 'daily/2026-09-25.md',
                             'daily/2026-10-01.md')
        sources = {}
        for source in evidence.all_sources(self.vault, through=FINISHED):
            sources.setdefault(source['ref'], []).append(source)
        # The evidence gate alone would pass it: the drop comes from the pending-day rule.
        self.assertIsNotNone(patterns.candidate(item, sources))
        self.assertEqual(self.run_with(json.dumps([item])), 0)
        self.assertNotIn('## Pattern: Finish fast', self.ledger_now())
        self.assertIn('Patterns: 0 added, 0 evidence updates, 0 rejected', self.last_log())
        self.assertEqual(patterns.last_scan(self.ledger_now()), FINISHED)
        self.assertNotIn('Finish fast', self.personality.read_text(encoding='utf-8'))

    def test_a_new_pattern_mixing_history_and_a_pending_day_is_written(self):
        item = self.proposal('Finish fast', 'daily/2026-09-20.md', 'daily/2026-09-25.md',
                             'daily/2026-10-10.md')
        self.assertEqual(self.run_with(json.dumps([item])), 0)
        self.assertIn('## Pattern: Finish fast', self.ledger_now())
        self.assertIn('`daily/2026-10-10.md:3`', self.ledger_now())
        self.assertIn('Patterns: 1 added', self.last_log())
        self.assertIn('Finish fast: wants the work finished in one pass.',
                      self.personality.read_text(encoding='utf-8'))

    def test_a_history_receipt_counts_toward_the_three_days(self):
        item = self.proposal('Finish fast', 'receipts/older.md', 'daily/2026-09-25.md',
                             'daily/2026-10-10.md')
        self.assertEqual(self.run_with(json.dumps([item])), 0)
        self.assertIn('`receipts/older.md:', self.ledger_now())

    def test_a_pending_day_from_a_receipt_satisfies_the_rule(self):
        (self.vault / 'daily' / '2026-10-10.md').unlink()
        receipt_file(self.vault, 'pending', '2026-10-10', 'stop asking, just ship it')
        item = self.proposal('Finish fast', 'daily/2026-09-20.md', 'daily/2026-09-25.md',
                             'receipts/pending.md')
        self.assertEqual(self.run_with(json.dumps([item])), 0)
        self.assertIn('## Pattern: Finish fast', self.ledger_now())

    def test_of_two_proposals_only_the_one_with_a_pending_quote_is_written(self):
        old = self.proposal('History only', 'daily/2026-09-20.md', 'daily/2026-09-25.md',
                            'daily/2026-10-01.md')
        mixed = self.proposal('Mixed days', 'daily/2026-09-20.md', 'daily/2026-10-01.md',
                              'daily/2026-10-10.md')
        self.assertEqual(self.run_with(json.dumps([old, mixed])), 0)
        self.assertNotIn('## Pattern: History only', self.ledger_now())
        self.assertIn('## Pattern: Mixed days', self.ledger_now())

    def test_evidence_for_an_existing_pattern_may_come_from_history_alone(self):
        existing = patterns.candidate(
            self.proposal('Finish fast', 'receipts/older.md', 'daily/2026-09-20.md',
                          'daily/2026-10-01.md'),
            {source['ref']: [source] for source in evidence.all_sources(self.vault, through=FINISHED)})
        self.ledger.write_bytes(ledger_text(extra='\n' + patterns.block(existing)).encode('utf-8'))
        more = self.proposal('Finish fast', 'daily/2026-09-25.md', kind='evidence')
        self.assertEqual(self.run_with(json.dumps([more])), 0)
        self.assertIn('`daily/2026-09-25.md:3`', self.ledger_now())
        self.assertIn('1 evidence updates', self.last_log())

    def test_the_model_sees_pending_lines_then_only_the_history_window(self):
        self.assertEqual(self.run_with('[]'), 0)
        self.assertEqual(len(self.prompts), 1)
        text = self.prompts[0]
        history = text[text.index('Earlier observations'):]
        for name in ('daily/2026-09-20.md', 'daily/2026-09-25.md', 'daily/2026-10-01.md',
                     'receipts/older.md'):
            self.assertIn(f'"ref": "{name}:', history)
        pending = text[text.index('Pending observations:'):text.index('Earlier observations')]
        self.assertIn('"ref": "daily/2026-10-10.md:3"', pending)
        self.assertNotIn('2026-09-20', pending)
        self.assertNotIn('2026-08-01', text)
        # Only the window's files are copied into the model's stage.
        self.assertIn('sources/daily/2026-10-10.md', self.staged[0])
        self.assertIn('sources/daily/2026-09-20.md', self.staged[0])
        self.assertNotIn('sources/daily/2026-08-01.md', self.staged[0])

    def test_no_pending_observation_means_no_model_call_and_no_reply_file(self):
        self.ledger.write_bytes(ledger_text(scan=FINISHED.isoformat()).encode('utf-8'))
        self.assertEqual(self.run_with('[]'), 0)
        self.assertEqual(self.prompts, [])
        self.assertFalse((self.state / patterns.LAST_REPLY).exists())

    def test_the_raw_reply_is_kept_as_returned(self):
        item = self.proposal('Finish fast', 'daily/2026-09-20.md', 'daily/2026-09-25.md',
                             'daily/2026-10-01.md')
        reply = '```json\n' + json.dumps([item]) + '\n```\n'
        self.assertEqual(self.run_with(reply), 0)
        self.assertEqual(self.kept_reply(), reply)

    def test_a_reply_with_no_candidate_is_kept(self):
        self.assertEqual(self.run_with('[]\n'), 0)
        self.assertEqual(self.kept_reply(), '[]\n')
        self.assertIn('Patterns: 0 added, 0 evidence updates', self.last_log())

    def test_a_reply_that_is_not_json_is_kept_and_the_run_fails_visibly(self):
        reply = 'I looked at the observations and found no pattern.'
        self.assertEqual(self.run_with(reply), 1)
        self.assertEqual(self.kept_reply(), reply)
        self.assertIn('Pattern evolution failed', self.last_log())

    def test_an_empty_reply_replaces_the_previous_runs_reply_with_a_note(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / patterns.LAST_REPLY).write_text('yesterday reply\n', encoding='utf-8')
        self.assertEqual(self.run_with('  \n'), 1)
        self.assertIn('replied with nothing', self.kept_reply())
        self.assertNotIn('yesterday', self.kept_reply())

    def test_a_failed_model_call_leaves_a_note_not_the_previous_reply(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / patterns.LAST_REPLY).write_text('yesterday reply\n', encoding='utf-8')
        self.assertEqual(self.run_with(RuntimeError('model CLI unavailable: claude')), 1)
        self.assertIn('the model call failed: model CLI unavailable', self.kept_reply())
        self.assertIn('Pattern evolution failed', self.last_log())


if __name__ == '__main__':
    unittest.main()
