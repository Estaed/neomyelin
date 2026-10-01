#!/usr/bin/env python3
"""NeoMyelin engine: the vault's receipts, tasks and daily view, kept as plain Markdown files.

    python brain.py receipt --file <json|-> [--harness claude|codex|agy|manual]
    python brain.py task-create --file <json|->
    python brain.py task-update --file <json|->
    python brain.py history <tasks/<id>.md or id>
    python brain.py sync
    python brain.py doctor

install.py puts this file at the vault root; it works on the folder it sits in. Each command
prints one JSON result on stdout and exits 0; a refused or failed command prints
{"error", "message"} on stderr and exits 1. Files carry JSON frontmatter between `---` lines,
then the body, UTF-8 with LF line endings.

- receipt: {"event_id", "summary", "refs", "session"?} -> receipts/<sha256(event_id)>.md with
  {kind, event_id, harness, refs, visibility, created_at, session?} and the summary as body.
  Every ref must be an existing file in the vault. Written once per event_id: the same receipt
  again writes nothing; another summary or other refs under that event_id are refused. Secrets
  in the summary (API keys, tokens, private keys, passwords) are masked as [REDACTED] and
  counted. A written receipt runs sync.
- task-create: {"source": "tasks/<id>.md", "text", "metadata"} with metadata from TASK_FIELDS;
  the file gets kind "task" and revision 1. task-update: {"id", "expected_revision", "changes"};
  an expected revision that is not the file's own is refused (RevisionConflict). Each create
  and update is logged in .brain/.state/engine/history/<id>.jsonl; history prints that log,
  plus the file as it is now when it changed after the last entry (a hand edit, or a task made
  before the log existed), so the last entry always carries the file's revision.
- sync: daily/<local day>.md, one generated view per day of the receipts: local time, the
  summary's first line, where it ran, the body and a link to the receipt. A daily note that is
  not a generated view is never overwritten.
- doctor: the engine's self-check (unreadable receipts and tasks, duplicate task ids, daily
  views out of date); .brain/scripts/doctor.py reports it.

The file formats follow Avenox V3's, which this engine replaces; the code is NeoMyelin's own.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import sys
import tempfile
import time

VAULT = Path(__file__).resolve().parent
HARNESSES = ('claude', 'codex', 'agy', 'manual')
STATUSES = ('inbox', 'active', 'waiting', 'blocked', 'done', 'cancelled')
TASK_FIELDS = ('id', 'title', 'status', 'owner', 'project', 'next_action', 'due_at', 'kind', 'revision')
CHANGEABLE = ('title', 'status', 'owner', 'project', 'next_action', 'due_at')
TEXT_FIELDS = ('title', 'owner', 'project', 'next_action', 'due_at')
TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}')
TASK_SOURCE = re.compile(r'tasks/[^/\\]+\.md')
SESSION = re.compile(r'[A-Za-z0-9_-]{1,64}')
FRONTMATTER = re.compile(r'---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)', re.S)
VIEW = {'generated': True, 'kind': 'receipt-index'}
LOCK_WAIT, LOCK_STALE = 15.0, 120.0  # seconds

REDACTED = '[REDACTED]'
_NAME = (r'(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|refresh[_ -]?token|client[_ -]?secret'
         r'|secret[_ -]?key|private[_ -]?key|password|passwd|pwd|secret|token)')
# (pattern, group to mask; 0 masks the whole match). Kept narrow: a receipt is prose, and a
# masked word that was no secret loses the record its meaning.
SECRETS = (
    (re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:.*?-----END [A-Z0-9 ]*PRIVATE KEY-----)?', re.S), 0),
    (re.compile(rf'(?i)(?<![A-Za-z0-9]){_NAME}["\']?\s*[:=]\s*["\']?(?!\[REDACTED\])([^\s"\'&,;]{{6,}})'), 1),
    (re.compile(r'(?<=://)[^/\s:@]+:(?!\[REDACTED\]@)([^/\s@]+)(?=@)'), 1),
    (re.compile(r'(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{16,})'), 1),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}'), 0),
    (re.compile(r'\bglpat-[A-Za-z0-9_-]{20,}'), 0),
    (re.compile(r'\bsk-[A-Za-z0-9_-]{20,}'), 0),
    (re.compile(r'\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}'), 0),
    (re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'), 0),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{35}'), 0),
    (re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}'), 0),
    (re.compile(r'\bnpm_[A-Za-z0-9]{36}\b'), 0),
    (re.compile(r'\bhf_[A-Za-z0-9]{30,}\b'), 0),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'), 0),
)


class Refused(ValueError):
    """The input or the vault does not allow the command; nothing was written."""


class RevisionConflict(Refused):
    """task-update named a revision that is not the task file's own."""


