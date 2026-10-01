"""Marked source lines require whole quotes and a dated source."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import tempfile
import unittest

from helpers import make_vault, write
import evidence


class EvidenceTests(unittest.TestCase):
    def test_daily_markers_and_exact_quotes(self):
        with tempfile.TemporaryDirectory() as root:
            vault, _ = make_vault(Path(root))
            write(vault / 'daily' / '2026-09-01.md',
                  '- OBSERVATION: asks: "Show one measured result"\n'
                  '- REACTION: rejects: "Please check the actual value"\n'
                  '- CORRECTION: says: "Use the shorter answer"\n'
                  '- Note: "This is not a marked line"\n')
            found = evidence.daily_lines(vault, through=dt.date(2026, 9, 1))
            self.assertEqual(len(found), 3)
            source_map = {item['ref']: [item] for item in found}
            proof = {key: found[0][key] for key in ('day', 'ref', 'quote')}
            self.assertIsNotNone(evidence.verified(proof, source_map))
            proof['quote'] = 'one measured result'
            self.assertIsNone(evidence.verified(proof, source_map))
            self.assertEqual(evidence.daily_lines(vault, after=dt.date(2026, 9, 1)), [])


if __name__ == '__main__':
    unittest.main()
