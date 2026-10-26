"""git_gate.py refuses a commit that would store a file with other line endings than HEAD.

Each case is a real temporary git repository whose `core.autocrlf` is set explicitly (this
machine's global value may be `true`). The hook runs as the harness runs it, the PreToolUse JSON
on stdin from an unrelated folder, and only reads the repository; it never runs the command.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helpers import ROOT

import git_gate

SCRIPT = ROOT / 'brain' / 'scripts' / 'git_gate.py'
ENV = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}
LF, CRLF, MIXED = b'one\ntwo\n', b'one\r\ntwo\r\n', b'one\r\ntwo\n'


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(['git', *args], cwd=repo, capture_output=True, check=True).stdout


def crlf(data: bytes) -> bytes:
    return data.replace(b'\n', b'\r\n')


class LineEndingGate(unittest.TestCase):
    autocrlf = 'false'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / 'repo'
        self.elsewhere = self.base / 'not-a-repo'
        self.repo.mkdir()
        self.elsewhere.mkdir()
        git(self.repo, 'init', '-q')
        for key, value in (('core.autocrlf', 'false'), ('core.safecrlf', 'false'), ('user.name', 't'),
                           ('user.email', 'tester.local'), ('commit.gpgsign', 'false')):
            git(self.repo, 'config', key, value)
        self.put('.gitattributes', b'*.raw binary\n*.keep -text\n')
        self.put('lf.txt', LF)
        self.put('crlf.txt', CRLF)
        self.put('mixed.txt', MIXED)
        self.put('bin.dat', b'\x00\x01head\nbody\n')
        self.put('declared.raw', LF)
        self.put('kept.keep', LF)
        self.put('gone.txt', LF)
        self.put('other.txt', b'other\n')
        self.put('sub/deep.txt', b'deep\nfile\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'init')
        git(self.repo, 'config', 'core.autocrlf', self.autocrlf)  # HEAD was written with false

    def put(self, name: str, data: bytes) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def stage(self, *names: str) -> None:
        git(self.repo, 'add', '--', *names)

    def hook(self, command: str, cwd: Path | None = None, tool: str = 'Bash') -> str | None:
        payload = json.dumps({'tool_name': tool, 'tool_input': {'command': command},
                              'cwd': str(cwd or self.repo)})
        result = subprocess.run([sys.executable, str(SCRIPT), '--pre-tool-use'], input=payload.encode('utf-8'),
                                capture_output=True, env=ENV, cwd=self.elsewhere, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.decode('utf-8').strip()
        if not out:
            return None
        answer = json.loads(out)['hookSpecificOutput']
        self.assertEqual((answer['hookEventName'], answer['permissionDecision']), ('PreToolUse', 'deny'))
        return answer['permissionDecisionReason']

    def refused(self, command: str, *named: str, cwd: Path | None = None, tool: str = 'Bash') -> str:
        reason = self.hook(command, cwd, tool)
        self.assertIsNotNone(reason, f'allowed: {command}')
        self.assertTrue(reason.startswith('git_gate: this commit would store other line endings'), reason)
        self.assertNotIn('\n', reason)
        self.assertIn('# eol-ok', reason)
        self.assertIn("read 'rb'", reason)
        for name in named:
            self.assertIn(name, reason)
        return reason

    def allowed(self, command: str, cwd: Path | None = None) -> None:
        self.assertIsNone(self.hook(command, cwd), f'refused: {command}')


class Refused(LineEndingGate):
    def test_staged_lf_file_stored_as_crlf_is_refused_in_one_line(self):
        self.put('lf.txt', crlf(b'one\ntwo\nthree\n'))
        self.stage('lf.txt')
        self.refused('git commit -m "x"', 'lf.txt (lf -> crlf)')
        self.refused('git commit -m "x"', 'lf.txt (lf -> crlf)', tool='PowerShell')
        self.refused('git status && git commit -m x', 'lf.txt (lf -> crlf)')
        self.refused('git status; git commit -m x', 'lf.txt (lf -> crlf)')

    def test_crlf_to_lf_and_lf_to_mixed_are_refused(self):
        self.put('crlf.txt', b'one\ntwo\nthree\n')
        self.put('other.txt', b'other\r\nmore\n')
        self.stage('crlf.txt', 'other.txt')
        self.refused('git commit -m x', 'crlf.txt (crlf -> lf)', 'other.txt (lf -> mixed)')

    def test_commit_all_stores_the_tracked_modified_files(self):
        self.put('lf.txt', CRLF)
        for command in ('git commit -am "x"', 'git commit -a -m x', 'git commit --all -m x', 'git commit -qam x',
                        'git commit -a -m "x" 2>&1'):
            with self.subTest(command=command):
                self.refused(command, 'lf.txt (lf -> crlf)')

    def test_git_add_earlier_in_the_command_is_replayed(self):
        self.put('lf.txt', CRLF)
        self.put('new.txt', LF)
        self.refused('git add lf.txt && git commit -m "x"', 'lf.txt (lf -> crlf)')
        self.refused('git add -A; git commit -m x', 'lf.txt (lf -> crlf)')
        self.refused('git add . && git commit -m x', 'lf.txt (lf -> crlf)')
        self.refused('git add new.txt lf.txt && git commit -m x', 'lf.txt (lf -> crlf)')

    def test_add_from_a_subdirectory_names_the_root_relative_file(self):
        self.put('sub/deep.txt', b'deep\r\nfile\r\n')
        self.refused('git add deep.txt && git commit -m x', 'sub/deep.txt (lf -> crlf)', cwd=self.repo / 'sub')

    def test_git_dash_c_and_cd_point_at_the_repository(self):
        self.put('lf.txt', CRLF)
        repo = self.repo.as_posix()
        self.allowed('git commit -am x', cwd=self.elsewhere)  # the session's folder is not a repository
        self.refused(f'git -C "{repo}" commit -am x', 'lf.txt', cwd=self.elsewhere)
        self.refused(f'git -C {repo} add lf.txt && git -C {repo} commit -m x', 'lf.txt', cwd=self.elsewhere)
        self.refused(f'cd "{repo}" && git commit -am x', 'lf.txt', cwd=self.elsewhere)
        self.refused(f'cd {self.repo}; git commit -am x', 'lf.txt', cwd=self.elsewhere, tool='PowerShell')
        self.refused('git -C sub commit -am x', 'lf.txt')

    def test_heredoc_message_still_checks_what_is_staged(self):
        self.put('lf.txt', CRLF)
        self.stage('lf.txt')
        self.refused("git commit -m \"$(cat <<'EOF'\nTitle line\n\nBody, with a lf.txt word.\nEOF\n)\"",
                     'lf.txt (lf -> crlf)')
        self.refused("git commit -F - <<'EOF'\nTitle\nEOF", 'lf.txt (lf -> crlf)')

    def test_commit_with_paths_stores_those_files_from_the_working_tree(self):
        self.put('lf.txt', CRLF)                      # changed, not staged
        self.refused('git commit -m x lf.txt', 'lf.txt (lf -> crlf)')
        self.refused('git commit -m x -- lf.txt', 'lf.txt (lf -> crlf)')
        self.put('lf.txt', LF)
        self.put('crlf.txt', LF)
        self.stage('crlf.txt')                        # staged, but `commit <paths>` leaves it out
        self.put('other.txt', b'other\nmore\n')
        self.allowed('git commit -m x other.txt')
        self.refused('git commit -i -m x other.txt', 'crlf.txt (crlf -> lf)')

    def test_up_to_eight_files_are_named(self):
        for number in range(10):
            self.put(f'many/f{number}.txt', LF)
        self.stage('many')
        git(self.repo, 'commit', '-q', '-m', 'many')
        for number in range(10):
            self.put(f'many/f{number}.txt', CRLF)
        reason = self.refused('git commit -am x', 'many/f0.txt (lf -> crlf)', 'many/f7.txt', 'and 2 more')
        self.assertNotIn('many/f9.txt', reason)

    def test_non_ascii_path_is_named(self):
        name = '850-Companion \U0001f52e/Evolution.md'
        self.put(name, LF)
        self.stage(name)
        git(self.repo, 'commit', '-q', '-m', 'emoji')
        self.put(name, CRLF)
        self.stage(name)
        self.refused('git commit -m x', f'{name} (lf -> crlf)')

    def test_an_agents_own_worktree_is_checked_too(self):
        tree = self.base / 'tree'
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(tree))
        (tree / 'lf.txt').write_bytes(CRLF)
        self.refused('git commit -am x', 'lf.txt (lf -> crlf)', cwd=tree)
        self.refused('git add lf.txt && git commit -m x', 'lf.txt (lf -> crlf)', cwd=tree)


    def test_a_quoted_separator_or_hash_keeps_the_all_flag(self):
        self.put('lf.txt', CRLF)
        self.refused('git commit -m "update; notes" -a', 'lf.txt')
        self.refused('git commit -m "#29 fix" -a', 'lf.txt')
        self.refused("git commit -m 'a | b' -a", 'lf.txt')

    def test_a_bare_minus_text_file_is_checked(self):
        self.put('kept.keep', CRLF)  # `-text` stops conversion; the CRLF reaches history
        self.stage('kept.keep')
        self.refused('git commit -m x', 'kept.keep')

    def test_an_add_glob_stages_what_bash_would(self):
        self.put('lf.txt', CRLF)
        self.put('sub/deep.txt', crlf(b'deep\nfile\n'))
        reason = self.refused('git add *.txt && git commit -m x', 'lf.txt')
        self.assertNotIn('deep.txt', reason)  # bash expands *.txt in the top folder only

    def test_powershell_leaves_an_add_glob_to_git(self):
        # PowerShell does not expand `*.txt` for a native command; git matches sub-folders too.
        self.put('sub/deep.txt', crlf(b'deep\nfile\n'))
        self.refused('git add *.txt; git commit -m x', 'sub/deep.txt', tool='PowerShell')


class Allowed(LineEndingGate):
    def test_an_add_glob_does_not_reach_sub_folders(self):
        self.put('sub/deep.txt', crlf(b'deep\nfile\n'))
        self.allowed('git add *.txt && git commit -m x')

    def test_a_comment_hides_what_follows_it(self):
        self.put('lf.txt', CRLF)
        self.allowed('git commit -m x # -a')

    def test_what_is_staged_counts_not_the_working_tree_file(self):
        self.put('lf.txt', b'one\ntwo\nthree\n')
        self.stage('lf.txt')
        self.put('lf.txt', CRLF)                      # flipped after staging; a plain commit stores LF
        self.allowed('git commit -m x')
        self.refused('git commit -am x', 'lf.txt (lf -> crlf)')
        self.stage('lf.txt')
        self.put('lf.txt', LF)                        # the working tree is back, the staged blob is not
        self.refused('git commit -m x', 'lf.txt (lf -> crlf)')

    def test_same_kind_passes(self):
        self.put('lf.txt', b'one\ntwo\nthree\n')
        self.put('crlf.txt', b'one\r\ntwo\r\nthree\r\n')
        self.put('mixed.txt', b'one\ntwo\r\n')
        self.stage('lf.txt', 'crlf.txt', 'mixed.txt')
        self.allowed('git commit -m x')
        self.allowed('git commit -am x')

    def test_eol_ok_lets_an_intended_change_through(self):
        self.put('lf.txt', CRLF)
        self.stage('lf.txt')
        self.allowed('git commit -m "x"  # eol-ok')
        self.allowed('git add lf.txt && git commit -am x # eol-ok')

    def test_new_deleted_binary_and_mixed_head_files_pass(self):
        self.put('new.txt', CRLF)
        self.put('bin.dat', b'\x00\x01head\r\nbody\r\n')
        self.put('declared.raw', CRLF)                # `binary` in .gitattributes
        self.put('mixed.txt', LF)
        (self.repo / 'gone.txt').unlink()
        git(self.repo, 'add', '-A')
        self.allowed('git commit -m x')
        self.allowed('git commit -am x')

    def test_a_line_without_an_ending_has_no_kind(self):
        self.put('lf.txt', b'one')
        self.stage('lf.txt')
        self.allowed('git commit -m x')

    def test_commands_that_store_nothing_pass(self):
        self.put('lf.txt', CRLF)
        self.stage('lf.txt')
        for command in ('git add lf.txt', 'git status', 'git diff --cached', 'echo "git commit -m x"',
                        'git commit --dry-run -m x', 'git log -1 --format=%s'):
            with self.subTest(command=command):
                self.allowed(command)

    def test_unstaged_file_not_named_is_not_stored(self):
        self.put('lf.txt', CRLF)
        self.put('other.txt', b'other\nmore\n')
        self.allowed('git add other.txt && git commit -m x')

    def test_first_commit_has_no_head_to_compare(self):
        fresh = self.base / 'fresh'
        fresh.mkdir()
        git(fresh, 'init', '-q')
        git(fresh, 'config', 'core.autocrlf', 'false')
        (fresh / 'a.txt').write_bytes(CRLF)
        git(fresh, 'add', 'a.txt')
        self.allowed('git commit -m x', cwd=fresh)

    def test_the_repository_is_not_written(self):
        self.put('lf.txt', b'content no object\r\nholds yet\r\n')  # blobs the repository lacks
        self.put('new.txt', b'a new file\n')
        git_dir = self.repo / '.git'
        index = (git_dir / 'index').read_bytes()
        objects = sorted(path for path in (git_dir / 'objects').rglob('*') if path.is_file())
        self.refused('git add -A && git commit -am x', 'lf.txt')
        self.assertEqual((git_dir / 'index').read_bytes(), index)
        self.assertEqual(sorted(path for path in (git_dir / 'objects').rglob('*') if path.is_file()), objects)
        self.assertEqual(git(self.repo, 'status', '--porcelain'), b' M lf.txt\n?? new.txt\n')

    def test_the_check_never_blocks_on_its_own_failure(self):
        self.put('lf.txt', CRLF)
        self.stage('lf.txt')
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git commit -m x'},
                              'cwd': str(self.repo)}).encode('utf-8')
        result = subprocess.run([sys.executable, str(SCRIPT), '--pre-tool-use'], input=payload,
                                capture_output=True, env={**ENV, 'PATH': str(self.elsewhere)},
                                cwd=self.elsewhere, timeout=60)
        self.assertEqual((result.returncode, result.stdout), (0, b''))  # no git on PATH
        with mock.patch.object(git_gate.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 7)):
            self.assertEqual(git_gate.eol_changes('git commit -m x', str(self.repo)), [])
        self.assertTrue(git_gate.eol_changes('git commit -m x', str(self.repo)))

    def test_200_staged_files_are_checked_well_inside_the_hook_timeout(self):
        body = b''.join(b'line %d\n' % number for number in range(300))
        for number in range(200):
            self.put(f'bulk/f{number:03d}.txt', body)
        self.stage('bulk')
        git(self.repo, 'commit', '-q', '-m', 'bulk')
        for number in range(200):
            self.put(f'bulk/f{number:03d}.txt', crlf(body))
        self.stage('bulk')
        timings = []
        for _ in range(3):
            start = time.perf_counter()
            reason = self.refused('git commit -m x', 'bulk/f000.txt (lf -> crlf)', 'and 192 more')
            timings.append(time.perf_counter() - start)
        self.assertLess(min(timings), 2.0, timings)


class NormalisingRepositories(LineEndingGate):
    """A repository that normalises endings stores them normalised: no false refusal, no skip."""
    autocrlf = 'true'

    def test_autocrlf_true_working_tree_crlf_is_stored_as_lf(self):
        self.put('lf.txt', crlf(b'one\ntwo\nthree\n'))  # what a checkout writes under autocrlf=true
        self.put('crlf.txt', b'one\r\ntwo\r\nthree\r\n')  # HEAD holds CRLF; git keeps it CRLF
        self.allowed('git commit -am x')
        self.allowed('git add . && git commit -m x')
        self.allowed('git add lf.txt crlf.txt && git commit -m x')

    def test_a_crlf_blob_staged_before_autocrlf_is_still_refused(self):
        self.put('lf.txt', CRLF)
        git(self.repo, '-c', 'core.autocrlf=false', 'add', 'lf.txt')  # the index really holds CRLF
        self.refused('git commit -m x', 'lf.txt (lf -> crlf)')
        self.allowed('git add --renormalize lf.txt && git commit -m x')  # renormalising stores LF

    def test_gitattributes_eol_lf_with_autocrlf_false(self):
        git(self.repo, 'config', 'core.autocrlf', 'false')
        self.put('.gitattributes', b'* text=auto eol=lf\n*.raw -text\n')
        git(self.repo, 'add', '.gitattributes')
        git(self.repo, 'commit', '-q', '-m', 'attributes')
        self.put('lf.txt', CRLF)
        self.allowed('git commit -am x')
        self.allowed('git add lf.txt && git commit -m x')


if __name__ == '__main__':
    unittest.main()