class ReceiptConflict(Refused):
    """The event_id already has a receipt with another summary or other refs."""


# ---------------------------------------------------------------------------------------------
# Files

def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def split(text: str) -> tuple[dict, str]:
    """(metadata, body) of a file with JSON frontmatter; ValueError when it has none."""
    match = FRONTMATTER.match(text)
    if not match:
        raise ValueError('no JSON frontmatter')
    metadata = json.loads(match.group(1))
    if not isinstance(metadata, dict):
        raise ValueError('the frontmatter is not a JSON object')
    return metadata, text[match.end():]


def read(path: Path) -> tuple[dict, str]:
    return split(path.read_bytes().decode('utf-8-sig'))


def render(metadata: dict, body: str) -> str:
    return '---\n' + json.dumps(metadata, ensure_ascii=False, indent=2) + '\n---\n' + body


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(vault: Path):
    """One writing command at a time per vault; a lock left by a killed command goes stale."""
    path = vault / '.brain' / '.state' / 'engine.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise Refused(f'another brain.py command holds {path}; try again, or delete it if '
                              'no command is running') from None
            time.sleep(0.05)
    try:
        os.write(descriptor, f'{os.getpid()}\n'.encode('ascii'))
        os.close(descriptor)
        yield
    finally:
        path.unlink(missing_ok=True)


def vault_file(vault: Path, ref: object) -> str:
    """A ref as a vault-relative POSIX path; Refused unless it names an existing file in the vault."""
    if not isinstance(ref, str) or not ref.strip():
        raise Refused('every ref must be a non-empty vault-relative path')
    relative = PurePath(ref.strip().replace('\\', '/'))
    if relative.anchor or '..' in relative.parts:
        raise Refused(f'ref {ref!r} must be relative to the vault, without ..')
    target = vault / relative
    try:
        inside = target.resolve().is_relative_to(vault.resolve())
    except OSError:
        inside = False
    if not inside or not target.is_file():
        raise Refused(f'ref {ref!r} is not a file in the vault (refs must name existing files; '
                      'else use AGENTS.md)')
    return relative.as_posix()


def redact(text: str) -> tuple[str, int]:
    count = 0

    def mask(match: re.Match, group: int) -> str:
        nonlocal count
        count += 1
        if not group:
            return REDACTED
        whole, start = match.group(0), match.start()
        return whole[:match.start(group) - start] + REDACTED + whole[match.end(group) - start:]

    for pattern, group in SECRETS:
        text = pattern.sub(lambda match, group=group: mask(match, group), text)
    return text, count


def load_input(name: str) -> object:
    try:
        raw = sys.stdin.buffer.read() if name == '-' else Path(name).read_bytes()
        return json.loads(raw.decode('utf-8-sig'))
    except OSError as exc:
        raise Refused(f'cannot read {name}: {exc}') from None
    except (UnicodeError, ValueError) as exc:
        raise Refused(f'{"stdin" if name == "-" else name} is not valid JSON: {exc}') from None


# ---------------------------------------------------------------------------------------------
# Receipts and the daily view

