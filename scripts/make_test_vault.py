#!/usr/bin/env python3
"""Make an empty vault folder for tests and smoke runs.

    python scripts/make_test_vault.py <dir>

NeoMyelin installs from an empty folder (`install.py --vault <dir>`), so a test vault is a new,
empty folder. A path that exists and is not an empty folder is refused, so a test never installs
over real notes. Prints {"vault": "<absolute path>"}.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass


def check_target(target: Path) -> None:
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f'{target} exists and is not an empty folder; pick a new one')


def make(target: Path) -> dict:
    target = target.expanduser().resolve()
    check_target(target)
    target.mkdir(parents=True, exist_ok=True)
    return {'vault': str(target)}


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('dir', type=Path, help='New, empty vault folder.')
    args = parser.parse_args(argv)
    try:
        summary = make(args.dir)
    except (ValueError, OSError) as exc:
        print(f'make_test_vault: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
