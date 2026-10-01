from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from helpers import write

import make_test_vault


class MakeTestVaultTests(unittest.TestCase):
    def test_makes_an_empty_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'a' / 'vault'
            self.assertEqual(make_test_vault.make(target), {'vault': str(target.resolve())})
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            make_test_vault.make(target)  # an empty folder again: fine

    def test_never_over_existing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'vault'
            note = write(target / 'note.md', 'mine')
            with self.assertRaisesRegex(ValueError, 'not an empty folder'):
                make_test_vault.make(target)
            with self.assertRaisesRegex(ValueError, 'not an empty folder'):
                make_test_vault.make(note)
            self.assertEqual(note.read_text(encoding='utf-8'), 'mine')


if __name__ == '__main__':
    unittest.main()
