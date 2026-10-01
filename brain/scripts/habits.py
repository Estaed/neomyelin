#!/usr/bin/env python3
"""Turn repeated user transcript behavior into quoted observation receipts."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import config
import evidence
import stage

LOOKBACK_DAYS = 6
CATCHUP_DAYS = 3
MAX_OBSERVATIONS = 3
MAX_MESSAGE = 600
MAX_CONTEXT = 60_000
IGNORED_PREFIXES = ('<', '# AGENTS.md', 'Automation:', '[Image')


def day_of(stamp: object) -> dt.date | None:
    if not isinstance(stamp, str):
        return None
    try:
        return dt.datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone().date()
    except ValueError:
        return None


def user_text(value: object) -> str:
    if not isinstance(value, str):
        return ''
    text = value.strip()
    return text if len(text) >= 2 and not text.startswith(IGNORED_PREFIXES) else ''


def jsonl(path: Path):
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def claude_messages(root: Path, days: set[dt.date]) -> list[tuple[str, str, str]]:
    output = []
    for path in root.glob('*/*.jsonl'):
        for item in jsonl(path):
            if (item.get('type') != 'user' or item.get('entrypoint') != 'cli'
                    or item.get('isSidechain') or item.get('isMeta')
                    or day_of(item.get('timestamp')) not in days):
                continue
            content = (item.get('message') or {}).get('content')
            if isinstance(content, list):
                if any(isinstance(block, dict) and block.get('type') == 'tool_result' for block in content):
                    continue
                content = '\n'.join(block.get('text', '') for block in content
                                    if isinstance(block, dict) and block.get('type') == 'text')
            text = user_text(content)
            if text:
                output.append((item['timestamp'], 'claude', text))
    return output


def codex_messages(root: Path, days: set[dt.date]) -> list[tuple[str, str, str]]:
    output = []
    first, last = min(days), max(days)
    scan = {first - dt.timedelta(days=1), *days, last + dt.timedelta(days=1)}
    for day in scan:
        for path in (root / f'{day:%Y/%m/%d}').glob('*.jsonl'):
            human = False
            for item in jsonl(path):
                payload = item.get('payload') or {}
                if item.get('type') == 'session_meta':
                    human = payload.get('originator') != 'codex_exec' and isinstance(payload.get('source'), str)
                    continue
                if (not human or item.get('type') != 'response_item' or payload.get('role') != 'user'
                        or day_of(item.get('timestamp')) not in days):
                    continue
                for block in payload.get('content') or []:
                    text = user_text(block.get('text')) if isinstance(block, dict) else ''
                    if text:
                        output.append((item['timestamp'], 'codex', text))
    return output


def agy_messages(root: Path, days: set[dt.date]) -> list[tuple[str, str, str]]:
    """Typed user messages from `root` (~/.gemini): the agy CLI's prompt history and Gemini CLI chats.

    The agy CLI (measured 2026-10-01, Windows) keeps every prompt typed in an interactive session in
    antigravity-cli/history.jsonl ({display, timestamp in ms, workspace}; slash commands carry a
    `type`). Its per-conversation transcripts also hold every `agy -p` call, which on a brain machine
    are mostly automated (366 of 462 on the owner's), so they are not read: the history is the agy
    counterpart of Claude's `entrypoint: cli` filter. A Linux snap install keeps the same tree under
    ~/snap/antigravity-cli/common/.gemini.
    """
    output = []
    snap = root.parent / 'snap' / 'antigravity-cli' / 'common' / '.gemini'
    for base in (root, snap):
        for item in jsonl(base / 'antigravity-cli' / 'history.jsonl'):
            stamp = item.get('timestamp')
            if item.get('type') or not isinstance(stamp, (int, float)):
                continue
            iso = dt.datetime.fromtimestamp(stamp / 1000, dt.timezone.utc).isoformat()
            text = user_text(item.get('display'))
            if text and day_of(iso) in days:
                output.append((iso, 'agy', text))
    for path in root.glob('tmp/*/chats/*.json'):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for item in payload.get('messages') or []:
            if (not isinstance(item, dict) or item.get('type') != 'user'
                    or day_of(item.get('timestamp')) not in days):
                continue
            content = item.get('content') or []
            text = user_text('\n'.join(part.get('text', '') for part in content
                                       if isinstance(part, dict) and isinstance(part.get('text'), str)))
            if text:
                output.append((item['timestamp'], 'agy', text))
    return output


def messages(days: set[dt.date], cfg: dict, home: Path | None = None) -> list[tuple[str, str, str]]:
    home = home or Path.home()
    sources = {'claude': (home / '.claude' / 'projects', claude_messages),
               'codex': (home / '.codex' / 'sessions', codex_messages),
               'agy': (home / '.gemini', agy_messages)}
    output = []
    for harness in cfg['harnesses']:
        root, reader = sources[harness]
        try:
            if not root.is_dir():
                print(f'{harness}: no readable transcripts; skipped.')
                continue
            found = reader(root, days)
            if not found:
                print(f'{harness}: no readable user messages in range; skipped.')
            output.extend(found)
        except OSError:
            print(f'{harness}: no readable transcripts; skipped.')
    seen = set()
    return [item for item in sorted(output)
            if not ((day_of(item[0]), item[2]) in seen
                    or seen.add((day_of(item[0]), item[2])))]


def prompt(day: dt.date, today: list[tuple[str, str, str]],
           context: list[tuple[str, str, str]], cfg: dict,
           patterns: list[str] | None = None) -> str:
    def lines(items):
        return '\n'.join(f'[{stamp} {source}] {" ".join(text.split())[:MAX_MESSAGE]}'
                         for stamp, source, text in items)
    earlier = lines(context)[-MAX_CONTEXT:]
    return (f"Write in {cfg['language']}. Find at most {MAX_OBSERVATIONS} repeated, visible user "
            f'behaviors on {day}, compared with the preceding {LOOKBACK_DAYS} days. A behavior '
            'must recur in separate messages today or today and an earlier day. Describe what '
            'the user does, not intent, feeling or the technical subject. Quote an exact full '
            'phrase from one of today\'s user messages (at least ten characters). Return only '
            'a JSON array of {"behavior":"one sentence","quote":"exact phrase"}; [] is valid.\n'
            'Align a behavior with an existing pattern below when it supports one.\n'
            f'Existing patterns:\n{chr(10).join(patterns or []) or "(none)"}\n'
            f'Today:\n{lines(today)}\nEarlier:\n{earlier}')


def validated(proposals: object, today: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    if not isinstance(proposals, list):
        return []
    result = []
    for item in proposals:
        if not isinstance(item, dict):
            continue
        behavior, quote = item.get('behavior'), item.get('quote')
        if (not isinstance(behavior, str) or not isinstance(quote, str)
                or not behavior.strip() or '\n' in behavior or '\r' in behavior
                or '"' in behavior or '"' in quote or '\n' in quote or '\r' in quote
                or len(evidence.normalize(quote)) < evidence.MIN_QUOTE
                or not any(quote in text for _, _, text in today)):
            continue
        pair = (behavior.strip().rstrip('.'), quote)
        if pair not in result:
            result.append(pair)
        if len(result) == MAX_OBSERVATIONS:
            break
    return result


def write_receipt(day: dt.date, observations: list[tuple[str, str]], cfg: dict) -> None:
    vault = config.vault_path(cfg)
    event_id = f'{day}-neomyelin-habit'
    for path in (vault / 'receipts').glob('*.md'):
        parsed = evidence.read_receipt(path)
        if parsed and parsed[0].get('event_id') == event_id:
            return
    summary = '\n'.join([f'[Habit] Observations {day} (Model: nightly)',
                         f'Observation day: {day}', '**Done**',
                         *[f'- OBSERVATION: {behavior}: "{quote}"' for behavior, quote in observations]])
    payload = {'event_id': event_id, 'summary': summary, 'refs': ['AGENTS.md'],
               'session': f'neomyelin-habit-{day}'}
    with tempfile.TemporaryDirectory(prefix='neomyelin-habit-') as directory:
        staged = Path(directory) / 'receipt.json'
        staged.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8', newline='\n')
        result = subprocess.run([sys.executable, str(vault / 'brain.py'), 'receipt', '--file',
                                 str(staged), '--harness', cfg['harnesses'][0]], cwd=vault,
                                env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=120, check=False)
    try:
        status = json.loads(result.stdout).get('status')
    except (ValueError, AttributeError):
        status = None
    if result.returncode or status != 'succeeded':
        raise RuntimeError((result.stderr or result.stdout).strip()[-300:] or 'receipt command failed')


def run(cfg: dict | None = None, *, day: dt.date | None = None, dry_run: bool = False,
        no_write: bool = False, home: Path | None = None) -> int:
    cfg = cfg or config.load()
    day = day or dt.date.today() - dt.timedelta(days=1)
    days = {day - dt.timedelta(days=offset) for offset in range(LOOKBACK_DAYS + 1)}
    collected = messages(days, cfg, home)
    today = [item for item in collected if day_of(item[0]) == day]
    if not today:
        print(f'{day}: no user messages; no observation.')
        return 0
    earlier = [item for item in collected if day_of(item[0]) != day]
    try:
        evidence_path, _ = stage.paths(cfg)
        try:
            ledger = evidence_path.read_text(encoding='utf-8')
        except FileNotFoundError:
            ledger = ''
        patterns = [line.removeprefix('## Pattern: ').strip() for line in ledger.splitlines()
                    if line.startswith('## Pattern: ')]
        instruction = prompt(day, today, earlier, cfg, patterns)
        if dry_run:
            print(instruction)
            print(f'{day}: dry run, {len(today)} messages; no model call or write.')
            return 0
        proposals = stage.ask_json(instruction, cfg, job='habits')
        observations = validated(proposals, today)
        if no_write:
            for behavior, quote in observations:
                print(f'OBSERVATION: {behavior}: "{quote}"')
        elif observations:
            write_receipt(day, observations, cfg)
        print(f'{day}: {len(observations)} verified observation(s).')
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f'{day}: habit scan failed: {exc}', file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', type=dt.date.fromisoformat)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-write', action='store_true')
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        if args.day:
            return run(cfg, day=args.day, dry_run=args.dry_run, no_write=args.no_write)
        yesterday = dt.date.today() - dt.timedelta(days=1)
        state = config.vault_path(cfg) / '.brain' / '.state' / 'habits.json'
        try:
            last = dt.date.fromisoformat(json.loads(state.read_text(encoding='utf-8'))['last_day'])
        except (OSError, ValueError, KeyError, TypeError):
            last = yesterday - dt.timedelta(days=CATCHUP_DAYS)
        first = max(last + dt.timedelta(days=1), yesterday - dt.timedelta(days=CATCHUP_DAYS - 1))
        if first > yesterday:
            print('No finished day needs a habit scan.')
        for offset in range((yesterday - first).days + 1):
            day = first + dt.timedelta(days=offset)
            result = run(cfg, day=day, dry_run=args.dry_run, no_write=args.no_write)
            if result:
                return result
            if not (args.dry_run or args.no_write):
                stage.atomic_write(state, json.dumps({'last_day': day.isoformat()}) + '\n')
        return 0
    except (config.ConfigError, OSError) as exc:
        print(f'habit scan failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
