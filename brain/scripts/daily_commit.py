#!/usr/bin/env python3
"""Commit vault changes daily, then push when a remote exists."""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys

import config


FORBIDDEN = re.compile(r'(^|/)\.claude/settings\.local\.json$|(^|/)\.env$|'
                       r'(^|/)\.brain/\.state/|(^|/)\.brain/\.backup/|\.bak$|\.orig$')
EXCLUDES = (':(exclude).claude/settings.local.json', ':(exclude)**/.env',
            ':(exclude).brain/.state/**', ':(exclude).brain/.backup/**', ':(exclude)**/*.bak',
            ':(exclude)**/*.orig')


def run() -> int:
    vault = config.vault_path(config.load())
    if not (vault / '.git').exists():
        print('daily commit skipped: vault is not a git repository')
        return 0

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['git', '-C', str(vault), *args], capture_output=True,
                              text=True, encoding='utf-8', errors='replace', check=False)

    # Runtime state and install backups (.brain/.backup/: skills as they were before adoption)
    # are local artifacts, not vault notes.
    added = git('add', '-A', '--', '.', *EXCLUDES)
    if added.returncode:
        print(f'daily commit failed: {added.stderr.strip()}')
        return 1
    staged = git('diff', '--cached', '--name-only')
    if staged.returncode:
        print(f'daily commit failed: {staged.stderr.strip()}')
        return 1
    names = staged.stdout.splitlines()
    forbidden = [name for name in names if FORBIDDEN.search(name.replace('\\', '/'))]
    if forbidden:
        git('reset', '--', *forbidden)
        print(f'daily commit refused protected files: {", ".join(forbidden)}')
        return 1
    if names:
        result = git('commit', '-m', f'Daily vault update {dt.date.today().isoformat()}')
        if result.returncode:
            print(f'daily commit failed: {result.stderr.strip()}')
            return 1
        print(f'daily commit: {len(names)} files')
    else:
        print('daily commit: no changes')
    remote = git('remote')
    if remote.returncode or not remote.stdout.strip():
        print('daily push skipped: no remote')
        return 0
    branch = git('symbolic-ref', '--quiet', '--short', 'HEAD')
    if branch.returncode:
        print('daily push skipped: no current branch')
        return 0
    pushed = git('push')
    if pushed.returncode:
        print(f'daily push failed: {pushed.stderr.strip()}')
        return 1
    print('daily push complete')
    return 0


if __name__ == '__main__':
    config.force_utf8()
    try:
        raise SystemExit(run())
    except (config.ConfigError, OSError) as exc:
        print(f'daily commit failed: {exc}', file=sys.stderr)
        raise SystemExit(1) from None