def receipt(vault: Path, payload: object, harness: str = 'manual') -> dict:
    if not isinstance(payload, dict):
        raise Refused('the receipt JSON must be an object')
    event_id, summary, refs, session = (payload.get(key) for key in ('event_id', 'summary', 'refs', 'session'))
    if not isinstance(event_id, str) or not event_id.strip():
        raise Refused('"event_id" must be a non-empty string')
    if not isinstance(summary, str) or not summary.strip():
        raise Refused('"summary" must be a non-empty string')
    if not isinstance(refs, list) or not refs:
        raise Refused('"refs" must be a non-empty list of existing vault files (else AGENTS.md)')
    refs = [vault_file(vault, ref) for ref in refs]
    if session is not None and (not isinstance(session, str) or not SESSION.fullmatch(session)):
        raise Refused('"session" must be 1-64 letters, digits, "_" or "-" (the [Memory: Session] value)')
    if harness not in HARNESSES:
        raise Refused(f'--harness must be one of {", ".join(HARNESSES)}')
    summary, count = redact(summary)
    source = f'receipts/{hashlib.sha256(event_id.encode("utf-8")).hexdigest()}.md'
    path = vault / source
    daily: list[str] = []
    result = {}
    with locked(vault):
        if path.exists():
            try:
                metadata, body = read(path)
            except (OSError, UnicodeError, ValueError) as exc:
                raise ReceiptConflict(f'{source} exists and is not a readable receipt ({exc})') from None
            if (body[:-1] if body.endswith('\n') else body) != summary or metadata.get('refs') != refs:
                raise ReceiptConflict(f'event_id {event_id!r} already has a receipt ({source}) with another '
                                      'summary or other refs; give this one a new event_id')
            written = False
        else:
            metadata = {'kind': 'receipt', 'event_id': event_id, 'harness': harness, 'refs': refs,
                        'visibility': 'internal', 'created_at': now_utc()}
            if session is not None:
                metadata['session'] = session
            write_atomic(path, render(metadata, summary + '\n'))
            written = True
            try:
                daily = write_views(vault)['written']
            except OSError as exc:  # the receipt stands; the view catches up at the next sync
                result['daily_error'] = f'{type(exc).__name__}: {exc}; run brain.py sync'
    return {'status': 'succeeded', 'id': event_id, 'event_id': event_id, 'source': source,
            'written': written, 'daily': daily, 'redacted': count, 'secrets_redacted': count, **result}


def _instant(value: object) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)  # a naive stamp is UTC


def origin(harness: object) -> str:
    return 'Manual' if harness in (None, '', 'manual') else 'Local PC'


def _entry(local: dt.datetime, source: str, summary: str, harness: object) -> str:
    first, _, rest = summary.partition('\n')
    title, body = (first.rstrip(), rest) if first.startswith('[') else ('', summary)
    head = f'### {local:%H:%M}' + (f' — {title}' if title else '') + f' · {origin(harness)}'
    return '\n\n'.join(part for part in (head, body.strip(), f'[[{source}|receipt]]') if part) + '\n'


def plan_views(vault: Path) -> tuple[dict[str, str], list[dict]]:
    """({daily/<day>.md: text}, skipped receipts): the daily view every receipt asks for."""
    days: dict[str, list[tuple]] = {}
    skipped = []
    for path in sorted((vault / 'receipts').glob('*.md')):
        source = f'receipts/{path.name}'
        try:
            metadata, body = read(path)
            if metadata.get('kind') != 'receipt':
                raise ValueError('kind is not "receipt"')
            instant = _instant(metadata['created_at'])
            local = instant.astimezone()
        except (OSError, UnicodeError, ValueError, KeyError, OverflowError) as exc:
            skipped.append({'source': source, 'reason': f'not in the daily view: {exc}'})
            continue
        summary = body[:-1] if body.endswith('\n') else body
        days.setdefault(local.date().isoformat(), []).append(
            (instant, source, local, summary, metadata.get('harness')))
    views = {}
    for day, items in sorted(days.items()):
        entries = [_entry(local, source, summary, harness)
                   for _, source, local, summary, harness in sorted(items, key=lambda item: item[:2])]
        views[f'daily/{day}.md'] = render(VIEW, f'# Daily: {day}\n\n' + '\n'.join(entries))
    return views, skipped


