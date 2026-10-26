"""The personality gate uses source days, exact quotes, and additive writes."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, receipt

import patterns
import evidence
import stage


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault, self.cfg = make_vault(Path(temporary.name))
        self.evidence, self.personality = stage.paths(self.cfg)
        self.manual = self.personality.read_bytes()

    def observation(self, name: str, day: int) -> dict:
        quote = f'Please show the measured result for case {day}'
        receipt(self.vault, name, f'- OBSERVATION: asks for a measurement: "{quote}"',
                event_id=name, created_at=f'2026-09-{day:02d}T10:00:00+09:30')
        return next(item for item in evidence.source_lines(self.vault) if item['quote'] == quote)

    def proposal(self, proofs: list[dict]) -> dict:
        return {'title': 'Check measurements', 'area': 'decisions',
                'behavior': 'Ask for a measured result before deciding.',
                'evidence': [{key: value for key, value in item.items() if key != 'line'}
                             for item in proofs]}

    def test_one_day_cannot_change_personality_or_call_model(self):
        proofs = [self.observation(f'one-{index}', 1) for index in range(3)]
        with mock.patch.object(stage, 'ask_json') as ask:
            self.assertEqual(patterns.run(self.cfg), 0)
        ask.assert_not_called()
        self.assertFalse(self.evidence.exists())
        self.assertEqual(self.personality.read_bytes(), self.manual)
        self.assertEqual(len({proof['day'] for proof in proofs}), 1)

    def test_three_days_add_one_line_and_evidence_without_touching_manual_line(self):
        proofs = [self.observation(f'day-{day}', day) for day in (1, 2, 3)]
        with mock.patch.object(stage, 'ask_json', return_value=[self.proposal(proofs)]):
            self.assertEqual(patterns.run(self.cfg), 0)
            first = self.personality.read_bytes()
            self.assertEqual(patterns.run(self.cfg), 0)
        self.assertEqual(self.personality.read_bytes(), first)
        self.assertTrue(first.startswith(self.manual))
        self.assertEqual(first.count(b'<!-- neomyelin:'), 1)
        ledger = self.evidence.read_text(encoding='utf-8')
        for proof in proofs:
            self.assertIn(f"`{proof['ref']}`", ledger)
        self.assertIn('Status: active', ledger)
        self.assertEqual(patterns.last_scan(ledger), dt.date.today() - dt.timedelta(days=1))

    def test_partial_quote_fails_and_veto_cannot_return(self):
        proofs = [self.observation(f'day-{day}', day) for day in (1, 2, 3)]
        partial = self.proposal(proofs)
        partial['evidence'][0]['quote'] = 'show the measured result'
        with mock.patch.object(stage, 'ask_json', return_value=[partial]):
            self.assertEqual(patterns.run(self.cfg), 0)
        self.assertEqual(self.personality.read_bytes(), self.manual)
        with mock.patch.object(stage, 'ask_json', return_value=[self.proposal(proofs)]):
            self.assertEqual(patterns.run(self.cfg), 0)
            self.assertEqual(patterns.veto('Check measurements', self.cfg), 0)
            self.assertEqual(patterns.run(self.cfg), 0)
        self.assertEqual(self.personality.read_bytes(), self.manual)
        self.assertIn('Status: vetoed', self.evidence.read_text(encoding='utf-8'))

    def test_receipt_day_comes_from_metadata_not_filename(self):
        receipt(self.vault, '2026-09-01-misleading',
                '- REACTION: checks a claim: "Show me the actual result"',
                created_at='2026-09-04T10:00:00+09:30')
        source = evidence.source_lines(self.vault)[0]
        self.assertEqual(source['day'], '2026-09-04')

    def test_failed_personality_write_rolls_back_new_evidence(self):
        proofs = [self.observation(f'day-{day}', day) for day in (1, 2, 3)]
        original_write = stage.atomic_write

        def fail_personality(path, value):
            if path == self.personality:
                raise OSError('simulated write failure')
            original_write(path, value)

        with mock.patch.object(stage, 'ask_json', return_value=[self.proposal(proofs)]), \
                mock.patch.object(stage, 'atomic_write', side_effect=fail_personality):
            with self.assertRaises(OSError):
                patterns.run(self.cfg)
        self.assertFalse(self.evidence.exists())
        self.assertEqual(self.personality.read_bytes(), self.manual)

    def test_sleeping_pattern_collects_evidence_then_wakes(self):
        older = [self.observation(f'a-{day}', day) for day in (1, 2, 3)]
        newer = [self.observation(f'b-{day}', day) for day in (4, 5, 6)]
        second = {'title': 'Second pattern', 'area': 'attitude', 'direction': 'positive',
                  'behavior': 'Keep the useful result visible.',
                  'evidence': [{k: v for k, v in proof.items() if k != 'line'} for proof in newer]}
        with mock.patch.object(stage, 'ask_json', return_value=[self.proposal(older), second]):
            self.assertEqual(patterns.run(self.cfg), 0)
        self.cfg['pattern_budget'] = 1
        text, slept, _ = patterns.rank(self.evidence.read_text(encoding='utf-8'), 1)
        self.assertEqual(len(slept), 1)
        self.assertIn('Direction: positive', text)
        self.assertIn('Area: attitude', text)
        sleeping = next(item for item in patterns.entries(text) if item['status'] == 'sleeping')
        extra = self.observation('extra', 7)
        changed, added = patterns.add_evidence(text, sleeping, [extra])
        self.assertTrue(added)
        self.assertIn(f"`{extra['ref']}`", changed)
        reranked, _, woke = patterns.rank(changed, 1)
        self.assertEqual(woke, [sleeping['title']])
        self.assertEqual(len([item for item in patterns.entries(reranked)
                              if item['status'] == 'active']), 1)

    def test_veto_keeps_reason_counter_evidence_and_fingerprint(self):
        proofs = [self.observation(f'day-{day}', day) for day in (1, 2, 3)]
        with mock.patch.object(stage, 'ask_json', return_value=[self.proposal(proofs)]):
            self.assertEqual(patterns.run(self.cfg), 0)
        self.assertEqual(patterns.veto('Check measurements', self.cfg,
                                      reason='This no longer helps', who='assistant'), 0)
        ledger = self.evidence.read_text(encoding='utf-8')
        self.assertIn('Status: vetoed', ledger)
        self.assertIn('Counter evidence:', ledger)
        self.assertIn('assistant veto: This no longer helps', ledger)
        self.assertIn('Fingerprint:', ledger)
        self.assertNotIn('Check measurements:', self.personality.read_text(encoding='utf-8'))

    def test_counter_evidence_and_stale_state_remain(self):
        proofs = [self.observation(f'day-{day}', day) for day in (1, 2, 3)]
        source = patterns.candidate(self.proposal(proofs),
                                    {proof['ref']: [proof] for proof in proofs})
        text = patterns.block(source).replace('Status: active', 'Status: stale')
        row = patterns.entries(text)[0]
        changed, added = patterns.add_counter_evidence(text, row,
                                                       [self.observation('counter', 4)])
        self.assertTrue(added)
        self.assertIn('Status: stale', patterns.rank(changed, 1)[0])
        self.assertIn('Counter evidence:\n-', changed)

    def test_scan_date_never_advances_to_today(self):
        result = patterns.advance_scan(patterns.EMPTY, dt.date(2026, 9, 30))
        self.assertEqual(patterns.last_scan(result), dt.date(2026, 9, 30))
        self.assertEqual(patterns.advance_scan(result, dt.date(2026, 9, 29)), result)

    def test_dry_run_has_no_model_call_or_write(self):
        proofs = [self.observation(f'day-{day}', day) for day in (1, 2, 3)]
        with mock.patch.object(stage, 'ask_json') as ask:
            self.assertEqual(patterns.run(self.cfg, dry_run=True), 0)
        ask.assert_not_called()
        self.assertFalse(self.evidence.exists())
        self.assertEqual(len(proofs), 3)

    def test_source_edited_during_model_call_is_rejected(self):
        proofs = [self.observation(f'day-{day}', day) for day in (1, 2, 3)]

        def change_source(*_args, **_kwargs):
            path = self.vault / proofs[0]['ref'].rsplit(':', 1)[0]
            path.write_text(path.read_text(encoding='utf-8').replace(
                proofs[0]['quote'], 'An edited source quote with different words'), encoding='utf-8')
            return [self.proposal(proofs)]

        with mock.patch.object(stage, 'ask_json', side_effect=change_source):
            self.assertEqual(patterns.run(self.cfg), 0)
        self.assertEqual(self.personality.read_bytes(), self.manual)
        self.assertFalse(self.evidence.exists())

    def test_binding_rules_include_existing_companion_rules(self):
        companion = self.personality.parent
        (companion / 'Rules.md').write_text('- **Cite the source.** Check each claim.\n',
                                             encoding='utf-8')
        rules = patterns.binding_rules(self.cfg, [])
        self.assertIn('Cite the source.', rules)


class LedgerLineEndingTests(unittest.TestCase):
    """A CRLF, LF or mixed ledger (an editor saved it so) reads and writes the same, and keeps
    its own line ending over two nightly runs."""

    TITLE = 'Check measurements'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault, self.cfg = make_vault(Path(temporary.name))
        self.evidence, _ = stage.paths(self.cfg)
        self.proofs = [self.observation(f'day-{day}', day) for day in (1, 2, 3)]

    def observation(self, name: str, day: int, words: str = 'Please show the measured result for case') -> dict:
        quote = f'{words} {day}'
        receipt(self.vault, name, f'- OBSERVATION: asks for a measurement: "{quote}"',
                event_id=name, created_at=f'2026-09-{day:02d}T10:00:00+09:30')
        return next(item for item in evidence.source_lines(self.vault) if item['quote'] == quote)

    @staticmethod
    def cited(proofs: list[dict]) -> list[dict]:
        return [{key: value for key, value in item.items() if key != 'line'} for item in proofs]

    def ledger(self, kind: str) -> bytes:
        """A ledger scanned through 2026-09-03 holding one active pattern, in `kind` endings. The
        mixed one is the shape the old reader left: a CRLF file whose CRLF `Last scan` line went
        unread, an LF one inserted beside it, and one LF line the user typed."""
        item = patterns.candidate({'title': self.TITLE, 'area': 'decisions',
                                   'behavior': 'Ask for a measured result before deciding.',
                                   'evidence': self.cited(self.proofs)},
                                  {proof['ref']: [proof] for proof in self.proofs})
        text = (patterns.advance_scan(patterns.EMPTY, dt.date(2026, 9, 3)) + '\n'
                + patterns.block(item, today=dt.date(2026, 9, 3)))
        if kind == 'crlf':
            text = text.replace('\n', '\r\n')
        if kind == 'mixed':
            text = text.replace('\n', '\r\n').replace('Last scan: 2026-09-03', 'Last scan: 2026-09-02')
            first = text.find('\n') + 1
            text = text[:first] + 'Last scan: 2026-09-03\n' + text[first:]
            text = text.replace('stored here.\r\n', 'stored here.\n')
        return text.encode('utf-8')

    def night(self, today: dt.date, replies: list[dict]) -> set[str]:
        """One nightly run; returns the days the model was shown as pending."""
        prompts = []

        def ask(text, *_args, **_kwargs):
            prompts.append(text)
            return replies

        with mock.patch.object(stage, 'ask_json', side_effect=ask):
            self.assertEqual(patterns.run(self.cfg, today=today), 0)
        self.assertEqual(len(prompts), 1)
        rows = prompts[0].split('Pending observations: ', 1)[1].split('\nEarlier observations', 1)[0]
        return {row['day'] for row in json.loads(rows)}

    def assert_kept(self, kind: str, data: bytes) -> None:
        lone_lf = data.count(b'\n') - data.count(b'\r\n')
        if kind == 'lf':
            self.assertNotIn(b'\r', data)
        elif kind == 'crlf':
            self.assertEqual(lone_lf, 0, data)
        else:  # the user's own LF line stays as typed; every line written is CRLF
            self.assertEqual(lone_lf, 1, data)
            self.assertIn(b'stored here.\n', data)

    def test_two_nightly_runs_keep_each_ledger_kind(self):
        for kind, ending in (('lf', '\n'), ('crlf', '\r\n'), ('mixed', '\r\n')):
            with self.subTest(kind=kind):
                self.evidence.parent.mkdir(parents=True, exist_ok=True)
                self.evidence.write_bytes(self.ledger(kind))
                self.assertEqual(patterns.last_scan(self.evidence.read_bytes().decode('utf-8')),
                                 dt.date(2026, 9, 3))
                support = self.observation(f'{kind}-support-4', 4)
                against = self.observation(f'{kind}-against-4', 4, f'No measurement needed for {kind}')
                pending = self.night(dt.date(2026, 9, 5), [
                    {'title': self.TITLE, 'kind': 'evidence', 'evidence': self.cited([support])},
                    {'title': self.TITLE, 'kind': 'counter', 'evidence': self.cited([against])}])
                self.assertEqual(pending, {'2026-09-04'})
                later = self.observation(f'{kind}-support-5', 5, f'Measure it first for {kind}')
                pending = self.night(dt.date(2026, 9, 6), [
                    {'title': self.TITLE, 'kind': 'evidence', 'evidence': self.cited([later])}])
                self.assertEqual(pending, {'2026-09-05'})
                data = self.evidence.read_bytes()
                text = data.decode('utf-8')
                self.assert_kept(kind, data)
                self.assertEqual(text.count('Last scan:'), 1, text)
                self.assertIn(f'Last scan: 2026-09-05{ending}', text)
                for proof in (support, against, later):
                    self.assertIn(f"`{proof['ref']}` \"{proof['quote']}\"{ending}", text)
                row = patterns.entries(text)[0]
                self.assertEqual([proof['ref'] for proof in row['evidence']],
                                 [proof['ref'] for proof in self.proofs + [support, later]])
                self.assertEqual(row['fingerprint'], patterns.fingerprint(self.TITLE, row['evidence']))
                self.assertEqual((row['status'], row['area'], row['id']),
                                 ('active', 'decisions', patterns.identifier(self.TITLE)))
                for path in (self.vault / 'receipts').glob(f'{kind}-*.md'):
                    path.unlink()  # the next kind starts from the same three days

    def test_duplicate_scan_lines_fold_into_the_newest(self):
        text = 'Header\r\nLast scan: 2026-09-01\r\nBody\r\nLast scan: 2026-09-07\n'
        self.assertEqual(patterns.last_scan(text), dt.date(2026, 9, 7))
        self.assertEqual(patterns.advance_scan(text, dt.date(2026, 9, 3)),
                         'Header\r\nLast scan: 2026-09-07\r\nBody\r\n')
        once = patterns.advance_scan('Header\r\nLast scan: 2026-09-01\r\nBody\r\n', dt.date(2026, 9, 3))
        self.assertEqual(once, 'Header\r\nLast scan: 2026-09-03\r\nBody\r\n')
        self.assertEqual(patterns.advance_scan(once, dt.date(2026, 9, 2)), once)

    def test_binding_date_and_veto_write_the_ledger_ending(self):
        item = patterns.candidate({'title': self.TITLE, 'area': 'decisions', 'behavior': 'Measure first.',
                                   'evidence': self.cited(self.proofs)},
                                  {proof['ref']: [proof] for proof in self.proofs})
        item['evidence'] = item['evidence'][:2]
        text = patterns.block(item, ending='\r\n').replace('Status: active', 'Status: candidate')
        text = '\r\n'.join(line for line in text.split('\r\n') if not line.startswith('Binding date:'))
        changed, added = patterns.add_evidence(text, patterns.entries(text)[0], [self.proofs[2]])
        self.assertTrue(added)
        self.assertIn('Status: active\r\n', changed)
        self.assertIn('Binding date: ', changed)
        self.assertEqual(changed.count('\n'), changed.count('\r\n'), changed)
        self.evidence.parent.mkdir(parents=True, exist_ok=True)
        self.evidence.write_bytes(changed.encode('utf-8'))
        self.assertEqual(patterns.veto(self.TITLE, self.cfg, reason='Not useful'), 0)
        vetoed = self.evidence.read_bytes()
        self.assertIn(b'Status: vetoed\r\n', vetoed)
        self.assertIn(b'user veto: Not useful\r\n', vetoed)
        self.assertEqual(vetoed.count(b'\n'), vetoed.count(b'\r\n'), vetoed)


if __name__ == '__main__':
    unittest.main()
