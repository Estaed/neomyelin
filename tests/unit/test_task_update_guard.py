"""task_update.py refuses a task file with uncommitted changes unless --mine says they are its own."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, write

import task_update

# Fixture commits only: an identity and no signing, whatever the machine's git config says.
GIT = ('git', '-c', 'user.name=Sam', '-c', 'user.email=sam@localhost', '-c', 'commit.gpgsign=false')


@unittest.skipUnless(shutil.which('git'), 'git is not installed')
class TaskUpdateGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        (self.vault / '.brain').mkdir(parents=True)
        shutil.copyfile(ROOT / 'engine' / 'brain.py', self.vault / 'brain.py')
        meta = {'id': 'water', 'title': 'Water the garden', 'status': 'active', 'owner': 'Sam',
                'kind': 'task', 'revision': 3}
        self.task = write(self.vault / 'tasks' / 'water.md', '---\n' + json.dumps(meta, indent=2) + '\n---\nBy hand.\n')
        self.git('init', '-q')
        self.git('add', 'tasks')
        self.git('commit', '-q', '-m', 'tasks')
        patch = mock.patch.object(task_update.config, 'load', return_value={'vault': str(self.vault)})
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, *args: str) -> None:
        subprocess.run([*GIT, *args], cwd=self.vault, check=True, capture_output=True)

    def update(self, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = task_update.main(list(args))
        return code, err.getvalue()

    def status(self) -> str:
        return json.loads(self.task.read_text(encoding='utf-8').split('---')[1])['status']

    def test_a_clean_task_updates_as_before(self):
        self.assertEqual(self.update('water', 'status=done'), (0, ''))
        self.assertEqual(self.status(), 'done')

    def test_uncommitted_changes_stop_the_update(self):
        self.task.write_text(self.task.read_text(encoding='utf-8') + 'Another session was here.\n', encoding='utf-8')
        with mock.patch.object(task_update, '_call') as call:
            code, err = self.update('water', 'status=done')
        self.assertEqual(code, 3)
        call.assert_not_called()
        self.assertIn('tasks/water.md has uncommitted changes', err)
        self.assertIn('Ask that session first', err)
        self.assertIn('rerun the same command with --mine', err)
        self.assertEqual(self.status(), 'active')

    def test_staged_and_new_task_files_count_as_uncommitted(self):
        self.task.write_text(self.task.read_text(encoding='utf-8') + 'Staged.\n', encoding='utf-8')
        self.git('add', 'tasks')
        self.assertEqual(self.update('water', 'status=done')[0], 3)
        new = dict(id='feed', title='Feed the cat', status='active', owner='Sam', kind='task', revision=1)
        write(self.vault / 'tasks' / 'feed.md', '---\n' + json.dumps(new, indent=2) + '\n---\n')
        self.assertEqual(self.update('feed', '--json', '{"status": "done"}')[0], 3)

    def test_mine_lets_this_sessions_own_changes_through(self):
        self.task.write_text(self.task.read_text(encoding='utf-8') + 'My own fix.\n', encoding='utf-8')
        self.assertEqual(self.update('water', 'status=done', '--mine'), (0, ''))
        self.assertEqual(self.status(), 'done')
        self.assertEqual(self.update('--mine', 'water', '--json', '{"status": "waiting"}'), (0, ''))
        self.assertEqual(self.status(), 'waiting')

    def test_only_the_named_task_counts(self):
        write(self.vault / 'tasks' / 'other.md', 'another session works here\n')
        self.assertEqual(self.update('water', 'status=done'), (0, ''))

    def test_a_vault_outside_any_repository_updates_as_before(self):
        (self.vault / '.git').rename(self.vault.parent / 'git-moved-away')  # its objects are read-only on Windows
        self.task.write_text(self.task.read_text(encoding='utf-8') + 'No git here.\n', encoding='utf-8')
        self.assertEqual(self.update('water', 'status=done'), (0, ''))

    def parent_repository(self, *tracked: str) -> None:
        """The vault becomes a plain folder inside a repository one level up."""
        parent = self.vault.parent
        (self.vault / '.git').rename(parent / 'git-moved-away')
        write(parent / 'readme.md', 'The parent project.\n')
        for args in (('init', '-q'), ('add', 'readme.md', *tracked), ('commit', '-q', '-m', 'parent')):
            subprocess.run([*GIT, *args], cwd=parent, check=True, capture_output=True)

    def test_a_parent_repository_that_tracks_the_vault_guards_its_tasks(self):
        self.parent_repository('vault/tasks')
        self.assertEqual(self.update('water', 'status=done'), (0, ''))  # clean: updates as before
        code, err = self.update('water', 'status=waiting')  # the update left the file uncommitted
        self.assertEqual(code, 3)
        self.assertIn('tasks/water.md has uncommitted changes', err)
        self.assertEqual(self.status(), 'done')
        new = dict(id='feed', title='Feed the cat', status='active', owner='Sam', kind='task', revision=1)
        write(self.vault / 'tasks' / 'feed.md', '---\n' + json.dumps(new, indent=2) + '\n---\n')
        self.assertEqual(self.update('feed', 'status=done')[0], 3)

    def test_a_parent_repository_that_never_committed_the_vault_updates_as_before(self):
        self.parent_repository()
        self.task.write_text(self.task.read_text(encoding='utf-8') + 'Not in the parent.\n', encoding='utf-8')
        self.assertEqual(self.update('water', 'status=done'), (0, ''))
        self.assertEqual(self.status(), 'done')

    def test_no_git_on_the_machine_updates_as_before(self):
        self.task.write_text(self.task.read_text(encoding='utf-8') + 'Changed.\n', encoding='utf-8')
        with mock.patch.object(task_update.subprocess, 'run', side_effect=FileNotFoundError('git')):
            self.assertFalse(task_update._uncommitted(self.vault, 'water'))


if __name__ == '__main__':
    unittest.main()