def _generated(data: bytes) -> bool:
    try:
        return split(data.decode('utf-8-sig'))[0].get('generated') is True
    except (UnicodeError, ValueError):
        return False


def stale_views(vault: Path) -> tuple[dict[str, str], list[dict]]:
    """The views sync would write, and what it skips (unreadable receipts, notes in a view's place)."""
    views, skipped = plan_views(vault)
    stale = {}
    for relative, text in views.items():
        try:
            current = (vault / relative).read_bytes()
        except FileNotFoundError:
            current = None
        if current == text.encode('utf-8'):
            continue
        if current is not None and not _generated(current):
            skipped.append({'source': relative, 'reason': 'a note that is not a generated view; left as it is'})
            continue
        stale[relative] = text
    return stale, skipped


def write_views(vault: Path) -> dict:
    stale, skipped = stale_views(vault)
    for relative, text in stale.items():
        write_atomic(vault / relative, text)
    return {'status': 'succeeded', 'written': sorted(stale), 'skipped': skipped}


def sync(vault: Path) -> dict:
    with locked(vault):
        return write_views(vault)


# ---------------------------------------------------------------------------------------------
# Tasks

def _check_fields(fields: dict) -> None:
    for key in TEXT_FIELDS:
        if key in fields and not isinstance(fields[key], str):
            raise Refused(f'"{key}" must be a string')
    if 'owner' in fields and not fields['owner'].strip():
        raise Refused('"owner" must be a non-empty string')
    if 'status' in fields and fields['status'] not in STATUSES:
        raise Refused(f'"status" must be one of {", ".join(STATUSES)}')
    if 'due_at' in fields:
        try:
            dt.date.fromisoformat(fields['due_at'][:10])
        except ValueError:
            raise Refused('"due_at" must start with a date, YYYY-MM-DD') from None


def _redact_task(fields: dict) -> int:
    count = 0
    for key in ('title', 'next_action'):
        if isinstance(fields.get(key), str):
            fields[key], found = redact(fields[key])
            count += found
    return count


def _tasks(vault: Path):
    """(path, metadata, body) of every readable task file."""
    for path in sorted((vault / 'tasks').glob('*.md')):
        try:
            metadata, body = read(path)
        except (OSError, UnicodeError, ValueError):
            continue
        yield path, metadata, body


