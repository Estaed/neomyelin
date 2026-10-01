#!/usr/bin/env python3
"""The one gate. Run from the repo root: `python scripts/gate.py`. Non-zero on any failure.

1. Compiles every .py in the repo (in memory, no .pyc written).
2. Runs tests/unit and tests/acceptance, each folder in its own process (neither is a
   package, and both may hold a module of the same name).
3. Greps tracked and new files for personal data: the owner's drive path (DRIVE below), the
   owner's name, email addresses. AGENTS.md, notes.md, tasks/, docs/ and reports/ are the project's own
   records and are skipped.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TEST_DIRS = ('tests/unit', 'tests/acceptance')
SKIPPED = ('AGENTS.md', 'notes.md')
SKIPPED_DIRS = ('tasks/', 'docs/', 'reports/')
# Built from parts so this file does not flag itself.
DRIVE = 'D' + ':/'
NAME = re.compile('tar' + '[i\u0131]' + 'k', re.IGNORECASE)
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+' + '@' + r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass


def python_files(root: Path = ROOT) -> list[Path]:
    return sorted(path for path in root.rglob('*.py')
                  if not any(part.startswith('.') or part == '__pycache__'
                             for part in path.relative_to(root).parts[:-1]))


def compile_all(root: Path = ROOT) -> list[str]:
    errors = []
    for path in python_files(root):
        try:
            compile(path.read_bytes(), str(path), 'exec', dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            errors.append(f'{path.relative_to(root).as_posix()}: {exc}')
    return errors


def run_tests(root: Path = ROOT) -> list[str]:
    failures = []
    for folder in TEST_DIRS:
        if not (root / folder).is_dir():
            failures.append(f'{folder}: missing')
            continue
        print(f'--- {folder}', flush=True)
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', folder, '-t', folder],
                                cwd=root, env=_test_env())
        if result.returncode != 0:
            failures.append(f'{folder}: unittest exited {result.returncode}')
    return failures


def _test_env() -> dict[str, str]:
    return {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}


def skipped(relative: str) -> bool:
    return relative in SKIPPED or relative.startswith(SKIPPED_DIRS)


def personal_data(text: str) -> list[str]:
    hits = []
    if DRIVE in text:
        hits.append(DRIVE)
    hits += [match.group(0) for match in NAME.finditer(text)]
    hits += [match.group(0) for match in EMAIL.finditer(text)]
    return hits


def repo_files(root: Path = ROOT) -> list[str]:
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            cwd=root, capture_output=True)
    if result.returncode == 0:
        return sorted({name for name in result.stdout.decode('utf-8').split('\0') if name})
    # Not a git checkout (the release ZIP unpacked, or git missing): every file but caches counts.
    return sorted(path.relative_to(root).as_posix() for path in root.rglob('*')
                  if path.is_file() and not {'.git', '__pycache__', '.cache', 'dist'} & set(path.parts))


def grep_personal(root: Path = ROOT) -> list[str]:
    findings = []
    for relative in repo_files(root):
        path = root / relative
        if skipped(relative) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeError:
            continue  # binary
        for number, line in enumerate(text.splitlines(), 1):
            for hit in personal_data(line):
                findings.append(f'{relative}:{number}: {hit}')
    return findings


def main() -> int:
    force_utf8()
    problems = []
    errors = compile_all()
    print(f'compile: {len(python_files())} files, {len(errors)} errors')
    problems += errors
    problems += run_tests()
    findings = grep_personal()
    print(f'personal-data grep: {len(findings)} hits')
    problems += [f'personal data: {finding}' for finding in findings]
    if problems:
        print('\nGATE FAILED')
        for problem in problems:
            print(f'  {problem}')
        return 1
    print('\nGATE CLEAN')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
