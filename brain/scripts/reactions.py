#!/usr/bin/env python3
"""Nightly reaction audit: was the user's praise or objection written down where it lasts?

A method the user liked belongs in a skill or a note, an objection in the rules, a skill or a
task; left to the agent in the session, that step was the one it missed (measured on the
original brain: about 24 clear reactions in nine days, 7 recorded). After the habit scan, this
step gives a model the user's own messages since the last scan together with the same window's
commits (the vault and every repository under `projects_root`) and receipts. The model names
each reaction that carries a durable lesson, quoting it word for word, and says whether the
lesson landed somewhere. The script checks every quote against the messages and keeps the
unhandled ones in `.brain/.state/reactions.json`; the session start shows them as
`[reaction debt]` until a session handles one and runs `reactions.py close <id> "<where>"`.

    reactions.py                  the nightly run
    reactions.py --dry-run        print the prompt, no model call
    reactions.py --no-write       call the model, print what it found, record nothing
    reactions.py --since DAY      with --dry-run or --no-write: start from DAY (a trial)
    reactions.py list             the open debt
    reactions.py close <id> "<where it went>"
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import config
import evidence
import habits
import stage

STATE = '.brain/.state/reactions.json'
SETTLE = dt.timedelta(hours=2)        # a newer message waits: its session may still handle it
FIRST_RUN = dt.timedelta(days=1)      # how far back the first run looks
MAX_MESSAGE = 600
# Character budgets (about 60k tokens in all). When the messages overflow, the newest wait for the
# next night: `last` advances only to the last message in the prompt, so none is skipped.
MAX_MESSAGES = 80_000
MAX_COMMITS = 70_000
MAX_RECEIPTS = 70_000
MAX_RECEIPT_BODY = 900
MAX_REACTIONS = 8
OPT_OUT = ('[no-record]',)            # receipt_gate.OPT_OUT
SKIPPED_PREFIXES = ('[Request interrupted',)
HABIT_EVENT = '-neomyelin-habit'      # the nightly habit receipts are not a session's work
MARKED = ('OBSERVATION', 'REACTION', 'CORRECTION')


class StateError(Exception):
    """The state file exists but cannot be trusted; writing over it would lose open debt."""


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _instant(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone()


def _state_path(cfg: dict) -> Path:
    return config.vault_path(cfg) / STATE


def read_state(cfg: dict) -> dict:
    """An empty state when there is no file; StateError when it is unreadable or malformed."""
    path = _state_path(cfg)
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise StateError(f'{path.name} unreadable: {exc}') from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise StateError(f'{path.name} is not JSON: {exc}') from exc
    open_items = payload.get('open', []) if isinstance(payload, dict) else None
    if (not isinstance(open_items, list) or not isinstance(payload.get('closed', {}), dict)
            or not all(isinstance(item, dict) and isinstance(item.get('id'), str)
                       and isinstance(item.get('quote'), str) for item in open_items)):
        raise StateError(f'{path.name} is not in the expected shape')
    if 'last' in payload:
        try:
            _instant(str(payload['last']))
        except ValueError as exc:
            raise StateError(f"{path.name}: unreadable last {payload['last']!r}") from exc
    return payload


def write_state(cfg: dict, payload: dict) -> None:
    stage.atomic_write(_state_path(cfg), json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def messages(start: dt.datetime, end: dt.datetime, cfg: dict,
             home: Path | None = None) -> list[tuple[str, str, str]]:
    """The user's typed messages in (start, end], oldest first."""
    days = {start.date() + dt.timedelta(days=n) for n in range((end.date() - start.date()).days + 1)}
    found = habits.messages(days, cfg, home)
    return sorted((item for item in found if start < _instant(item[0]) <= end
                   and not any(marker in item[2] for marker in OPT_OUT)
                   and not item[2].startswith(SKIPPED_PREFIXES)),
                  key=lambda item: _instant(item[0]))


