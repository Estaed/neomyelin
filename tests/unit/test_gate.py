from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from helpers import write

import gate

# Built from parts so this test file passes the grep it tests.
DRIVE_PATH = 'D' + ':/Projects/x'
NAMES = ('Tar' + 'ik', 'TAR' + 'IK', 'Tar' + '\u0131k')
EMAIL = 'someone' + '@' + 'example.org'


class GateTests(unittest.TestCase):
    def test_personal_data_hits(self):
        self.assertEqual(gate.personal_data(f'path {DRIVE_PATH}'), [gate.DRIVE])
        for name in NAMES:
            self.assertEqual(gate.personal_data(f'hello {name}'), [name])
        self.assertEqual(gate.personal_data(f'mail {EMAIL} now'), [EMAIL])

    def test_clean_text_has_no_hits(self):
        for text in ('print(f"failed:\\n")', '@property', 'pkg@1.2', 'C:/Users/x', 'Nova and Alex'):
            self.assertEqual(gate.personal_data(text), [], text)

    def test_project_records_are_skipped(self):
        for path in ('AGENTS.md', 'notes.md', 'tasks/Task-00.md', 'docs/x.md', 'reports/r.md'):
            self.assertTrue(gate.skipped(path), path)
        for path in ('scripts/gate.py', 'brain/scripts/recall.py', 'sub/AGENTS.md', 'templates/Personality.md',
                     'docs/releases/v1.2.0.md',  # release notes are published, so they are grepped
                     'docs/harnesses.md'):  # shipped in the release ZIP
            self.assertFalse(gate.skipped(path), path)

    def test_every_shipped_doc_is_grepped(self):
        # A file release.py ships must never be skipped by the grep (docs/harnesses.md, 2026-10-12).
        # release.py is a dev script and is not in the release ZIP, whose own gate runs this file.
        scripts = Path(gate.__file__).resolve().parent
        if not (scripts / 'release.py').is_file():
            self.skipTest('release.py is not shipped; this checks the dev repo')
        import sys
        sys.path.insert(0, str(scripts))
        import release
        for item in release.PRODUCT:
            if item.startswith('docs/') and not item.endswith('/'):
                self.assertFalse(gate.skipped(item), item)

    def test_grep_flags_release_notes_but_not_other_docs(self):
        with tempfile.TemporaryDirectory() as tmp:  # not a git checkout: every file counts
            root = Path(tmp)
            write(root / 'docs' / 'releases' / 'v9.9.9.md', f'# Notes\n\nThanks, {NAMES[0]}.\n')
            write(root / 'docs' / 'blueprint.md', f'Owner: {NAMES[0]}\n')
            self.assertEqual(gate.grep_personal(root), [f'docs/releases/v9.9.9.md:3: {NAMES[0]}'])

    def test_compile_reports_a_broken_file_and_skips_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / 'ok.py', 'x = 1\n')
            write(root / 'bad.py', 'def broken(:\n')
            write(root / '.cache' / 'skip.py', 'def also broken(:\n')
            write(root / '__pycache__' / 'skip.py', 'def also broken(:\n')
            self.assertEqual([p.name for p in gate.python_files(root)], ['bad.py', 'ok.py'])
            errors = gate.compile_all(root)
            self.assertEqual(len(errors), 1)
            self.assertTrue(errors[0].startswith('bad.py:'))


if __name__ == '__main__':
    unittest.main()
