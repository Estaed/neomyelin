#!/usr/bin/env python3
"""Turn-end reminders: a receipt, a knowledge note for each learning, and the reply's language.

    hook: python .brain/scripts/receipt_gate.py --harness claude|codex|agy --event <Event>

Session start context alone forgets mid-session; the receipt is what makes the vault
accumulate. The receipt reminder fires once per session, in and outside the vault, and never
again after it was shown, after a receipt carrying this session's value exists in `receipts/`, or
after a prompt holding the opt-out marker (OPT_OUT: `[no-record]`). Once that receipt exists and
holds a `**Learning**` item while no note under `knowledge/concepts/` was written or changed since
the session began, a second reminder asks for the note, once per session (the nightly knowledge
audit only catches it a day later). The language check (language_gate.py) asks, once per turn,
for a reply that slid into English to be rewritten in the language the user wrote in. Reminders
due at the same Stop go out as one block.

Claude Code and Codex (one shape for both):
    UserPromptSubmit   counts a human prompt; notes the opt-out and the prompt's language
    PostToolUse        counts a file edit (matcher Edit|Write|MultiEdit|NotebookEdit|apply_patch)
    Stop               3+ edits, or 2+ prompts, and no receipt; a receipt with a learning and no
                       note; or a reply in English (`last_assistant_message`) to a prompt that was
                       not: `{"decision": "block", "reason": ...}`; `stop_hook_active` (the Stop
                       that follows a block) always passes

Antigravity (`agy`) runs Stop after every agent loop, so only a Stop with `fullyIdle` (the turn
is over) counts as a turn. agy has no edit or prompt event this layer uses and its Stop output
has no verified way to show text, so after 2 idle turns without a receipt the reminder is
queued and agy_hook.py shows it once, through PreInvocation's `injectSteps`, at the start of
the next turn; the learning reminder is queued the same way. The language check needs the
prompt and the reply, which agy's events do not carry, so agy has none. The conversation id is
the session id.

State is a per-session append-only event file under .state/receipt_gate/ (parallel edits append,
nothing is read-modified-written); it is pruned after 7 days. Any error lets the turn end.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
import language_gate  # noqa: E402

STATE_DIR = SCRIPT_DIR / '.state' / 'receipt_gate'
STALE_SECONDS = 7 * 24 * 3600
EDITS = 3   # edits that make a session "changed files" (the original's threshold)
TURNS = 2   # prompts (Claude, Codex) or idle turns (agy) that make it "several turns"
# A prompt holding this marker: this session is not recorded.
OPT_OUT = ('[no-record]',)  # prompt_recall.OPT_OUT
INVOKED_ENV = 'NEOMYELIN_INVOKED_BY'  # memory_context.INVOKED_ENV: the layer's own processes
HARNESSES = ('claude', 'codex', 'agy')  # also brain.py's --harness names
EVENTS = ('UserPromptSubmit', 'PostToolUse', 'Stop')
TEXTS = {
    'edited': 'Files changed in this session and it has no receipt yet. If the work is done, write one now',
    'chat': ('This session ran several turns without changing files and has no receipt yet. If an '
             'answer or a decision worth keeping came out of it, write one now'),
    'turns': 'This conversation ran several turns and has no receipt yet. If something worth keeping came out of it, write one now',
}
LEARNING = ('[Memory: Learning] This session\'s receipt has a **Learning** item, but no note in '
            '`{vault}/knowledge/concepts/` was written or changed in this session. Each learning '
            'becomes a short note there plus its row in `knowledge/index.md`; if a note already '
            'covers it, update that note.')


def digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode('utf-8')).hexdigest()


def _events_file(key: str) -> Path:
    return STATE_DIR / f'{key}.events'


def record(key: str, kind: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with _events_file(key).open('a', encoding='utf-8', newline='\n') as handle:
        handle.write(f'{kind} {time.time():.0f}\n')


def history(key: str) -> tuple[Counter, float | None]:
    """(event kind -> count, time of the first event)."""
    counts: Counter = Counter()
    first = None
    try:
        lines = _events_file(key).read_text(encoding='utf-8').splitlines()
    except OSError:
        return counts, None
    for line in lines:
        kind, _, stamp = line.partition(' ')
        counts[kind] += 1
        try:
            first = min(first, float(stamp)) if first is not None else float(stamp)
        except ValueError:
            pass
    return counts, first


def _frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == '---')
        value = json.loads('\n'.join(lines[1:end]))
    except (StopIteration, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def find_receipt(vault: Path, session: str, since: float | None) -> Path | None:
    """A receipt (receipts/*.md, JSON frontmatter) carrying this session's value. Receipts are
    written once, so files older than the session's first event are not read."""
    floor = (since or 0) - 120
    for path in (vault / 'receipts').glob('*.md'):
        try:
            if path.stat().st_mtime < floor:
                continue
            if _frontmatter(path.read_text(encoding='utf-8-sig')).get('session') == session:
                return path
        except (OSError, UnicodeError):
            continue
    return None


def has_receipt(vault: Path, session: str, since: float | None) -> bool:
    return find_receipt(vault, session, since) is not None


def notes_changed(vault: Path, since: float | None) -> bool:
    """A note under knowledge/concepts/ written or changed since the session's first event; by any
    tool or shell, so its mtime decides, not the edit events. An unknown start never nags."""
    if since is None:
        return True
    for path in (vault / 'knowledge' / 'concepts').rglob('*.md'):
        try:
            if path.stat().st_mtime >= since - 2:
                return True
        except OSError:
            continue
    return False


def learning_reminder(key: str, vault: Path, receipt: Path, since: float | None) -> str:
    """Once per session: the receipt holds a learning and no note was written for it."""
    from knowledge_audit import learnings  # noqa: PLC0415 - only a session with a receipt needs it
    try:
        body = receipt.read_text(encoding='utf-8-sig').split('---', 2)[2]
    except (OSError, UnicodeError, IndexError):
        return ''
    if not learnings(body) or notes_changed(vault, since) or not _claim(STATE_DIR / f'{key}.learned'):
        return ''
    return LEARNING.format(vault=vault.as_posix())


def reminder(kind: str, harness: str, vault: Path, session: str) -> str:
    python = 'py -3' if os.name == 'nt' else 'python3'  # each platform's default launcher
    command = f'{python} "{vault.as_posix()}/brain.py" receipt --file <receipt.json> --harness {harness}'
    return (f'[Memory: Receipt] {TEXTS[kind]}: {command}, with "session": "{session}" in the JSON. '
            'If the work is not done, write it when it is; if the session is not worth a record, '
            'say so in one sentence and stop.')


def _claim(path: Path) -> bool:
    """Create the marker; False when it already existed (another Stop got there first)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8'):
            pass
    except FileExistsError:
        return False
    return True


def _cleanup() -> None:
    cutoff = time.time() - STALE_SECONDS
    try:
        for path in STATE_DIR.iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _session_id(harness: str, payload: dict) -> str:
    value = payload.get('conversationId' if harness == 'agy' else 'session_id')
    return value if isinstance(value, str) else ''


def on_prompt(payload: dict) -> None:
    from prompt_recall import prompt_text, human_turn  # noqa: PLC0415 - only this event needs it
    session_id = _session_id('claude', payload)
    prompt = prompt_text(payload)
    if not session_id or not prompt.strip():
        return
    key = digest(session_id)
    if any(marker in prompt for marker in OPT_OUT):
        record(key, 'optout')
    elif human_turn(payload, prompt):
        record(key, 'prompt')
    else:
        return
    # Overwritten by each human prompt: the language check judges the reply to the latest one.
    (STATE_DIR / f'{key}.lang').write_text(language_gate.prompt_language(prompt), encoding='utf-8',
                                            newline='\n')


def language_reminder(key: str, payload: dict, cfg: dict) -> str:
    try:
        verdict = (STATE_DIR / f'{key}.lang').read_text(encoding='utf-8').strip()
    except OSError:
        return ''
    if not language_gate.drifted(verdict, payload.get('last_assistant_message'), cfg.get('language')):
        return ''
    return language_gate.REASON.format(user=cfg.get('user_name') or 'the user')


def on_edit(payload: dict) -> None:
    session_id = _session_id('claude', payload)
    if session_id:
        record(digest(session_id), 'edit')


def record_reminders(harness: str, key: str, vault: Path) -> tuple[str, str]:
    """(receipt reminder, learning reminder); at most one of them is not ''."""
    counts, first = history(key)
    if counts['optout']:
        return '', ''
    session = key[:24]
    receipt = find_receipt(vault, session, first)
    if receipt is not None:
        return '', learning_reminder(key, vault, receipt, first)
    done = STATE_DIR / f'{key}.done'
    if done.exists():
        return '', ''
    if harness == 'agy':
        kind = 'turns' if counts['turn'] >= TURNS else ''
    else:
        kind = 'edited' if counts['edit'] >= EDITS else 'chat' if counts['prompt'] >= TURNS else ''
    if not kind or not _claim(done):
        return '', ''
    return reminder(kind, harness, vault, session), ''


def on_stop(harness: str, payload: dict) -> dict | None:
    """The Stop answer for Claude and Codex; agy's reminders are queued (see agy_reminder)."""
    if payload.get('stop_hook_active') is True:
        return None
    if harness == 'agy' and payload.get('fullyIdle') is not True:
        return None
    session_id = _session_id(harness, payload)
    if not session_id:
        return None
    key = digest(session_id)
    _cleanup()
    if harness == 'agy':
        record(key, 'turn')
    cfg = config.load()
    receipt_text, learning_text = record_reminders(harness, key, config.vault_path(cfg))
    if harness == 'agy':
        # Apart, because a receipt that arrives before the next turn cancels only its reminder.
        for suffix, text in (('pending', receipt_text), ('learning', learning_text)):
            if text:
                (STATE_DIR / f'{key}.{suffix}').write_text(text, encoding='utf-8', newline='\n')
        return None
    texts = [text for text in (language_reminder(key, payload, cfg), receipt_text, learning_text) if text]
    return {'decision': 'block', 'reason': '\n\n'.join(texts)} if texts else None


def _take(path: Path) -> str:
    try:
        text = path.read_text(encoding='utf-8')
        path.unlink()
    except OSError:
        return ''
    return text


def agy_reminder(conversation_id: object) -> str:
    """The queued agy reminders for this conversation, once; '' when there is none."""
    if not isinstance(conversation_id, str) or not conversation_id:
        return ''
    key = digest(conversation_id)
    text = _take(STATE_DIR / f'{key}.pending')
    try:
        if text and has_receipt(config.vault_path(config.load()), key[:24], history(key)[1]):
            text = ''
    except (config.ConfigError, OSError):
        pass
    return '\n\n'.join(part for part in (text, _take(STATE_DIR / f'{key}.learning')) if part)


def respond(harness: str, event: str, raw: str) -> dict | None:
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    if event == 'Stop':
        return on_stop(harness, payload)
    if harness != 'agy':
        (on_prompt if event == 'UserPromptSubmit' else on_edit)(payload)
    return None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # exit 2 would make the harness block the prompt or the stop
        self.print_usage(sys.stderr)
        print(f'{self.prog}: error: {message}', file=sys.stderr)
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--harness', choices=HARNESSES, required=True)
    parser.add_argument('--event', choices=EVENTS, required=True)
    args = parser.parse_args(argv)
    if os.environ.get(INVOKED_ENV):
        return 0
    try:
        raw = '' if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read().decode('utf-8', 'replace')
        answer = respond(args.harness, args.event, raw)
    except Exception:  # noqa: BLE001 - bookkeeping must never stop the session
        return 0
    if answer is not None:
        sys.stdout.write(json.dumps(answer, ensure_ascii=False) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
