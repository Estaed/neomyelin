"""Nightly launch, lock, and model selection."""
from __future__ import annotations

import datetime as dt
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

from helpers import make_vault, write

import engine
import nightly
import daily_commit
import memory_context
import render_hooks


class NightlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault, self.cfg = make_vault(self.base)
        self.cfg['nightly_at'] = '21:00'

    def test_due_session_detaches_once_and_finished_day_does_not_launch(self):
        scripts = self.base / 'scripts'
        write(scripts / 'nightly.py', '# nightly\n')
        now = dt.datetime(2026, 10, 1, 21, 5).astimezone()
        warnings = []
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(memory_context, 'SCRIPT_DIR', scripts), \
                mock.patch.object(memory_context, 'local_now', return_value=now), \
                mock.patch.object(memory_context.subprocess, 'Popen') as spawn:
            memory_context._start_nightly(self.cfg, warnings)
            memory_context._start_nightly(self.cfg, warnings)
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(warnings, [])
            state = self.vault / '.brain' / '.state'
            (state / 'nightly.starting').unlink()
            (state / 'nightly.last-run').write_text(now.isoformat(), encoding='utf-8')
            memory_context._start_nightly(self.cfg, warnings)
            self.assertEqual(spawn.call_count, 1)

    def test_nightly_lock_names_the_lock_and_keeps_steps_ordered(self):
        lock, log, stamp = nightly.paths(self.vault)
        lock.parent.mkdir(parents=True)
        lock.write_text('another process\n', encoding='utf-8')
        self.assertEqual(nightly.run(self.cfg), 0)
        self.assertIn('lock exists', log.read_text(encoding='utf-8'))
        self.assertFalse(stamp.exists())
        lock.unlink()
        scripts = self.base / 'scripts'
        write(scripts / 'a.py', 'import sys; sys.exit(1)\n')
        write(scripts / 'b.py', 'print("second ran")\n')
        with mock.patch.object(nightly, 'SCRIPT_DIR', scripts), \
                mock.patch.object(nightly, 'STEPS', (('first', ('a.py',)), ('missing', ('none.py',)),
                                                    ('second', ('b.py',)))):
            self.assertEqual(nightly.run(self.cfg), 1)
        text = log.read_text(encoding='utf-8')
        self.assertLess(text.index('first'), text.index('missing'))
        self.assertLess(text.index('missing'), text.index('second'))
        self.assertIn('second ran', text)
        self.assertTrue(stamp.exists())
        self.assertFalse(lock.exists())

    def test_engine_auto_uses_first_installed_and_marks_child(self):
        cfg = {**self.cfg, 'harnesses': ['codex', 'claude'], 'engine': 'auto'}
        completed = mock.Mock(returncode=0, stdout='answer\n', stderr='')
        found = {'claude': 'C:/bin/claude.exe'}
        with mock.patch.object(engine.shutil, 'which', side_effect=found.get), \
                mock.patch.object(engine.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(engine.ask('question', cfg=cfg), 'answer')
        self.assertEqual(run.call_args.args[0], ['C:/bin/claude.exe', '-p'])
        self.assertEqual(run.call_args.kwargs['input'], 'question')
        self.assertEqual(run.call_args.kwargs['env']['NEOMYELIN_INVOKED_BY'], 'engine')

    def test_engine_codex_runs_resolved_cmd_read_only_with_prompt_on_stdin(self):
        cfg = {**self.cfg, 'harnesses': ['codex'], 'engine': 'auto'}
        completed = mock.Mock(returncode=0, stdout='ok\n', stderr='')
        found = {'codex': 'C:/npm/codex.CMD'}
        with mock.patch.object(engine.shutil, 'which', side_effect=found.get), \
                mock.patch.object(engine.subprocess, 'run', return_value=completed) as run:
            engine.ask('line one\nline two', cfg=cfg)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], 'C:/npm/codex.CMD')
        self.assertIn('read-only', argv)
        self.assertEqual(argv[-1], '-')
        self.assertEqual(run.call_args.kwargs['input'], 'line one\nline two')

    def test_installed_session_start_returns_before_nightly_finishes_and_only_once(self):
        scripts = self.vault / '.brain' / 'scripts'
        scripts.mkdir(parents=True)
        for name in ('config.py', 'memory_context.py', 'nightly.py'):
            shutil.copyfile(Path(memory_context.__file__).with_name(name), scripts / name)
        cfg = {**self.cfg, 'nightly_at': '00:00'}
        write(self.vault / '.brain' / 'config.json', json.dumps(cfg))
        env = {key: value for key, value in os.environ.items() if key != 'NEOMYELIN_INVOKED_BY'}
        env['PYTHONUTF8'] = '1'

        def start():
            beginning = time.monotonic()
            result = subprocess.run([sys.executable, str(scripts / 'memory_context.py'), '--session-start'],
                                    input=json.dumps({'cwd': str(self.vault)}), cwd=self.vault,
                                    env=env, capture_output=True, text=True, encoding='utf-8', timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(time.monotonic() - beginning, 2)
            self.assertIn('[Memory: Identity]', result.stdout)

        start()
        log = self.vault / '.brain' / '.state' / 'nightly.log'
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.exists() and '[DONE]' in log.read_text(encoding='utf-8'):
                break
            time.sleep(0.02)
        self.assertIn('[DONE]', log.read_text(encoding='utf-8'))
        first = log.read_bytes()
        start()
        self.assertEqual(log.read_bytes(), first)

    def test_claude_env_gets_only_utf8_mode_and_codex_none(self):
        claude = render_hooks.merge('claude', {'env': {'MINE': 'x'}}, self.vault, sys.executable)
        self.assertEqual(claude['env'], {'MINE': 'x', 'PYTHONUTF8': '1'})
        self.assertEqual(render_hooks.merge('claude', claude, self.vault, sys.executable), claude)
        codex = render_hooks.merge('codex', {}, self.vault, sys.executable)
        self.assertNotIn('env', codex)

    def test_daily_commit_skips_a_non_git_vault(self):
        with mock.patch.object(daily_commit.config, 'load', return_value=self.cfg), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(daily_commit.run(), 0)
        self.assertIn('not a git repository', output.getvalue())

    def test_daily_commit_pushes_to_a_remote_added_after_init(self):
        # INSTALL.md adds the remote by hand, so the branch has no upstream and a bare `git push`
        # refuses; the first nightly push has to set it.
        if shutil.which('git') is None:
            self.skipTest('git is not installed')
        remote = self.base / 'remote.git'
        identity = {'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test',
                    'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test'}

        def git(*args, cwd=self.vault):
            return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                                  encoding='utf-8', check=True).stdout.strip()

        git('init', '--bare', str(remote), cwd=self.base)
        git('init')
        git('remote', 'add', 'origin', str(remote))
        write(self.vault / 'note.md', 'first\n')
        with mock.patch.dict(os.environ, identity), \
                mock.patch.object(daily_commit.config, 'load', return_value=self.cfg), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(daily_commit.run(), 0, output.getvalue())
            write(self.vault / 'note.md', 'second\n')
            self.assertEqual(daily_commit.run(), 0, output.getvalue())
        self.assertEqual(output.getvalue().count('daily push complete'), 2)
        self.assertEqual(git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD', cwd=remote))


class StepArguments(unittest.TestCase):
    """Every nightly step's script accepts the arguments nightly.py passes it.

    The doctor row once passed the original's `--kaydet` to a port that only knows `--save`; the
    step failed every night and no unit test saw it (found on Linux, 2026-10-01).
    """

    # Parse for real, then stop before the script does any work (no model call, no write):
    # `--help` would not do, argparse prints help before it reports an unknown argument.
    PARSE_ONLY = (
        'import argparse, os, runpy, sys\n'
        'sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))\n'
        'parse = argparse.ArgumentParser.parse_args\n'
        'def parse_then_stop(self, *a, **k):\n'
        '    parse(self, *a, **k)\n'
        '    raise SystemExit(0)\n'
        'argparse.ArgumentParser.parse_args = parse_then_stop\n'
        'sys.argv = sys.argv[1:]\n'
        'runpy.run_path(sys.argv[0], run_name="__main__")\n'
    )

    def test_every_step_parses_its_arguments(self):
        scripts = Path(nightly.__file__).resolve().parent
        for name, (script, *args) in nightly.STEPS:
            with self.subTest(step=name):
                path = scripts / script
                self.assertTrue(path.is_file(), f'{name}: {script} is missing')
                if not args or 'argparse' not in path.read_text(encoding='utf-8'):
                    continue  # no arguments to check, and running it would do real work
                result = subprocess.run([sys.executable, '-c', self.PARSE_ONLY, str(path), *args],
                                        capture_output=True, text=True, encoding='utf-8',
                                        errors='replace', timeout=60)
                self.assertEqual(result.returncode, 0, f'{name}: {result.stderr[-400:]}')


if __name__ == '__main__':
    unittest.main()
