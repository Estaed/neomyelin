"""The personality gate uses source days, exact quotes, and additive writes."""
from __future__ import annotations

import datetime as dt
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


if __name__ == '__main__':
    unittest.main()
