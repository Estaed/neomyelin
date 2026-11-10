#!/usr/bin/env python3
"""Run the gate's personal-data grep over the staged content of the files in a commit, and refuse
Claude Code's local permissions file, which a forced `git add` would otherwise carry past .gitignore."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402

LOCAL_PERMISSIONS = '/.claude/settings.local.json'


def main() -> int:
    gate.force_utf8()
    # -z: without it git quotes a path with non-ASCII characters and the name would not match.
    names = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z'],
                           capture_output=True, text=True, encoding='utf-8', check=True).stdout.split('\0')
    local = [name for name in names if f'/{name}'.endswith(LOCAL_PERMISSIONS)]
    for name in local:
        print(f'local permissions file: {name}', file=sys.stderr)
    if local:
        print('commit refused: unstage it, approved commands can hold a pasted key', file=sys.stderr)
        return 1
    hits = []
    for name in filter(None, names):
        if gate.skipped(name):
            continue
        staged = subprocess.run(['git', 'show', f':{name}'], capture_output=True, check=False)
        try:
            text = staged.stdout.decode('utf-8')
        except UnicodeDecodeError:
            continue
        hits += [f'{name}: {hit}' for hit in gate.personal_data(text)]
    for hit in hits:
        print(f'personal data: {hit}', file=sys.stderr)
    if hits:
        print('commit refused: remove personal data from the staged files', file=sys.stderr)
    return 1 if hits else 0


if __name__ == '__main__':
    raise SystemExit(main())
