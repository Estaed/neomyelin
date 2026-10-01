#!/usr/bin/env python3
"""Run the gate's personal-data grep over the staged content of the files in a commit."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402


def main() -> int:
    gate.force_utf8()
    names = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR'],
                           capture_output=True, text=True, encoding='utf-8', check=True).stdout.split('\n')
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
