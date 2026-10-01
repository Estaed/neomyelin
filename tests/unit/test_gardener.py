from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import make_vault, write
import gardener


class GardenerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault, self.cfg = make_vault(Path(self.temp.name))
        self.cfg['companion_dir'] = 'companion'
        self.evolution = write(self.vault / 'companion' / 'Evolution.md',
                               '# Evolution\n\n## Candidates\nuser-owned line\n')
        self.ledger = self.vault / '.brain' / 'reports' / 'agent-experience.jsonl'
        patch = mock.patch.object(gardener.config, 'load', return_value=self.cfg)
        patch.start()
        self.addCleanup(patch.stop)

    def save(self, symptom='same symptom', *extra):
        return gardener.main(['record', '--category', 'workflow', '--symptom', symptom,
                              '--evidence', 'observed output', '--workaround', 'retry once',
                              '--proposal', 'check state first', *extra])

    def rows(self):
        return [json.loads(line) for line in self.ledger.read_text(encoding='utf-8').splitlines()]

    def set_day(self, index, day):
        rows = self.rows()
        rows[index]['created_at'] = day + 'T12:00:00+00:00'
        self.ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def test_locked_append_only_and_explicit_same(self):
        self.assertEqual(self.save('first symptom', '--task', 'task-1', '--commit', 'abc'), 0)
        before = self.ledger.read_bytes()
        fp = self.rows()[0]['fingerprint']
        self.assertEqual(self.save('different wording', '--same', 'unknown'), 1)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.save('different wording', '--same', fp), 0)
        self.assertTrue(self.ledger.read_bytes().startswith(before))
        self.assertEqual(self.rows()[1]['fingerprint'], fp)
        self.assertTrue(self.ledger.with_name(f'.{self.ledger.name}.lock').exists())
        self.assertEqual(self.rows()[0]['task'], 'task-1')

    def test_distinct_days_and_candidate_idempotence(self):
        self.save()
        self.save()
        original = self.evolution.read_bytes()
        with mock.patch('recall.choose_engine', return_value=('bm25', 'unavailable')):
            self.assertEqual(gardener.main(['candidates']), 0)
            self.assertEqual(self.evolution.read_bytes(), original)
            self.set_day(0, '2026-09-30')
            self.assertEqual(gardener.main(['candidates', '--dry-run']), 0)
            self.assertEqual(self.evolution.read_bytes(), original)
            self.assertEqual(gardener.main(['candidates']), 0)
            result = self.evolution.read_bytes()
            self.assertTrue(result.startswith(original))
            self.assertEqual(result.count(b'### Candidate'), 1)
            self.assertEqual(gardener.main(['candidates']), 0)
            self.assertEqual(self.evolution.read_bytes(), result)

    def test_embedding_clusters_distinct_fingerprints_and_fallback_is_visible(self):
        rows = [{'created_at': '2026-09-30T00:00:00Z', 'fingerprint': 'a' * 12,
                 'symptom': 'timeout on command'},
                {'created_at': '2026-10-01T00:00:00Z', 'fingerprint': 'b' * 12,
                 'symptom': 'command stalled'}]
        with mock.patch('recall.choose_engine', return_value=('ollama', 'ready')), \
                mock.patch('recall._embed', return_value=[[1.0, 0.0], [0.99, 0.1]]):
            self.assertEqual(len(gardener._semantic_groups(rows)), 1)
        output = io.StringIO()
        with mock.patch('recall.choose_engine', return_value=('bm25', 'not ready')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(len(gardener._semantic_groups(rows)), 2)
        self.assertIn('fingerprints only', output.getvalue())

    def test_decisions_and_correction_result_update(self):
        self.save()
        self.set_day(0, '2026-09-28')
        self.save()
        self.set_day(1, '2026-09-29')
        with mock.patch('recall.choose_engine', return_value=('bm25', 'unavailable')):
            gardener.main(['candidates'])
        text = self.evolution.read_text(encoding='utf-8')
        self.evolution.write_text(text.replace('- Decision: pending',
                                               '- Decision: rule\n- Decision note: 2026-09-29 fixed in guide'),
                                  encoding='utf-8')
        self.save()
        self.set_day(2, '2026-10-01')
        output = io.StringIO()
        with mock.patch('recall.choose_engine', return_value=('bm25', 'unavailable')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(gardener.main(['report']), 0)
            self.assertIn('1 new cases', output.getvalue())
            self.assertEqual(gardener.main(['candidates']), 0)
        self.assertIn('- Failed: 1 new cases, last 2026-10-01 (gardener)',
                      self.evolution.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
