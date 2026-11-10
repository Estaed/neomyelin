"""receipt_commit.py: receipts and the daily view are committed at Stop, pushed in the background."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helpers import make_vault, remove_tree, write, write_config

import receipt_commit

SCRIPT = Path(receipt_commit.__file__).resolve()
IDENTITY = '[user]\n\tname = Alex Example\n\temail = alex.example\n'
# A wait not under test (a git call, the Stop's wait on its commit child) gets this long, however
# loaded the machine and however short the budget a test sets for another.
PATIENT = 60.0


@unittest.skipIf(shutil.which('git') is None, 'git is not installed')
class ReceiptCommitTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='receipt-commit-'))
        # Waits out a process still in the folder: on Windows a folder in use cannot be removed, and
        # a test that failed while the git it never kills still ran also gave an ERROR (2026-11-10).
        self.addCleanup(remove_tree, self.base)
        self.processes: list[subprocess.Popen] = []  # started by the test; waited for at cleanup
        self.releases: list[Path] = []  # of held hooks; let go at cleanup
        self.addCleanup(self.let_go)
        self.vault, cfg = make_vault(self.base)
        self.gitconfig = write(self.base / 'gitconfig', IDENTITY)
        # The machine's own git config (identity, hooks, signing, line endings) stays out. No git here
        # looks above the test's folder: when the temp folder sat in a repository, a child whose vault
        # lost its .git to cleanup staged receipts in that repository's index (2026-11-10).
        patch = mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(self.base, cfg)),
                                             'GIT_CONFIG_GLOBAL': str(self.gitconfig),
                                             'GIT_CONFIG_NOSYSTEM': '1',
                                             'GIT_CEILING_DIRECTORIES': str(self.base)})
        patch.start()
        self.addCleanup(patch.stop)
        # The Stop waits for its commit child as on an idle machine: under load 7 s ran out before
        # the child committed. The tests of the budget set a short one and hold the commit.
        budget = mock.patch.object(receipt_commit, 'BUDGET', PATIENT)
        budget.start()
        self.addCleanup(budget.stop)
        for name in (receipt_commit.INVOKED_ENV, *receipt_commit.GIT_LOCATION, 'GIT_AUTHOR_NAME',
                     'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL', 'EMAIL'):
            os.environ.pop(name, None)
        self.state = self.vault / '.brain' / '.state'
        self.log = self.state / receipt_commit.LOG_NAME
        self.health = self.state / 'health.json'

    # -- helpers ---------------------------------------------------------------------------

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        result = subprocess.run(['git', *args], cwd=cwd or self.vault, capture_output=True, text=True,
                                encoding='utf-8', errors='replace')
        if check and result.returncode:
            self.fail(f'git {" ".join(args)}: {result.stderr}')
        return result.stdout.strip()

    def init_repo(self, *tracked: str) -> str:
        """A vault repository with one commit holding notes/tracked.md and `tracked`; its branch."""
        self.git('init', '-q')
        write(self.vault / 'notes' / 'tracked.md', 'tracked\n')
        for name in tracked:
            write(self.vault / name, f'{name}\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'start')
        return self.git('symbolic-ref', '--short', 'HEAD')

    def remote(self, name: str = 'origin') -> Path:
        path = self.base / f'{name}.git'
        self.git('init', '-q', '--bare', str(path), cwd=self.base)
        self.git('remote', 'add', name, str(path))
        return path

    def stop(self) -> int:
        """The Stop hook in this process; its `--commit` child is a real detached process."""
        with mock.patch('sys.stdout', new_callable=io.StringIO), \
                mock.patch('sys.stderr', new_callable=io.StringIO):
            return receipt_commit.main([])

    def commit(self) -> int:
        """The `--commit` child in this process, so a test can patch what it calls."""
        with mock.patch('sys.stdout', new_callable=io.StringIO), \
                mock.patch('sys.stderr', new_callable=io.StringIO):
            return receipt_commit.main(['--commit'])

    def git_dir(self) -> Path:
        return Path(self.git('rev-parse', '--absolute-git-dir'))

    def hook(self, name: str, body: str) -> None:
        write(self.git_dir() / 'hooks' / name, f'#!/bin/sh\n{body}').chmod(0o755)

    def held_hook(self, name: str, seconds: int, then: str = '') -> tuple[Path, Path]:
        """Hook `name` sleeps `seconds` like a slow lint or network, and past that until the test lets
        it go, then runs `then`; its two marker files. The hold makes it outlast the call under test
        however loaded the machine. It lets itself go after a minute or more, so a regression that
        waits for it turns red instead of hanging the gate."""
        started, release = self.base / f'{name}.started', self.base / f'{name}.release'
        self.releases.append(release)
        # A hook runs in the top folder of the work tree, the vault; the markers sit beside it.
        self.hook(name, f': > ../{started.name}\nsleep {seconds}\nn=0\n'
                        f'while [ ! -e ../{release.name} ] && [ "$n" -lt 300 ]; do sleep 0.2; n=$((n + 1)); done\n'
                        f'{then}')
        return started, release

    def let_go(self) -> None:
        """Also after a failure: release every held hook, then wait for what the test started."""
        for release in self.releases:
            write(release, '')
        for process in self.processes:
            process.wait(timeout=120)

    def patient(self, *timed: str):
        """Patch `git` so every call but the subcommands in `timed` gets PATIENT seconds: a budget a
        test shortens for one call must not end another that load made slow (a killed status is a skip)."""
        real = receipt_commit.git

        def call(vault, *args, timeout, **kwargs):
            return real(vault, *args, timeout=timeout if args[0] in timed else max(timeout, PATIENT), **kwargs)

        return mock.patch.object(receipt_commit, 'git', call)

    def assert_still_committing(self, process: subprocess.Popen, head: str, started: Path) -> None:
        """The call returned while `process` commits on, held in its hook: alive, HEAD unchanged,
        index.lock held. Nothing here measures time; the hold decides when it ends."""
        self.assertIsNone(process.poll(), self.log_text())
        self.assertEqual(self.head(), head)
        self.wait_for(started.exists, 'the pre-commit hook started')
        self.assertTrue((self.git_dir() / 'index.lock').exists())
        self.assertIsNone(process.poll(), self.log_text())
        self.assertEqual(self.head(), head)

    def wait_for(self, done, what: str) -> None:
        deadline = time.monotonic() + 60
        while not done() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(done(), f'{what}; log: {self.log_text()}')

    def spawned(self) -> tuple[list[subprocess.Popen], mock._patch]:
        """Patch `spawn` so the real children start and the test can wait for them."""
        children = self.processes
        real = receipt_commit.spawn

        def record(*args):
            children.append(real(*args))
            return children[-1]

        return children, mock.patch.object(receipt_commit, 'spawn', side_effect=record)

    def head(self) -> str:
        return self.git('rev-parse', 'HEAD')

    def committed(self) -> set[str]:
        return set(self.git('show', '--name-only', '--format=', 'HEAD').splitlines())

    def log_text(self) -> str:
        return self.log.read_text(encoding='utf-8') if self.log.exists() else ''

    def warnings(self) -> list[str]:
        if not self.health.exists():
            return []
        return json.loads(self.health.read_text(encoding='utf-8')).get('warnings', [])

    def assert_untouched(self, head: str, receipt: str = 'receipts/new.md') -> None:
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git('status', '--porcelain', '--', receipt), f'?? {receipt}')

    # -- what is committed -----------------------------------------------------------------

    def test_commits_only_receipts_and_daily_and_leaves_other_work_as_it_was(self):
        self.init_repo('daily/2026-10-11.md', 'notes/partial.md')
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        write(self.vault / 'daily' / '2026-10-11.md', 'regenerated\n')
        write(self.vault / 'daily' / '2026-10-12.md', 'today\n')
        write(self.vault / 'notes' / 'staged.md', 'another session staged this\n')
        self.git('add', 'notes/staged.md')
        write(self.vault / 'notes' / 'partial.md', 'staged half\n')
        self.git('add', 'notes/partial.md')
        write(self.vault / 'notes' / 'partial.md', 'unstaged half\n')
        write(self.vault / 'notes' / 'tracked.md', 'edited, not staged\n')
        write(self.vault / 'notes' / 'untracked.md', 'new\n')
        status = self.git('status', '--porcelain', '--', 'notes')
        index = self.git('ls-files', '-s', '--', 'notes')

        self.assertEqual(self.stop(), 0)

        self.assertEqual(self.committed(), {'receipts/new.md', 'daily/2026-10-11.md', 'daily/2026-10-12.md'})
        self.assertEqual(self.git('log', '-1', '--format=%s'), 'Receipts and daily view (3 files)')
        self.assertEqual(self.git('status', '--porcelain', '--', 'notes'), status)
        self.assertEqual(self.git('ls-files', '-s', '--', 'notes'), index)
        self.assertEqual(self.git('status', '--porcelain', '--', 'receipts', 'daily'), '')
        self.assertIn('commit: Receipts and daily view (3 files)', self.log_text())
        self.assertEqual(self.warnings(), [])

    def test_commits_with_the_users_own_identity(self):
        self.init_repo()
        self.git('config', 'user.name', 'Robin Vault')
        self.git('config', 'user.email', 'robin.vault')
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        self.assertEqual(self.stop(), 0)
        self.assertEqual(self.git('log', '-1', '--format=%an|%ae|%cn|%ce'),
                         'Robin Vault|robin.vault|Robin Vault|robin.vault')

    def test_a_failed_commit_is_logged_and_warned_and_leaves_the_index_as_it_was(self):
        self.init_repo('receipts/old.md')
        head = self.head()
        self.gitconfig.write_text('', encoding='utf-8')
        self.git('config', 'user.useConfigOnly', 'true')  # no identity and no guessing it
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        write(self.vault / 'receipts' / 'old.md', 'edited\n')
        write(self.vault / 'receipts' / 'staged.md', 'staged\n')  # another session staged it, then edited it
        self.git('add', 'receipts/staged.md')
        write(self.vault / 'receipts' / 'staged.md', 'staged, then edited\n')
        status = self.git('status', '--porcelain', '--untracked-files=all', '--', 'receipts')
        index = self.git('ls-files', '-s')
        self.assertEqual(self.stop(), 0)
        self.assertEqual(self.head(), head)
        # Nothing is left staged for another session's commit to sweep in.
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all', '--', 'receipts'), status)
        self.assertEqual(self.git('ls-files', '-s'), index)
        self.assertIn('receipt commit failed:', self.log_text())
        self.assertEqual(len(self.warnings()), 1, self.warnings())
        self.assertIn('receipt commit failed:', self.warnings()[0])
        self.assertIn(receipt_commit.LOG_NAME, self.warnings()[0])

    def test_a_receipts_folder_alone_is_committed(self):
        # A vault whose daily view was never written: a pathspec naming it would fail the commit.
        self.init_repo()
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        self.assertEqual(self.stop(), 0)
        self.assertEqual(self.committed(), {'receipts/new.md'})
        self.assertEqual(self.git('log', '-1', '--format=%s'), 'Receipts and daily view (1 file)')
        self.assertEqual(self.warnings(), [])

    # -- the push --------------------------------------------------------------------------

    def test_the_commit_starts_the_push_only_when_the_vault_has_a_remote(self):
        self.init_repo()
        write(self.vault / 'receipts' / 'one.md', 'receipt\n')
        with mock.patch.object(receipt_commit, 'spawn') as spawn:
            self.assertEqual(self.commit(), 0)
            spawn.assert_not_called()
            self.assertEqual(self.committed(), {'receipts/one.md'})
            self.remote('backup')
            write(self.vault / 'receipts' / 'two.md', 'receipt\n')
            self.assertEqual(self.commit(), 0)
            spawn.assert_called_once_with(self.vault, '--push')
            self.assertEqual(self.commit(), 0)  # nothing new: no commit, no push
            spawn.assert_called_once_with(self.vault, '--push')

    def test_the_push_sets_a_missing_upstream_on_a_remote_not_named_origin(self):
        branch = self.init_repo()
        remote = self.remote('backup')
        for name in ('one', 'two'):  # the first push sets the upstream, the second uses it
            write(self.vault / 'receipts' / f'{name}.md', 'receipt\n')
            with mock.patch.object(receipt_commit, 'spawn') as spawn:
                self.assertEqual(self.commit(), 0)
            spawn.assert_called_once_with(self.vault, '--push')
            self.assertEqual(receipt_commit.main(['--push']), 0)
            self.assertEqual(self.git('rev-parse', branch, cwd=remote), self.head())
        self.assertEqual(self.git('rev-parse', '--abbrev-ref', '@{upstream}'), f'backup/{branch}')
        self.assertEqual(self.log_text().count(f'push: {branch}'), 2, self.log_text())
        self.assertEqual(self.warnings(), [])

    def test_a_failed_push_is_logged_and_warned_never_silent(self):
        self.init_repo()
        self.git('remote', 'add', 'origin', str(self.base / 'missing.git'))
        self.assertEqual(receipt_commit.main(['--push']), 0)
        self.assertIn('receipt push failed:', self.log_text())
        self.assertEqual(len(self.warnings()), 1, self.warnings())
        self.assertIn('receipt push failed:', self.warnings()[0])

    def test_the_children_are_detached_and_open_no_console_window(self):
        for flag in ('--commit', '--push'):
            with mock.patch.object(receipt_commit.subprocess, 'Popen') as popen:
                receipt_commit.spawn(self.vault, flag)
            args, kwargs = popen.call_args
            self.assertEqual(args[0], [sys.executable, str(SCRIPT), flag])
            for stream in ('stdin', 'stdout', 'stderr'):  # no pipe of the harness stays open
                self.assertIs(kwargs[stream], subprocess.DEVNULL)
            if os.name == 'nt':
                # DETACHED_PROCESS gives Python no console, so every git it starts opens a window.
                flags = kwargs['creationflags']
                self.assertEqual(flags, subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
                self.assertFalse(flags & subprocess.DETACHED_PROCESS)
            else:
                self.assertTrue(kwargs['start_new_session'])
                self.assertNotIn('creationflags', kwargs)

    def test_as_a_stop_hook_it_commits_at_once_and_pushes_in_the_background(self):
        branch = self.init_repo()
        remote = self.remote()
        self.git('push', '-q', '-u', 'origin', branch)
        # A slow network: the push takes 5 s and more; the Stop (and the harness's pipes) must not wait for it.
        started, release = self.held_hook('pre-push', 5)
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        payload = json.dumps({'session_id': 's1', 'hook_event_name': 'Stop', 'stop_hook_active': False})
        # A fresh process given the payload on stdin, its pipes captured, as the harness runs the hook.
        # Its budget is out of reach, so it ends only when its commit child has ended, however
        # loaded the machine: a commit child that waited for the push would hold it as long as the hook.
        code = ('import sys; sys.path.insert(0, sys.argv[1]); import receipt_commit; '
                'receipt_commit.BUDGET = 120.0; raise SystemExit(receipt_commit.main([]))')
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT.parent)], input=payload,
                                capture_output=True, text=True, encoding='utf-8', timeout=300)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertEqual(self.committed(), {'receipts/new.md'})  # committed before the Stop ended
        self.assertNotEqual(self.git('rev-parse', branch, cwd=remote), self.head())  # still pushing
        self.wait_for(started.exists, 'the push started')  # in the background, the Stop long gone
        self.assertNotEqual(self.git('rev-parse', branch, cwd=remote), self.head())
        write(release, '')
        self.wait_for(lambda: self.git('rev-parse', branch, cwd=remote) == self.head(), 'pushed')
        self.wait_for(lambda: f'push: {branch}' in self.log_text(), 'push logged')

    # -- never blocks ----------------------------------------------------------------------

    def test_the_layers_own_processes_do_nothing(self):
        self.init_repo()
        head = self.head()
        remote = self.remote()
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        with mock.patch.dict(os.environ, {receipt_commit.INVOKED_ENV: 'nightly'}):
            self.assertEqual(self.stop(), 0)
            self.assertEqual(self.commit(), 0)
            self.assertEqual(receipt_commit.main(['--push']), 0)
        self.assert_untouched(head)
        self.assertEqual(self.git('for-each-ref', cwd=remote), '')
        self.assertFalse(self.state.exists())

    def test_no_git_repository_and_nothing_to_commit_leave_no_trace(self):
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        self.assertEqual(self.stop(), 0)
        self.assertFalse((self.vault / '.git').exists())
        self.assertFalse(self.state.exists())
        (self.vault / 'receipts' / 'new.md').unlink()
        self.init_repo('receipts/old.md', 'daily/2026-10-11.md')
        head = self.head()
        with mock.patch.object(receipt_commit, 'spawn') as spawn:
            self.assertEqual(self.stop(), 0)
        spawn.assert_not_called()  # a Stop with nothing to commit starts no child
        self.assertEqual(self.head(), head)
        self.assertFalse(self.state.exists())

    def test_a_missing_config_exits_zero(self):
        with mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(self.base / 'nowhere.json')}), \
                mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(receipt_commit.main([]), 0)
        self.assertIn('receipt commit skipped', error.getvalue())

    def test_a_held_index_lock_is_skipped_and_left_alone(self):
        self.init_repo()
        head = self.head()
        lock = Path(self.git('rev-parse', '--absolute-git-dir')) / 'index.lock'
        lock.write_text('', encoding='utf-8')
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        self.assertEqual(self.stop(), 0)
        self.assertTrue(lock.exists())
        self.assert_untouched(head)
        self.assertIn('skipped: another git process holds index.lock', self.log_text())
        self.assertEqual(self.warnings(), [])

    def test_a_lock_taken_after_the_check_is_a_skip_not_a_failure(self):
        self.init_repo()
        head = self.head()
        lock = Path(self.git('rev-parse', '--absolute-git-dir')) / 'index.lock'
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        lock.write_text('', encoding='utf-8')
        with mock.patch.object(receipt_commit, 'busy', return_value=None):  # the race window
            self.assertEqual(self.commit(), 0)
        lock.unlink()
        self.assert_untouched(head)
        self.assertIn('skipped: another git process holds index.lock (add)', self.log_text())
        self.assertEqual(self.warnings(), [])

    def race_the_commit(self, lock: Path | None, released: bool = True, meanwhile=None):
        """Between `add` and `commit`: `meanwhile` runs (another commit lands), then another process
        takes index.lock (when `lock` is given), released before the index is put back."""
        real = receipt_commit.git

        def racing(vault, *args, **kwargs):
            if args[0] != 'commit':
                return real(vault, *args, **kwargs)
            if meanwhile:
                meanwhile()
            if lock:
                lock.write_text('', encoding='utf-8')
            try:
                return real(vault, *args, **kwargs)
            finally:
                if lock and released:
                    lock.unlink()

        return mock.patch.object(receipt_commit, 'git', side_effect=racing)

    def test_a_lock_taken_between_add_and_commit_leaves_the_receipt_unstaged(self):
        self.init_repo()
        head = self.head()
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        with self.race_the_commit(self.git_dir() / 'index.lock', released=True):
            self.assertEqual(self.commit(), 0)
        self.assert_untouched(head)
        self.assertIn('skipped: another git process holds index.lock (commit)', self.log_text())
        self.assertEqual(self.warnings(), [])

    def test_a_lock_that_outlasts_the_wait_leaves_the_receipt_staged_with_a_warning(self):
        self.init_repo()
        head = self.head()
        lock = self.git_dir() / 'index.lock'
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        with self.race_the_commit(lock, released=False), mock.patch.object(receipt_commit, 'RESTORE_WAIT', 0.5):
            self.assertEqual(self.commit(), 0)
        lock.unlink()
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git('status', '--porcelain', '--', 'receipts'), 'A  receipts/new.md')
        self.assertEqual(len(self.warnings()), 1, self.warnings())
        self.assertIn('1 files left staged', self.warnings()[0])

    def test_a_parallel_stop_that_commits_the_receipts_first_is_no_failure(self):
        # Two sessions stop together: the other one's commit lands between this one's add and commit.
        self.init_repo()
        lock = self.git_dir() / 'index.lock'
        for name, held in (('one', None), ('two', lock)):  # git finds nothing to commit / the lock is still held
            receipt = f'receipts/{name}.md'
            write(self.vault / receipt, 'receipt\n')
            with self.race_the_commit(held, meanwhile=lambda: self.git('commit', '-q', '-m', 'other Stop',
                                                                        '--', 'receipts')):
                self.assertEqual(self.commit(), 0)
            self.assertEqual(self.git('log', '-1', '--format=%s'), 'other Stop')
            self.assertEqual(self.committed(), {receipt})
            self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all', '--', 'receipts'), '')
        self.assertEqual(self.log_text().count('skipped: nothing left to commit'), 2, self.log_text())
        self.assertEqual(self.warnings(), [])

    def test_another_commit_between_add_and_commit_still_leaves_the_receipt_unstaged(self):
        # A session commits its own work while this commit waits on that commit's index.lock.
        self.init_repo()
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        write(self.vault / 'notes' / 'tracked.md', 'edited\n')
        with self.race_the_commit(self.git_dir() / 'index.lock',
                                  meanwhile=lambda: self.git('commit', '-q', '-m', 'other work', '--', 'notes')):
            self.assertEqual(self.commit(), 0)
        self.assertEqual(self.git('log', '-1', '--format=%s'), 'other work')
        self.assert_untouched(self.head())  # not left staged for a plain `git commit` to sweep in
        self.assertIn('skipped: another git process holds index.lock (commit)', self.log_text())
        self.assertEqual(self.warnings(), [])

    def conflict(self, *command: str) -> str:
        """Leave `command` (a merge or a rebase onto `side`) stopped on a conflict; HEAD then."""
        branch = self.init_repo()
        self.git('checkout', '-q', '-b', 'side')
        write(self.vault / 'notes' / 'tracked.md', 'side\n')
        self.git('commit', '-q', '-am', 'side')
        self.git('checkout', '-q', branch)
        write(self.vault / 'notes' / 'tracked.md', 'main\n')
        self.git('commit', '-q', '-am', 'main')
        self.git(*command, 'side', check=False)
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        return self.head()

    def test_a_merge_in_progress_is_skipped(self):
        head = self.conflict('merge')
        git_dir = Path(self.git('rev-parse', '--absolute-git-dir'))
        self.assertTrue((git_dir / 'MERGE_HEAD').exists())
        self.assertEqual(self.stop(), 0)
        self.assert_untouched(head)
        self.assertTrue((git_dir / 'MERGE_HEAD').exists())
        self.assertIn('skipped: a merge is in progress', self.log_text())
        self.assertEqual(self.warnings(), [])

    def test_a_rebase_in_progress_is_skipped(self):
        head = self.conflict('rebase')
        git_dir = Path(self.git('rev-parse', '--absolute-git-dir'))
        self.assertTrue((git_dir / 'rebase-merge').exists())
        self.assertEqual(self.stop(), 0)
        self.assert_untouched(head)
        self.assertIn('skipped: a rebase is in progress', self.log_text())
        self.assertEqual(self.warnings(), [])

    def test_a_detached_head_is_skipped(self):
        # A commit there leaves the working tree at the next checkout, kept only by a dangling commit.
        self.init_repo()
        self.git('checkout', '-q', '--detach')
        head = self.head()
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        self.assertEqual(self.stop(), 0)
        self.assert_untouched(head)
        self.assertIn('skipped: HEAD is detached', self.log_text())
        self.assertEqual(self.warnings(), [])

    def test_a_slow_commit_goes_on_after_the_stop_and_is_still_pushed(self):
        branch = self.init_repo()
        head = self.head()
        remote = self.remote()
        self.git('push', '-q', '-u', 'origin', branch)
        started, release = self.held_hook('pre-commit', 3)
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        children, spawn = self.spawned()
        with mock.patch.object(receipt_commit, 'BUDGET', 1.0), self.patient(), spawn:
            self.assertEqual(self.stop(), 0)
        self.assert_still_committing(children[0], head, started)  # the Stop did not wait for the commit
        write(release, '')
        self.assertEqual(children[0].wait(timeout=60), 0)
        self.assertEqual(self.committed(), {'receipts/new.md'})
        self.wait_for(lambda: self.git('rev-parse', branch, cwd=remote) == self.head(), 'pushed')
        self.wait_for(lambda: f'push: {branch}' in self.log_text(), 'push logged')
        self.assertEqual(self.warnings(), [])

    def test_a_slow_commit_that_fails_after_the_stop_is_still_recorded(self):
        self.init_repo()
        head = self.head()
        started, release = self.held_hook('pre-commit', 3, 'echo lint failed >&2\nexit 1\n')
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        children, spawn = self.spawned()
        with mock.patch.object(receipt_commit, 'BUDGET', 1.0), self.patient(), spawn:
            self.assertEqual(self.stop(), 0)
        self.assert_still_committing(children[0], head, started)  # it fails only after the Stop
        write(release, '')
        self.assertEqual(children[0].wait(timeout=60), 0)
        self.assert_untouched(head)
        self.assertIn('receipt commit failed: lint failed', self.log_text())
        self.assertEqual(len(self.warnings()), 1, self.warnings())

    def test_a_commit_past_the_childs_timeout_is_left_to_finish_never_killed(self):
        # A killed `git commit` leaves index.lock behind and every later git command fails.
        self.init_repo()
        head = self.head()
        # It prints after the child has returned: a closed pipe would kill it then.
        started, release = self.held_hook('pre-commit', 3, 'echo lint passed\necho lint detail >&2\n')
        write(self.vault / 'receipts' / 'new.md', 'receipt\n')
        lock = self.git_dir() / 'index.lock'
        real_popen = subprocess.Popen

        def record(*args, **kwargs):
            self.processes.append(real_popen(*args, **kwargs))
            return self.processes[-1]

        # Only the commit gets the short timeout: under load the git calls before it may take longer.
        with mock.patch.object(receipt_commit, 'CHILD_TIMEOUT', 1.0), self.patient('commit'), \
                mock.patch.object(receipt_commit.subprocess, 'Popen', side_effect=record):
            self.assertEqual(self.commit(), 0)
        commit = next(process for process in self.processes if 'commit' in process.args)
        self.assert_still_committing(commit, head, started)
        # Its outcome is not seen, so it is a warning, not a quiet skip.
        self.assertIn('receipt commit failed: git commit gave no answer', self.log_text())
        self.assertEqual(len(self.warnings()), 1, self.warnings())
        write(release, '')
        self.assertEqual(commit.wait(timeout=60), 0)  # it finished by itself, it was not killed
        self.assertEqual(self.committed(), {'receipts/new.md'})
        self.assertFalse(lock.exists())

    def test_changed_paths_skips_the_source_of_a_rename(self):
        self.assertEqual(receipt_commit.changed_paths('R  receipts/b.md\0receipts/a.md\0?? daily/x.md\0'),
                         ['receipts/b.md', 'daily/x.md'])


if __name__ == '__main__':
    unittest.main()
