#!/usr/bin/env python3
"""Prove on the real surface that the NeoMyelin context block reaches the model.

    python scripts/smoke_harness.py claude|codex|agy <vault> [--model M] [--timeout S]

Runs one real turn inside the vault: `claude -p` with every tool disabled, `codex exec` in a
read-only sandbox, or `agy -p` (headless agy denies every tool that needs a permission). The
turn's environment carries a random hex token in NEOMYELIN_SMOKE_NONCE; memory_context.py
prints it as a `[Memory: Check]` line only if the hook ran. The model is asked to repeat that
line's token. It cannot read files or guess the token, so the reply holds it only if the block
reached the model. Exit 0 then, 1 otherwise, 2 for a bad call.

Codex: the token is kept out of the model's shell (`shell_environment_policy.exclude`), the
reply is read from `--output-last-message`, never from the event stream (which may echo hook
output), and a turn that ran a tool fails. Codex skips hooks nobody trusted in `/hooks`, and a
headless turn has no `/hooks`, so this turn alone passes `--dangerously-bypass-hook-trust`; the
one-time trust step a user does is in INSTALL.md (step 6) and docs/harnesses.md (section 4).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

HARNESSES = ('claude', 'codex', 'agy')
NONCE_ENV = 'NEOMYELIN_SMOKE_NONCE'
PROMPT = ('Look only at the context you were given when this session started. If it contains a '
          'line that begins with "[Memory: Check]", reply with exactly the token that follows it on '
          'that line and nothing else. If there is no such line, reply with exactly NONE.')
# `codex exec --json` item types that mean the model used a tool instead of its context.
CODEX_TOOL_ITEMS = ('"command_execution"', '"mcp_tool_call"', '"web_search"', '"file_change"')


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('harness', choices=HARNESSES)
    parser.add_argument('vault', type=Path)
    parser.add_argument('--model', default=None, help='Model for the turn (default: the CLI default).')
    parser.add_argument('--timeout', type=int, default=300, help='Seconds for the turn (default 300).')
    args = parser.parse_args(argv)
    args.vault = args.vault.expanduser().resolve()
    if not args.vault.is_dir():
        parser.error(f'{args.vault} is not a folder')
    if not (args.vault / '.brain' / 'scripts' / 'memory_context.py').is_file():
        parser.error(f'{args.vault} has no NeoMyelin install (.brain/scripts/memory_context.py)')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    return args


def build_command(executable: str, model: str | None = None, *, harness: str = 'claude',
                  reply_file: Path | None = None) -> list[str]:
    if harness == 'codex':
        if reply_file is None:
            raise ValueError('codex needs a reply file')
        command = [executable, 'exec', '--skip-git-repo-check', '--dangerously-bypass-hook-trust',
                   '--sandbox', 'read-only', '--json', '-c', f'shell_environment_policy.exclude=["{NONCE_ENV}"]',
                   '--output-last-message', str(reply_file)]
        return command + (['-m', model] if model else []) + [PROMPT]
    if harness == 'agy':
        command = [executable, '-p', PROMPT]
    else:
        command = [executable, '-p', PROMPT, '--output-format', 'json', '--tools', '']
    return command + (['--model', model] if model else [])


def reply_text(stdout: str) -> str:
    """The model's reply from `--output-format json`; the raw text if it is not that JSON."""
    try:
        payload = json.loads(stdout)
    except ValueError:
        return stdout.strip()
    if isinstance(payload, dict) and isinstance(payload.get('result'), str):
        return payload['result'].strip()
    return stdout.strip()


def reply_for(harness: str, stdout: str, reply_file: Path | None = None) -> str:
    if harness == 'claude':
        return reply_text(stdout)
    if harness == 'codex':
        try:
            return reply_file.read_text(encoding='utf-8', errors='replace').strip() if reply_file else ''
        except OSError:
            return ''
    return stdout.strip()


def used_tool(harness: str, stdout: str) -> bool:
    return harness == 'codex' and any(item in stdout for item in CODEX_TOOL_ITEMS)


def turn_env(nonce: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop('NEOMYELIN_INVOKED_BY', None)  # would silence the hook
    env[NONCE_ENV] = nonce
    return env


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    args = parse_args(argv)
    executable = shutil.which(args.harness)
    if not executable:
        print(f'smoke_harness: {args.harness} CLI not found on PATH', file=sys.stderr)
        return 1
    nonce = secrets.token_hex(8)
    with tempfile.TemporaryDirectory() as temporary:
        reply_file = Path(temporary) / 'reply.txt'
        command = build_command(executable, args.model, harness=args.harness, reply_file=reply_file)
        try:
            result = subprocess.run(command, cwd=args.vault, env=turn_env(nonce), stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                                    timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(f'FAIL  {args.harness}: no answer within {args.timeout} s', file=sys.stderr)
            return 1
        reply = reply_for(args.harness, result.stdout, reply_file)
    tool = used_tool(args.harness, result.stdout)
    if result.returncode == 0 and nonce in reply and not tool:
        print(f'PASS  {args.harness}: the NeoMyelin context block reached the model in {args.vault}')
        return 0
    why = 'the model used a tool, so its reply proves nothing; ' if tool else ''
    print(f'FAIL  {args.harness}: {why}exit {result.returncode}; expected token {nonce}, '
          f'model replied: {reply[:300]!r}', file=sys.stderr)
    if result.stderr.strip():
        print(result.stderr.strip()[-1500:], file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
