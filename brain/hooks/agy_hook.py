#!/usr/bin/env python3
"""Antigravity (`agy`) PreInvocation hook: NeoMyelin's session context, once per conversation.

agy runs PreInvocation before every model call and sends `invocationNum`; 0 is a conversation's
first call, which this hook treats as SessionStart. Only that call gets the context, in agy's
output shape `{"injectSteps": [{"ephemeralMessage": text}]}`; every other call prints `{}`.
The receipt reminder matches receipts by sha256(conversationId)[:24], so the conversation id goes
to memory_context.py as `session_id`. Registered by .brain/scripts/render_hooks.py.

It also carries the receipt reminder receipt_gate.py queued at an idle Stop (agy's Stop output
has no verified way to show text): any call that finds one adds it, once.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
INVOKED_ENV = 'NEOMYELIN_INVOKED_BY'  # memory_context.INVOKED_ENV; read before importing it


def output(text: str) -> str:
    return json.dumps({'injectSteps': [{'ephemeralMessage': text}]}, ensure_ascii=False)


def hook_input(payload: dict) -> str:
    """The SessionStart-shaped input memory_context.py reads."""
    session = payload.get('conversationId')
    data = {'session_id': session if isinstance(session, str) else ''}
    cwd = payload.get('cwd')  # not seen in agy's payload yet; memory_context falls back to the process cwd
    if isinstance(cwd, str) and cwd:
        data['cwd'] = cwd
    return json.dumps(data)


def session_context(payload: dict) -> str:
    try:
        import memory_context
        hook = json.loads(memory_context.session_start_context(hook_input(payload), emit=False))
        return hook['hookSpecificOutput']['additionalContext']
    except Exception as exc:  # noqa: BLE001 - a broken hook must say so in the session, not vanish
        return f'[Memory warning] NeoMyelin session context failed: {type(exc).__name__}: {exc}'


def receipt_reminder(payload: dict) -> str:
    try:
        import receipt_gate
        return receipt_gate.agy_reminder(payload.get('conversationId'))
    except Exception:  # noqa: BLE001 - bookkeeping must never cost the turn
        return ''


def respond(raw: str) -> str:
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        return '{}'
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    texts = [session_context(payload)] if payload.get('invocationNum') == 0 else []
    reminder = receipt_reminder(payload)
    if reminder:
        texts.append(reminder)
    return output('\n\n'.join(texts)) if texts else '{}'


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    if os.environ.get(INVOKED_ENV):
        sys.stdout.write('{}\n')
        return 0
    raw = '' if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read().decode('utf-8', 'replace')
    sys.stdout.write(respond(raw) + '\n')
    sys.stdout.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