def _find_task(vault: Path, key: str) -> tuple[Path, dict, str] | None:
    """The task file named by a `tasks/<file>.md` source or by its id."""
    key = key.strip().replace('\\', '/')
    if key.endswith('.md'):
        if not TASK_SOURCE.fullmatch(key):
            raise Refused(f'{key!r} is not a task source (tasks/<id>.md)')
        path = vault / key
        if not path.is_file():
            return None
        try:
            metadata, body = read(path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise Refused(f'{key} is not a readable task ({exc})') from None
        return path, metadata, body
    if not TASK_ID.fullmatch(key):
        raise Refused(f'{key!r} is neither a task id nor a task source (tasks/<id>.md)')
    direct = vault / 'tasks' / f'{key}.md'  # the usual place first; any file may hold the id
    candidates = sorted(_tasks(vault), key=lambda item: item[0] != direct)
    return next((item for item in candidates if item[1].get('id') == key), None)


def _history_file(vault: Path, task_id: str) -> Path:
    return vault / '.brain' / '.state' / 'engine' / 'history' / f'{task_id}.jsonl'


def _log(vault: Path, event: str, source: str, metadata: dict) -> None:
    path = _history_file(vault, metadata['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {'event_type': event, 'record_id': metadata['id'], 'revision': metadata['revision'],
            'source': source, 'at': now_utc(), 'record': metadata}
    with path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + '\n')


def task_create(vault: Path, payload: object) -> dict:
    if not isinstance(payload, dict):
        raise Refused('the task JSON must be an object: {"source", "text", "metadata"}')
    source, text, metadata = (payload.get(key) for key in ('source', 'text', 'metadata'))
    if not isinstance(source, str) or not TASK_SOURCE.fullmatch(source.replace('\\', '/')):
        raise Refused('"source" must be tasks/<id>.md')
    source = source.replace('\\', '/')
    if not isinstance(text, str) or not text.strip():
        raise Refused('"text" must be a non-empty string (the task body)')
    if text.lstrip().startswith('---'):
        raise Refused('"text" is the body only; put frontmatter fields in "metadata"')
    if not isinstance(metadata, dict):
        raise Refused('"metadata" must be an object')
    unknown = sorted(set(metadata) - set(TASK_FIELDS))
    if unknown:
        raise Refused(f'unsupported task metadata {", ".join(unknown)}; allowed: {", ".join(TASK_FIELDS)}')
    if not isinstance(metadata.get('id'), str) or not TASK_ID.fullmatch(metadata['id']):
        raise Refused('"id" must be letters, digits, "_" or "-" (up to 100), starting with a letter or digit')
    if 'owner' not in metadata or 'status' not in metadata:
        raise Refused('"owner" and "status" are required')
    if metadata.get('revision', 1) != 1 or type(metadata.get('revision', 1)) is not int:
        raise Refused('a new task has revision 1')
    if metadata.get('kind', 'task') != 'task':
        raise Refused('"kind" must be "task"')
    _check_fields(metadata)
    metadata = dict(metadata)
    metadata.update(kind='task', revision=1)
    text, count = redact(text)
    count += _redact_task(metadata)
    path = vault / source
    with locked(vault):
        if path.exists() or any(found.get('id') == metadata['id'] for _, found, _ in _tasks(vault)):
            raise Refused(f'task {metadata["id"]} or {source} exists; use task-update')
        write_atomic(path, render(metadata, text + '\n'))
        _log(vault, 'create', source, metadata)
    return {'status': 'succeeded', 'id': metadata['id'], 'source': source, 'revision': 1,
            'redacted': count, 'secrets_redacted': count}


def task_update(vault: Path, payload: object) -> dict:
    if not isinstance(payload, dict):
        raise Refused('the update JSON must be an object: {"id", "expected_revision", "changes"}')
    task_id, expected, changes = (payload.get(key) for key in ('id', 'expected_revision', 'changes'))
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise Refused('"id" must be a task id')
    if type(expected) is not int:
        raise Refused('"expected_revision" must be an integer (the revision you read)')
    if not isinstance(changes, dict) or not changes:
        raise Refused('"changes" must be a non-empty object')
    unknown = sorted(set(changes) - set(CHANGEABLE))
    if unknown:
        raise Refused(f'cannot change {", ".join(unknown)}; changeable: {", ".join(CHANGEABLE)}')
    _check_fields(changes)
    changes = dict(changes)
    count = _redact_task(changes)
    with locked(vault):
        found = _find_task(vault, task_id)
        if found is None:
            raise Refused(f'no task with id {task_id} in tasks/')
        path, metadata, body = found
        source = path.relative_to(vault).as_posix()
        current = metadata.get('revision')
        if type(current) is not int:
            raise Refused(f'{source} has no integer "revision"; fix its frontmatter first')
        if expected != current:
            raise RevisionConflict(f'stale revision: you sent {expected}, {source} is at revision {current}; '
                                   f'read it again (brain.py history {source}) and retry')
        metadata.update(changes)
        metadata['revision'] = current + 1
        write_atomic(path, render(metadata, body))
        _log(vault, 'update', source, metadata)
    return {'status': 'succeeded', 'id': task_id, 'source': source, 'revision': current + 1,
            'changed': sorted(changes), 'redacted': count, 'secrets_redacted': count}


def history(vault: Path, key: str) -> list[dict]:
    found = _find_task(vault, key)
    if found is not None:
        path, metadata, _ = found
        task_id = metadata.get('id') if isinstance(metadata.get('id'), str) else path.stem
    else:
        task_id = PurePath(key.strip().replace('\\', '/')).stem
    log = _history_file(vault, task_id) if TASK_ID.fullmatch(task_id) else None
    events = []
    try:
        lines = log.read_text(encoding='utf-8').splitlines() if log else []
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    if found is None and not events:
        raise Refused(f'no task {key} in tasks/ and no history for it')
    if found is not None and (not events or events[-1].get('record') != metadata):
        stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()
        events.append({'event_type': 'file', 'record_id': task_id, 'revision': metadata.get('revision'),
                       'source': path.relative_to(vault).as_posix(), 'at': stamp, 'record': metadata})
    return [{'sequence': number, **event} for number, event in enumerate(events, 1)]


# ---------------------------------------------------------------------------------------------
# Self-check

def doctor(vault: Path) -> dict:
    problems = []
    receipts = 0
    for path in sorted((vault / 'receipts').glob('*.md')):
        try:
            metadata, _ = read(path)
            if metadata.get('kind') != 'receipt' or not isinstance(metadata.get('event_id'), str):
                raise ValueError('no kind "receipt" or no event_id')
            _instant(metadata['created_at'])
            receipts += 1
        except (OSError, UnicodeError, ValueError, KeyError, OverflowError) as exc:
            problems.append(f'receipts/{path.name}: unreadable ({exc})')
    tasks, seen = 0, {}
    for path in sorted((vault / 'tasks').glob('*.md')):
        source = f'tasks/{path.name}'
        try:
            metadata, _ = read(path)
        except (OSError, UnicodeError, ValueError) as exc:
            problems.append(f'{source}: unreadable ({exc})')
            continue
        task_id, revision = metadata.get('id'), metadata.get('revision')
        if not isinstance(task_id, str) or type(revision) is not int or revision < 1:
            problems.append(f'{source}: needs a string "id" and an integer "revision"')
            continue
        tasks += 1
        if task_id in seen:
            problems.append(f'{source}: id {task_id} is also used by {seen[task_id]}')
        seen.setdefault(task_id, source)
    stale, skipped = stale_views(vault)
    if stale:
        problems.append(f'daily view out of date for {len(stale)} day(s); run brain.py sync')
    problems += [f'{item["source"]}: {item["reason"]}' for item in skipped
                 if not item['source'].startswith('receipts/')]  # unreadable receipts are listed above
    lock = vault / '.brain' / '.state' / 'engine.lock'
    if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE:
        problems.append(f'{lock.relative_to(vault).as_posix()} is stale; delete it if no brain.py command runs')
    return {'status': 'warning' if problems else 'ok', 'receipts': receipts, 'tasks': tasks,
            'problems': problems[:20], 'problem_count': len(problems)}


# ---------------------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='brain.py', description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest='command', required=True)
    write = commands.add_parser('receipt', help='Write an idempotent receipt and update the daily view')
    write.add_argument('--file', default='-', help='JSON {event_id, summary, refs, session}, or - for stdin')
    write.add_argument('--harness', choices=HARNESSES, default='manual')
    create = commands.add_parser('task-create', help='Create a task at revision 1')
    create.add_argument('--file', default='-', help='JSON {source, text, metadata}, or - for stdin')
    update = commands.add_parser('task-update', help="Change a task's metadata at its current revision")
    update.add_argument('--file', default='-', help='JSON {id, expected_revision, changes}, or - for stdin')
    log = commands.add_parser('history', help="A task's revisions, oldest first")
    log.add_argument('task', help='tasks/<id>.md or the task id')
    commands.add_parser('sync', help='Rebuild the daily view from the receipts')
    commands.add_parser('doctor', help="The engine's self-check")
    return root


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    args = parser().parse_args(argv)
    try:
        if not (VAULT / '.brain').is_dir():
            raise Refused(f'{VAULT} has no .brain folder; brain.py works from the vault root install.py set up')
        if args.command == 'receipt':
            result = receipt(VAULT, load_input(args.file), args.harness)
        elif args.command == 'task-create':
            result = task_create(VAULT, load_input(args.file))
        elif args.command == 'task-update':
            result = task_update(VAULT, load_input(args.file))
        elif args.command == 'history':
            result = history(VAULT, args.task)
        elif args.command == 'sync':
            result = sync(VAULT)
        else:
            result = doctor(VAULT)
    except (OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
