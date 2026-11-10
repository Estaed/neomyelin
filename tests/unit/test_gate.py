from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

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
        with tempfile.TemporaryDirectory() as tmp:  # no checkout's top: every file counts
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


@unittest.skipIf(shutil.which('git') is None, 'git is not installed')
class GrepFileListTests(unittest.TestCase):
    """Which files the grep reads depends on where the tree sits, not on what git lists there.
    release.py unpacks its ZIP in the temp folder, and a session's temp folder can be an ignored
    folder of a repository, where git lists nothing: the grep read zero files (2026-11-10)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        # The machine's git config (a global excludes file) and a calling hook's GIT_DIR or
        # GIT_INDEX_FILE stay out of these git calls and the gate's.
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=str(write(self.base / 'gitconfig', '')), GIT_CONFIG_NOSYSTEM='1')
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def checkout(self) -> Path:
        """A git checkout that ignores `scratch/`, as the dev repo ignores `.cache/`; README.md added."""
        repo = self.base / 'checkout'
        write(repo / '.gitignore', 'scratch/\n')
        write(repo / 'README.md', 'clean\n')
        for args in (('init', '-q'), ('add', '-A')):
            subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
        return repo

    def release_tree(self, tree: Path) -> str:
        """A tree like the unpacked ZIP, a name planted in its release notes; the hit it must give."""
        write(tree / 'scripts' / 'gate.py', 'x = 1\n')
        write(tree / 'docs' / 'releases' / 'v9.9.9.md', f'# Notes\n\nThanks, {NAMES[0]}.\n')
        return f'docs/releases/v9.9.9.md:3: {NAMES[0]}'

    def test_a_tree_in_an_ignored_folder_of_a_checkout_is_read_whole(self):
        tree = self.checkout() / 'scratch' / 'neomyelin-v9.9.9'
        hit = self.release_tree(tree)
        self.assertEqual(gate.repo_files(tree), ['docs/releases/v9.9.9.md', 'scripts/gate.py'])
        self.assertEqual(gate.grep_personal(tree), [hit])

    def test_at_the_top_of_a_checkout_git_lists_and_ignored_files_stay_out(self):
        repo = self.checkout()
        write(repo / 'new.md', f'{NAMES[0]}\n')  # new, never added: read
        write(repo / 'scratch' / 'notes.md', f'{NAMES[0]}\n')  # ignored: not read
        self.assertEqual(gate.repo_files(repo), ['.gitignore', 'README.md', 'new.md'])
        self.assertEqual(gate.grep_personal(repo), [f'new.md:1: {NAMES[0]}'])
        if os.name == 'nt':  # the same folder spelled another way is still the top
            self.assertTrue(gate.work_tree_top(Path(str(repo).upper())))

    def test_only_folders_inside_the_tree_are_caches(self):
        # A temp folder under ~/.cache, or under a dist/ folder, holds a tree like any other.
        tree = self.base / '.cache' / 'dist' / 'neomyelin-v9.9.9'
        hit = self.release_tree(tree)
        for cache in ('.cache', 'dist', '__pycache__'):
            write(tree / cache / 'x.md', f'{NAMES[0]}\n')
        self.assertEqual(gate.grep_personal(tree), [hit])

    def test_without_git_every_file_is_read(self):
        tree = self.base / 'tree'
        hit = self.release_tree(tree)
        with mock.patch.object(gate.subprocess, 'run', side_effect=FileNotFoundError('git')):
            self.assertEqual(gate.grep_personal(tree), [hit])


if __name__ == '__main__':
    unittest.main()
