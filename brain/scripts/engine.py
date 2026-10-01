#!/usr/bin/env python3
"""Headless model calls for background jobs.

ask(prompt, *, engine=None, timeout=300, cfg=None) -> str returns stdout or raises
RuntimeError/TimeoutExpired. `engine` overrides config `engine`; `auto` chooses the first
installed CLI in configured harness order. A child call carries NEOMYELIN_INVOKED_BY so its
session hooks load no memory and do not start another nightly run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import config


def ask(prompt: str, *, engine: str | None = None, timeout: int = 300,
        cfg: dict | None = None) -> str:
    """Return a headless CLI's reply; see module docstring for the stable signature."""
    cfg = cfg if cfg is not None else config.load()
    choice = engine or cfg.get('engine', 'auto')
    if choice == 'auto':
        choice = next((name for name in cfg['harnesses'] if shutil.which(name)), None)
    exe = shutil.which(choice) if choice in config.HARNESSES else None
    if exe is None:
        raise RuntimeError(f'model CLI unavailable: {choice}')
    # The resolved path, not the bare name: on Windows npm installs `codex.cmd`, which
    # CreateProcess does not find by name. Claude and Codex take the prompt on stdin, because a
    # multi-line argument breaks at the first newline when it passes through a .cmd file.
    # Codex runs read-only: a background judge never writes files.
    argv, stdin = {'claude': ([exe, '-p'], prompt),
                   'codex': ([exe, 'exec', '--skip-git-repo-check', '--sandbox', 'read-only', '-'],
                             prompt),
                   'agy': ([exe, '-p', prompt], None)}[choice]
    env = os.environ.copy()
    env['NEOMYELIN_INVOKED_BY'] = 'engine'
    env['PYTHONUTF8'] = '1'
    result = subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=timeout, env=env,
                            cwd=config.vault_path(cfg), check=False)
    if result.returncode:
        raise RuntimeError(f'{choice} exited {result.returncode}: {result.stderr.strip()[:300]}')
    return result.stdout.strip()


if __name__ == '__main__':
    config.force_utf8()
    print('engine.py is called by other scripts', file=sys.stderr)
    raise SystemExit(1)