def _git_log(repo: Path, start: dt.datetime) -> str:
    try:
        result = subprocess.run(
            ['git', '-c', 'core.quotepath=false', 'log', f'--since={start.isoformat()}', '--name-only',
             '--format=@@ %h %ad %s', '--date=format:%m-%d %H:%M'],
            cwd=repo, capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ''
    if result.returncode:
        return ''
    # One line per commit: its subject and at most six files; receipts and daily views are no signal.
    lines: list[str] = []
    files: list[str] = []

    def finish() -> None:
        if lines and files:
            more = f' (+{len(files) - 6})' if len(files) > 6 else ''
            lines[-1] += ' — ' + ', '.join(files[:6]) + more
        files.clear()

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith('@@'):
            finish()
            lines.append(line[3:])
        elif line and not line.startswith(('receipts/', 'daily/')):
            files.append(line)
    finish()
    return '\n'.join(lines)


def commits(start: dt.datetime, cfg: dict) -> str:
    vault = config.vault_path(cfg)
    repos = [vault]
    projects = config.projects_root(cfg)
    if projects is not None:
        try:
            repos += sorted(path for path in projects.iterdir()
                            if (path / '.git').exists() and path.resolve() != vault)
        except OSError:
            pass
    blocks = []
    for repo in repos:
        log = _git_log(repo, start)
        if log:
            blocks.append(f'### {repo.name}\n{log}')
    return '\n\n'.join(blocks) or '(no commits)'


def receipts(start: dt.datetime, cfg: dict) -> str:
    blocks = []
    for path in (config.vault_path(cfg) / 'receipts').glob('*.md'):
        parsed = evidence.read_receipt(path)
        if parsed is None:
            continue
        metadata, lines = parsed
        try:
            created = _instant(str(metadata.get('created_at')))
        except ValueError:
            continue
        if created <= start or str(metadata.get('event_id', '')).endswith(HABIT_EVENT):
            continue
        body = '\n'.join(lines[lines.index('---', 1) + 1:]).strip().splitlines()
        # Cut on a line boundary: marked lines are the evidence itself and may sit past the cut,
        # so they are kept whole, never split (a line across the cut lost its quote).
        kept: list[str] = []
        size = 0
        for line in body:
            if size + len(line) > MAX_RECEIPT_BODY:
                break
            kept.append(line)
            size += len(line) + 1
        if not kept and body:          # a first line longer than the budget is clipped
            kept = [body[0][:MAX_RECEIPT_BODY]]
        rest = body[len(kept):]
        marked = [line for line in rest if line.lstrip('-* ').startswith(MARKED)]
        if rest or (body and len(kept[0]) < len(body[0])):
            kept[-1] += '…'
        blocks.append((created, '\n'.join(kept + marked)))
    # Oldest first; over the budget the newest stay (handling comes after the reaction).
    text = ''
    for _, body in sorted(blocks, key=lambda item: item[0], reverse=True):
        if len(text) + len(body) > MAX_RECEIPTS:
            break
        text = body + '\n\n' + text
    return text.strip() or '(no receipts)'


def prompt(lines: list[str], commit_text: str, receipt_text: str, open_items: list[dict],
           cfg: dict) -> str:
    user, assistant = cfg['user_name'], cfg['assistant_name']
    companion = config.companion_dir(cfg)
    rules = f'`{companion.name}/Rules.md`' if companion else '`Rules.md`'
    known = '\n'.join(f'- {item["quote"]}' for item in open_items) or '(none)'
    return f"""Write in {cfg['language']}. Below are {user}'s own messages, the commits made in the same period and the session records (receipts). {user} works with an AI assistant, {assistant}, whose second brain keeps the rules ({rules}), skills, notes (`knowledge/concepts/`) and tasks (`tasks/`).

Your job: find {user}'s REACTIONS to a result or a behaviour of {assistant}, and decide for each whether its lesson was written somewhere it lasts.
- `praise`: {user} liked a result, a method or a style.
- `objection`: {user} disliked or corrected something, or asked why {assistant} did or did not do something.
Take only a reaction that carries a DURABLE lesson: a preference, method or mistake that will hold for the same kind of work next time. Leave out plain approval ("yes, do it", "ok"), questions, new requests, a one-off choice between options that was applied in that work, {user}'s own mistakes, and reactions to anything other than {assistant}.

`handled`: true when the lesson shows in the commits (a skill, the rules, a hook or script, a note or a task file changed for it) or in a receipt (an item that handles it, or a `CORRECTION:`/`REACTION:` line quoting it); put the commit hash or the receipt title in `evidence`. False when it was only applied in the work itself and written nowhere the next session would read. When unsure, false: the next session checks the debt.

Rules:
- Every reaction quotes the messages WORD FOR WORD: at least {evidence.MIN_QUOTE} characters, no double quotes, a piece that appears exactly in one message, in the message's own language.
- `lesson`: one sentence, what was liked or objected to and what to do next time.
- `where`: where the lesson should go (for example `skill:<name>`, `Rules`, `knowledge`, `tasks/<id>`).
- At most {MAX_REACTIONS} unhandled and {MAX_REACTIONS} handled reactions; pick the clearest. An empty list is a valid answer.
- Do not repeat the debts already open below.
Reply with only a JSON array, nothing else:
[{{"kind": "praise|objection", "quote": "<exact piece>", "lesson": "<one sentence>", "where": "<suggestion>", "handled": false, "evidence": "<hash, receipt title or empty>"}}]

## Debts already open
{known}

## {user}'s messages
{chr(10).join(lines)}

## Commits (same period and after)
{commit_text}

## Receipts (same period and after)
{receipt_text}
"""


def verified(proposals: list, texts: list[tuple[str, str]],
             known: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """Unhandled reactions whose quote appears word for word in a message; the day is the message's.
    Ids in `known` (already open or closed) are skipped before the cap, so repeats never crowd
    out a new one."""
    pool = [(stamp, evidence.normalize(text)) for stamp, text in texts]
    output: list[dict] = []
    for item in proposals:
        if not isinstance(item, dict) or item.get('handled') is not False:
            continue
        kind = item.get('kind')
        quote = evidence.normalize(str(item.get('quote', ''))).strip(' .,;:!?…')
        lesson = ' '.join(str(item.get('lesson', '')).split())
        if kind not in ('praise', 'objection') or not lesson or '"' in quote or len(quote) < evidence.MIN_QUOTE:
            continue
        stamp = next((stamp for stamp, text in pool if quote in text), None)
        item_id = hashlib.sha1(quote.encode('utf-8')).hexdigest()[:8]
        if stamp is None or item_id in known or any(found['id'] == item_id for found in output):
            continue
        output.append({'id': item_id, 'day': _instant(stamp).date().isoformat(), 'kind': kind,
                       'quote': quote, 'lesson': lesson,
                       'where': ' '.join(str(item.get('where', '')).split())})
        if len(output) == MAX_REACTIONS:
            break
    return output


def _prompt_lines(items: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """(stamp, line) pairs from the oldest on, up to the message budget."""
    output = []
    total = 0
    for stamp, source, text in items:
        line = f"[{_instant(stamp):%m-%d %H:%M} {source}] {' '.join(text.split())[:MAX_MESSAGE]}"
        if output and total + len(line) > MAX_MESSAGES:
            break
        output.append((stamp, line))
        total += len(line) + 1
    return output


def run(cfg: dict, *, dry_run: bool = False, no_write: bool = False,
        since: dt.datetime | None = None, home: Path | None = None) -> tuple[bool, str]:
    try:
        state = read_state(cfg)
    except StateError as exc:
        return False, f'{exc}; left untouched to keep the open debt, fix the file'
    end = _now() - SETTLE
    try:
        start = _instant(state['last'])
    except (KeyError, TypeError, ValueError):
        start = end - FIRST_RUN
    if since is not None:     # a trial only: a wider window with --dry-run or --no-write
        start = since
    if start >= end:
        return True, 'no settled window yet'
    items = messages(start, end, cfg, home)
    if not items:
        if not (dry_run or no_write):
            state['last'] = end.isoformat(timespec='seconds')
            write_state(cfg, state)
        return True, 'no new user messages'
    chosen = _prompt_lines(items)
    left = len(items) - len(chosen)
    # Messages left over move `last` only to the last one in the prompt; they wait for tomorrow.
    new_last = _instant(chosen[-1][0]) if left else end
    covered = items[:len(chosen)]
    open_items = state.get('open', [])
    instruction = prompt([line for _, line in chosen], commits(start, cfg)[:MAX_COMMITS],
                         receipts(start, cfg), open_items, cfg)
    rest = f', {left} message(s) wait for the next run' if left else ''
    if dry_run:
        print(instruction)
        return True, f'dry run: {len(covered)} message(s){rest}, prompt {len(instruction)} characters'
    try:
        proposals = stage.ask_json(instruction, cfg, job='reactions')
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return False, f'model call failed: {exc}; the messages are scanned again next run'
    known = {item.get('id') for item in open_items} | set(state.get('closed', {}))
    new = verified(proposals, [(stamp, text) for stamp, _, text in covered], known)
    handled = [item for item in proposals if isinstance(item, dict) and item.get('handled') is True]
    summary = (f'{len(covered)} message(s){rest}; {len(proposals)} reaction(s), {len(handled)} handled, '
               f'{len(new)} new debt')
    if no_write:
        for item in handled:
            print(f'  handled [{item.get("kind")}] "{item.get("quote")}" <- {item.get("evidence")}')
        for item in new:
            print(f'- [{item["kind"]}] {item["id"]} {item["day"]} "{item["quote"]}" -> {item["where"]}: {item["lesson"]}')
        return True, summary + ' (not recorded)'
    state['open'] = open_items + new
    state['last'] = new_last.isoformat(timespec='seconds')
    write_state(cfg, state)
    return True, f"{summary}; open {len(state['open'])}"


def list_open(cfg: dict) -> int:
    try:
        open_items = read_state(cfg).get('open', [])
    except StateError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not open_items:
        print('no open reaction debt')
    for item in open_items:
        print(f"{item.get('id')}  {item.get('day')}  [{item.get('kind')}] \"{item.get('quote')}\"\n"
              f"          lesson: {item.get('lesson')}\n          suggested place: {item.get('where')}")
    return 0


def close(item_id: str, where: str, cfg: dict) -> int:
    try:
        state = read_state(cfg)
    except StateError as exc:
        print(exc, file=sys.stderr)
        return 1
    open_items = state.get('open', [])
    left = [item for item in open_items if item.get('id') != item_id]
    if len(left) == len(open_items):
        print(f'not in the open debt: {item_id}', file=sys.stderr)
        return 1
    state['open'] = left
    state.setdefault('closed', {})[item_id] = f'{dt.date.today().isoformat()} -> {where}'
    write_state(cfg, state)
    print(f'closed: {item_id} -> {where}; open {len(left)}')
    return 0


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    argv = sys.argv[1:] if argv is None else argv
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f'reaction audit failed: {exc}', file=sys.stderr)
        return 1
    if argv[:1] == ['list']:
        return list_open(cfg)
    if argv[:1] == ['close']:
        if len(argv) != 3 or not argv[2].strip():
            print('usage: reactions.py close <id> "<where it went>"', file=sys.stderr)
            return 2
        return close(argv[1], argv[2].strip(), cfg)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dry-run', action='store_true', help='print the prompt, no model call')
    parser.add_argument('--no-write', action='store_true', help='call the model, record nothing')
    parser.add_argument('--since', type=dt.date.fromisoformat,
                        help='trial start day (only with --dry-run or --no-write)')
    args = parser.parse_args(argv)
    if args.since and not (args.dry_run or args.no_write):
        parser.error('--since only with --dry-run or --no-write')
    since = dt.datetime.combine(args.since, dt.time()).astimezone() if args.since else None
    ok, message = run(cfg, dry_run=args.dry_run, no_write=args.no_write, since=since)
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
