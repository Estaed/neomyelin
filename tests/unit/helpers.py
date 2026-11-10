"""Shared fixtures for the unit tests: a small vault and its config."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
for folder in (ROOT / 'brain' / 'scripts', ROOT / 'brain' / 'hooks', ROOT / 'scripts'):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

COMPANION = '850-Companion 🔮'


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def remove_tree(path: Path, patience: float = 60.0) -> None:
    """Remove a test's temporary folder once nothing holds it, git's read-only objects included.

    On Windows a folder that a process still runs in, or a file it holds open, cannot be removed:
    a detached child writing its last log line, a git the test left running on purpose. Removal is
    retried for `patience` seconds; past that the error is raised (a process that did not end).
    """
    def writable(function, name, _error) -> None:
        try:
            os.chmod(name, stat.S_IWRITE)  # git writes its objects read-only
            function(name)
        except FileNotFoundError:
            pass  # gone meanwhile

    handler = {'onexc': writable} if sys.version_info >= (3, 12) else {'onerror': writable}
    deadline = time.monotonic() + patience
    while os.path.lexists(path):
        try:
            shutil.rmtree(path, **handler)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def task(vault: Path, name: str, **metadata) -> Path:
    return write(vault / 'tasks' / f'{name}.md', '---\n' + json.dumps(metadata) + '\n---\nbody\n')


def receipt(vault: Path, name: str, body: str, **metadata) -> Path:
    return write(vault / 'receipts' / f'{name}.md', '---\n' + json.dumps(metadata, indent=2) + '\n---\n' + body + '\n')


def make_vault(base: Path, language: str = 'English') -> tuple[Path, dict]:
    vault = base / 'vault'
    write(vault / COMPANION / 'Rules.md', '---\ntitle: Rules\n---\n# Rules\n\nAlways cite the source.\n')
    write(vault / COMPANION / 'Core.md', '# Core\n\nThinking partner.\n')
    write(vault / COMPANION / 'Personality.md', '---\ntitle: Personality\n---\n# Personality\n\n- Likes tea. (taste, 2 evidence)\n')
    cfg = {'vault': str(vault), 'user_name': 'Alex', 'assistant_name': 'Nova',
           'language': language, 'harnesses': ['claude']}
    return vault, cfg


def write_config(base: Path, cfg: dict) -> Path:
    return write(base / 'config.json', json.dumps(cfg, ensure_ascii=False))
