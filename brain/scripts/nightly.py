#!/usr/bin/env python3
"""Run the vault's nightly maintenance sequence once at a time."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import subprocess
import sys

import config


SCRIPT_DIR = Path(__file__).resolve().parent
STEPS = (
    ('habits', ('habits.py',)),
    ('evolution', ('patterns.py', 'run')),
    ('skill-candidates', ('gardener.py', 'candidates')),
    ('recall', ('recall.py', '--update')),
    ('knowledge-audit', ('knowledge_audit.py',)),
    ('daily-commit', ('daily_commit.py',)),
    ('doctor', ('doctor.py', '--save')),
)


def paths(vault: Path) -> tuple[Path, Path, Path]:
    state = vault / '.brain' / '.state'
    return state / 'nightly.lock', state / 'nightly.log', state / 'nightly.last-run'


def log(path: Path, status: str, name: str, detail: str = '') -> None:
    line = f'{dt.datetime.now().astimezone().isoformat(timespec="seconds")} [{status}] {name}'
    if detail:
        line += f': {detail.replace(chr(10), " ")[:500]}'
    with path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write(line + '\n')
    print(line, flush=True)


def run(cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else config.load()
    vault = config.vault_path(cfg)
    lock, log_path, stamp = paths(vault)
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock.open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(str(os.getpid()) + '\n')
    except FileExistsError:
        log(log_path, 'SKIP', 'nightly', f'lock exists: {lock}')
        return 0
    failures = 0
    try:
        log(log_path, 'START', 'nightly')
        env = os.environ.copy()
        env.update(NEOMYELIN_INVOKED_BY='nightly', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        for name, command in STEPS:
            script = SCRIPT_DIR / command[0]
            if not script.is_file():
                log(log_path, 'SKIP', name, f'{command[0]} is not installed')
                continue
            try:
                result = subprocess.run([sys.executable, str(script), *command[1:]],
                                        cwd=vault, env=env, capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', check=False)
                detail = '\n'.join((result.stdout + '\n' + result.stderr).splitlines()[-3:]).strip()
                if result.returncode:
                    failures += 1
                log(log_path, 'OK' if result.returncode == 0 else 'ERROR', name,
                    detail or f'exit {result.returncode}')
            except OSError as exc:
                failures += 1
                log(log_path, 'ERROR', name, str(exc))
        stamp.write_text(dt.datetime.now().astimezone().isoformat(timespec='seconds') + '\n',
                         encoding='utf-8', newline='\n')
        log(log_path, 'DONE' if failures == 0 else 'DONE WITH ERRORS', 'nightly',
            f'{failures} failed')
        return 1 if failures else 0
    finally:
        lock.unlink(missing_ok=True)
        (lock.parent / 'nightly.starting').unlink(missing_ok=True)


if __name__ == '__main__':
    config.force_utf8()
    try:
        raise SystemExit(run())
    except (config.ConfigError, OSError) as exc:
        print(f'nightly: {exc}', file=sys.stderr)
        raise SystemExit(1) from None
