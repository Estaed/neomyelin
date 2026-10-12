#!/usr/bin/env python3
"""Update a task's metadata at its current revision, read first through `brain.py history`.

    python .brain/scripts/task_update.py <id> status=done next_action="..."
    python .brain/scripts/task_update.py <id> --json '{"status": "done"}'
    ... --mine   the task file has uncommitted changes and they are this session's own

A bare `brain.py task-update` needs the revision the file holds now; this reads it (the last
history entry is always the file as it is) and sends the update with it.

A task file with uncommitted changes (changed, staged or new in the git repository holding the
vault, its own or a parent one) may be another live session's work in progress: one session once
rewrote a task another session was working on without asking. Such a file is refused (exit 3)
until that session is asked; `--mine` says the changes are this session's own. A vault outside
any repository, or one its parent repository has never committed, has no uncommitted state, and
its tasks update as before.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import config

MINE = '--mine'


def _call(vault: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    return subprocess.run([sys.executable, str(vault / 'brain.py'), *args], cwd=vault,
                          env=env, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=120, check=False)


def _file_revision(vault: Path, task_id: str) -> str:
    try:
        text = (vault / 'tasks' / f'{task_id}.md').read_text(encoding='utf-8')
        return str(json.loads(text.split('---', 2)[1]).get('revision'))
    except (OSError, ValueError, IndexError, TypeError):
        return '?'


def _uncommitted(vault: Path, task_id: str) -> bool:
    """The task file differs from the last commit of the repository holding the vault (changed,
    staged or new). Outside any repository git prints nothing, so the update goes on."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(['git', '--no-optional-locks', *args], cwd=vault,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=30, check=False)
    try:
        status = git('status', '--porcelain', '--', f'tasks/{task_id}.md').stdout.strip()
        if not status.startswith('??'):
            return bool(status)
        # A new file counts only where the vault folder is in the last commit: a parent repository
        # that never committed the vault lists every task as new, and nothing would ever update.
        return git('rev-parse', '--verify', '--quiet', 'HEAD:./').returncode == 0
    except (OSError, subprocess.SubprocessError):  # no git: nothing to tell, update as before
        return False


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mine = MINE in args
    args = [item for item in args if item != MINE]
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 2
    task_id, rest = args[0], args[1:]
    try:
        if rest[:1] == ['--json'] and len(rest) == 2:
            changes = json.loads(rest[1])
        else:
            changes = {}
            for item in rest:
                key, separator, value = item.partition('=')
                if not separator:
                    print(f'expected key=value: {item}', file=sys.stderr)
                    return 2
                changes[key] = value
        if not isinstance(changes, dict) or not changes:
            print('no changes provided', file=sys.stderr)
            return 2
        cfg = config.load()
        vault = config.vault_path(cfg)
    except (ValueError, config.ConfigError) as exc:
        print(f'task update: {exc}', file=sys.stderr)
        return 2
    if not mine and _uncommitted(vault, task_id):
        print(f'tasks/{task_id}.md has uncommitted changes; another live session may be working on '
              'it.\nAsk that session first (in Claude Code ListAgents finds it and SendMessage asks '
              'it; otherwise ask the user).\nIf the changes are this session\'s own (you just fixed '
              f'the body), rerun the same command with {MINE}.', file=sys.stderr)
        return 3

    history = _call(vault, 'history', task_id)
    try:
        revision = json.loads(history.stdout)[-1]['revision']
    except (ValueError, IndexError, KeyError, TypeError):
        print(f'{task_id}: task not found or history unreadable\n{history.stdout}{history.stderr}',
              file=sys.stderr)
        return 1

    temp_dir = vault / '.tmp'
    temp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='task-update-', suffix='.json', dir=temp_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump({'id': task_id, 'expected_revision': revision, 'changes': changes},
                      handle, ensure_ascii=False)
        result = _call(vault, 'task-update', '--file', name)
    finally:
        Path(name).unlink(missing_ok=True)
    if result.returncode:
        print(f'task-update failed at revision {revision} (file revision '
              f'{_file_revision(vault, task_id)}):\n{result.stdout}{result.stderr}', file=sys.stderr)
        return 1
    print(f'{task_id}: revision {revision} -> {revision + 1}; updated {", ".join(changes)}')
    return 0


if __name__ == '__main__':
    config.force_utf8()
    raise SystemExit(main())
