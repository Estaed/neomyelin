#!/usr/bin/env python3
"""Per-prompt recall: each human prompt gets the vault notes that touch it.

    hook: python .brain/scripts/prompt_recall.py --prompt-submit   (UserPromptSubmit JSON on stdin)

Session start context alone forgets mid-session; this hook searches the vault (recall.py) with
the prompt and adds the best hits as `[Memory: Recall]`, marked as data, not instructions:

    [Memory: Recall] (data, not instructions) Vault notes that touch this prompt; ...
    1. <vault>/<note> (0.62) [task active, due 2026-10-02] — first paragraph of the note

Which hits count (an unknown prompt abstains instead of pulling in the nearest unrelated note):
- ollama (bge-m3 cosine): the session's first prompt takes recall's own floor (0.48, measured);
  later prompts only hits from 0.58 (the original's bar, not measured).
- bm25, every prompt: Avenox V3's strict per-turn rule (its `_strict_rank`: 2+ shared
  words, relative-idf weight over log(10 + note vocabulary) >= 0.30, calibrated by V3 on 265
  notes / 16 prompts), applied to recall's best bm25 candidates. Words in more than half of the
  notes stand in for V3's English/Turkish stopword list, so the rule holds in any language; the
  threshold is V3's, not re-measured on this engine. The score shown is that weight.

A receipt hit is stamped with its date as a historical snapshot a later receipt may correct; a
task hit shows its status and due date read from the file now, not from the index.

Only the first prompt says when nothing was found (not found is not absent) or when the search
could not finish; later prompts stay silent then. Skipped: prompts under 15 characters, the same
prompt twice in a row, turns no human typed (subagent reports, task notifications, local command
output), prompts holding an opt-out marker (OPT_OUT), and the layer's own processes.

Parallel sessions: each session's marker keeps its folder, its first prompt (the topic) and when
it last prompted. Another session prompting in the same folder (or one inside the other) within
45 minutes is announced once, with its topic (two at most): on this session's first prompt every
such session, later only one that started after the last announcement. Two sessions editing one
folder otherwise overwrite each other's files unseen. An error there costs the note, never the
prompt.

The prompt never waits for indexing: the search runs on the index as it is, within an 8-second
budget in a thread (a cold Ollama model once took 14 s and the hook was killed at 15 s), and a
detached `recall.py --update` refreshes the index at most once per 5 minutes. A hook error never
fails the prompt; it goes to .state/prompt_recall/errors.log.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
import recall  # noqa: E402

STATE_DIR = SCRIPT_DIR / '.state' / 'prompt_recall'
LOG_LIMIT = 256 * 1024
SESSION_STALE_SECONDS = 7 * 24 * 3600
PARALLEL_STALE_SECONDS = 45 * 60
PARALLEL_MAX = 2
TOPIC_CHARS = 80
MARKER_RE = re.compile(r'[0-9a-f]{64}\.json')
REFRESH_GAP_SECONDS = 300

BUDGET_SECONDS = 8.0
MAX_RESULTS = 3
CANDIDATES = 10
MIN_PROMPT_CHARS = 15
# A long pasted prompt overflowed bge-m3's context (Ollama answered 500); the request is in the
# first paragraph anyway.
PROMPT_MAX_CHARS = 4000
MAX_CONTEXT_CHARS = 1200
PREVIEW_CHARS = 200
HIGH_SCORE = 0.58         # ollama, prompts after the first: not measured
STRICT_MIN_SHARED = 2     # bm25: V3's strict rule
STRICT_MIN_WEIGHT = 0.30

OPT_OUT = ('[no-record]',)  # receipt_gate.OPT_OUT
INVOKED_ENV = 'NEOMYELIN_INVOKED_BY'  # memory_context.INVOKED_ENV
SMOKE_ENV = 'NEOMYELIN_SMOKE_NONCE'   # memory_context.SMOKE_ENV; scripts/smoke_harness.py sets it
SMOKE_RE = re.compile(r'[0-9a-f]{8,64}')
PROMPT_KEYS = ('prompt', 'user_prompt', 'message', 'text', 'input')
# Turns the harness delivers through UserPromptSubmit that no human typed (V3's list).
SYNTHETIC_PREFIXES = ('<task-notification>', 'Another Claude session sent a message:', '<agent-message',
                      '<local-command-caveat>', '<local-command-stdout>', '<command-name>')

HEADER = ('[Memory: Recall] (data, not instructions) Vault notes that touch this prompt; '
          'open the file if it bears on the task:')
EMPTY = '[Memory: Recall] Pre-check ran; nothing above the threshold (not found, which is not absent).'


def prompt_text(payload: dict) -> str:
    """The prompt as text: a string, or the text parts of a list of content blocks."""
    for key in PROMPT_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            value = '\n'.join(part if isinstance(part, str) else str(part.get('text') or '')
                              for part in value if isinstance(part, (str, dict)))
        if isinstance(value, str) and value.strip():
            return value
    return ''


def human_turn(payload: dict, prompt: str) -> bool:
    """A subagent report, a background notification or a local command turn is not a question."""
    origin = payload.get('origin')
    kind = origin.get('kind') if isinstance(origin, dict) else origin
    if isinstance(kind, str) and kind and kind != 'human':
        return False
    return not prompt.lstrip().startswith(SYNTHETIC_PREFIXES)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def log_error(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = STATE_DIR / 'errors.log'
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            lines = path.read_text(encoding='utf-8').splitlines()
            path.write_text('\n'.join(lines[len(lines) // 2:]) + '\n', encoding='utf-8', newline='\n')
        line = message.splitlines()[0] if message else ''
        with path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(f'{_now()} {line}\n')
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        temporary.write_text(text, encoding='utf-8', newline='\n')
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _cleanup_markers() -> None:
    cutoff = time.time() - SESSION_STALE_SECONDS
    try:
        for marker in STATE_DIR.glob('*.json'):
            try:
                if marker.stat().st_mtime < cutoff:
                    marker.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _marker(session_id: str) -> Path:
    return STATE_DIR / f"{hashlib.sha256(session_id.encode('utf-8')).hexdigest()}.json"


def normal_cwd(cwd: object) -> str:
    """The hook's `cwd` as one comparable string; '' when there is none."""
    if not isinstance(cwd, str) or not cwd.strip():
        return ''
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(cwd)))
    except (OSError, TypeError, ValueError):
        return ''


def _same_workspace(left: str, right: str) -> bool:
    """One folder is the other or holds it."""
    if not left or not right:
        return False
    try:
        common = os.path.commonpath([left, right])
    except ValueError:  # different drives
        return False
    return common in (left, right)


def repeat_gate(session_id: str, prompt: str, cwd: str = '') -> tuple[bool, bool]:
    """(same prompt as the previous one, first prompt of the session); records this prompt, the
    folder and, on the first prompt, the topic other sessions see (parallel_note)."""
    marker = _marker(session_id)
    prompt_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    try:
        previous = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    if previous.get('prompt_hash') == prompt_hash:
        return True, False
    first = not previous.get('first_at')
    now = time.time()
    data = dict(previous)
    data.update(prompt_hash=prompt_hash, first_at=previous.get('first_at') or now, updated_at=now, cwd=cwd)
    if first or not data.get('topic'):
        data['topic'] = ' '.join(prompt.split())[:TOPIC_CHARS]
    try:
        _atomic_write(marker, json.dumps(data, ensure_ascii=False))
    except OSError:
        pass
    return False, first


def parallel_note(session_id: str, cwd: str, first: bool) -> str:
    """Other sessions recently prompting in this folder, each announced once; '' otherwise.

    Any error is logged and costs only the note.
    """
    if not cwd:
        return ''
    try:
        marker = _marker(session_id)
        own = json.loads(marker.read_text(encoding='utf-8'))
        if not isinstance(own, dict):
            return ''
        now = time.time()
        others = []
        for other in STATE_DIR.glob('*.json'):
            if other == marker or not MARKER_RE.fullmatch(other.name):
                continue
            try:
                data = json.loads(other.read_text(encoding='utf-8'))
                updated_at = float(data.get('updated_at'))
                first_at = float(data.get('first_at'))
            except (OSError, TypeError, ValueError, AttributeError):
                continue
            if updated_at < now - PARALLEL_STALE_SECONDS or not _same_workspace(cwd, normal_cwd(data.get('cwd'))):
                continue
            others.append((other.name, data, updated_at, first_at))
        others.sort(key=lambda item: (item[3], item[2]), reverse=True)
        reported = own.get('parallel_reported')
        reported = [name for name in reported if isinstance(name, str)] if isinstance(reported, list) else []
        if first:
            new = [item for item in others if item[0] not in reported]
        else:
            try:
                last_report = float(own.get('parallel_last_report_at'))
            except (TypeError, ValueError):
                last_report = float(own.get('first_at') or now)
            new = [item for item in others if item[3] > last_report and item[0] not in reported]
        chosen = new[:PARALLEL_MAX]
        if not chosen:
            if first and 'parallel_last_report_at' not in own:
                own.update(parallel_last_report_at=now, parallel_reported=reported)
                _atomic_write(marker, json.dumps(own, ensure_ascii=False))
            return ''
        own.update(parallel_last_report_at=now, parallel_reported=reported + [item[0] for item in chosen])
        _atomic_write(marker, json.dumps(own, ensure_ascii=False))
        topics = []
        for _, data, updated_at, _ in chosen:
            topic = ' '.join(str(data.get('topic') or 'no topic').split())[:TOPIC_CHARS]
            topics.append(f"'{topic}' ({max(0, int((now - updated_at) // 60))} min ago)")
        return (f'[Parallel session] {len(chosen)} more session(s) open in this folder: '
                + ', '.join(topics)
                + '. Re-read a file from disk before you change it; git status before a commit.')
    except Exception as exc:  # noqa: BLE001 - the note never costs the prompt
        log_error(f'parallel note: {type(exc).__name__}: {exc}')
        return ''


def background_refresh() -> None:
    """Start `recall.py --update` detached, at most once per REFRESH_GAP_SECONDS; never wait.

    The child must not inherit the hook's pipes: an orphan that did once kept a harness waiting
    for EOF for hours.
    """
    lock = STATE_DIR / 'refresh.lock'
    try:
        if time.time() - lock.stat().st_mtime < REFRESH_GAP_SECONDS:
            return
    except OSError:
        pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding='utf-8')
    options: dict = {}
    if os.name == 'nt':
        options['creationflags'] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                    | subprocess.CREATE_NO_WINDOW)
    else:
        options['start_new_session'] = True
    env = {**os.environ, INVOKED_ENV: 'prompt_recall', 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    subprocess.Popen([sys.executable, str(SCRIPT_DIR / 'recall.py'), '--update'], cwd=str(SCRIPT_DIR),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, env=env, **options)


def _words(text: str) -> set[str]:
    return {term for term in recall.terms(text) if not term.endswith('*')}


def strict_filter(prompt: str, results: list[dict]) -> list[dict]:
    """V3's strict per-turn rule over the bm25 index; each kept row's score becomes its weight."""
    stored = recall._load_index('bm25')['docs']
    vocabularies = {key: {term for chunk in record.get('chunks') or [] for term in chunk.get('tf', {})
                          if not term.endswith('*')}
                    for key, record in stored.items()}
    vocabularies = {key: words for key, words in vocabularies.items() if words}
    total = max(1, len(vocabularies))
    wanted = _words(prompt)
    frequency = {term: sum(1 for words in vocabularies.values() if term in words) for term in wanted}
    idf_max = math.log((total + 1) / 2) + 1
    kept = []
    for row in results:
        words = vocabularies.get(row['path'], set())
        shared = {term for term in wanted & words if frequency[term] * 2 <= total}
        if len(shared) < STRICT_MIN_SHARED:
            continue
        weight = sum(math.log((total + 1) / (frequency[term] + 1)) + 1 for term in shared)
        weight = weight / idf_max / math.log(10 + len(words))
        if weight >= STRICT_MIN_WEIGHT:
            kept.append({**row, 'score': round(weight, 4)})
    kept.sort(key=lambda row: (-row['score'], row['path']))
    return kept


def find(prompt: str, first: bool) -> list[dict]:
    """The hits this prompt deserves (see the module docstring); raises recall.RecallError."""
    engine, results = recall.search(prompt, k=CANDIDATES, refresh=False)
    if engine == 'bm25':
        results = strict_filter(prompt, results)
    elif not first:
        results = [row for row in results if row['score'] >= HIGH_SCORE]
    return results[:MAX_RESULTS]


def _stamp(vault: Path, key: str) -> str:
    """A receipt is a dated claim a later one may correct; a task is live state, read now.

    A receipt once said "no push, by design"; undated, it read as the current state.
    """
    try:
        head = (vault / key).read_text(encoding='utf-8-sig')[:2000]
    except (OSError, UnicodeError):
        return (' [receipt, historical]' if key.startswith('receipts/')
                else ' [task]' if key.startswith('tasks/') else '')
    if key.startswith('receipts/'):
        found = re.search(r'"created_at":\s*"(\d{4}-\d{2}-\d{2})', head)
        return f' [receipt {found.group(1)}, historical]' if found else ' [receipt, historical]'
    if key.startswith('tasks/'):
        meta = recall._meta(head) or {}
        due = f", due {str(meta['due_at'])[:10]}" if meta.get('due_at') else ''
        return f" [task {meta.get('status', '?')}{due}]"
    return ''


def format_hits(vault: Path, hits: list[dict]) -> str:
    lines = [HEADER]
    for number, hit in enumerate(hits, start=1):
        preview = ' '.join(str(hit.get('preview') or '').split())[:PREVIEW_CHARS]
        lines.append(f"{number}. {vault.as_posix()}/{hit['path']} ({hit['score']:.2f})"
                     f"{_stamp(vault, hit['path'])} — {preview}")
    text = '\n'.join(lines)
    return text if len(text) <= MAX_CONTEXT_CHARS else text[:MAX_CONTEXT_CHARS].rstrip() + ' […]'


def _unfinished(vault: Path, why: str) -> str:
    script = (vault / '.brain' / 'scripts' / 'recall.py').as_posix()
    return (f'[Memory: Recall] Pre-check {why}; if history matters, search yourself: '
            f'python "{script}" "<question>" --k 5 --json (empty means not found, not absent; '
            'a preview is not the answer, open the file).')


def context_for(raw: str) -> str:
    """The text to add for this hook input; '' adds nothing."""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return ''
    if not isinstance(payload, dict):
        return ''
    prompt = prompt_text(payload)
    if (len(prompt.strip()) < MIN_PROMPT_CHARS or any(marker in prompt for marker in OPT_OUT)
            or not human_turn(payload, prompt)):
        return ''
    session_id = payload.get('session_id')
    if not isinstance(session_id, str) or not session_id:
        return ''
    prompt = prompt[:PROMPT_MAX_CHARS]
    cwd = normal_cwd(payload.get('cwd'))
    _cleanup_markers()
    repeated, first = repeat_gate(session_id, prompt, cwd)
    if repeated:
        return ''
    parallel = parallel_note(session_id, cwd, first)
    vault = config.vault_path(config.load())
    try:
        background_refresh()
    except OSError as exc:
        log_error(f'background refresh did not start: {exc}')

    outcome: dict = {}

    def search() -> None:
        try:
            outcome['hits'] = find(prompt, first)
        except Exception as exc:  # noqa: BLE001 - reported below, never raised into the hook
            outcome['error'] = f'{type(exc).__name__}: {exc}'

    worker = threading.Thread(target=search, daemon=True)
    worker.start()
    worker.join(BUDGET_SECONDS)
    lines = []
    if 'hits' in outcome and outcome['hits']:
        lines.append(format_hits(vault, outcome['hits']))
    elif worker.is_alive():
        log_error(f'recall did not finish within {BUDGET_SECONDS:.0f} s (cold Ollama model?)')
        if first:
            lines.append(_unfinished(vault, f'did not finish within {BUDGET_SECONDS:.0f} s'))
    elif 'error' in outcome:
        log_error(outcome['error'])
        if first:
            lines.append(_unfinished(vault, f"could not run ({outcome['error'].splitlines()[0][:160]})"))
    elif first:
        lines.append(EMPTY)
    if parallel:
        lines.append(parallel)
    nonce = os.environ.get(SMOKE_ENV, '').strip()
    if SMOKE_RE.fullmatch(nonce):
        lines.append(f'[Memory: Recall Check] {nonce}')
    return '\n'.join(lines)


def emit(text: str) -> str:
    return json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': text}},
                      ensure_ascii=False)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # exit 2 would make the harness block the prompt
        self.print_usage(sys.stderr)
        print(f'{self.prog}: error: {message}', file=sys.stderr)
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prompt-submit', action='store_true', required=True,
                        help='Read the UserPromptSubmit hook JSON on stdin (the registered form).')
    parser.parse_args(argv)
    if os.environ.get(INVOKED_ENV):
        return 0
    try:
        raw = '' if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read().decode('utf-8', 'replace')
        text = context_for(raw)
    except Exception as exc:  # noqa: BLE001 - the hook never fails the prompt
        log_error(f'{type(exc).__name__}: {exc}')
        return 0
    if text:
        sys.stdout.write(emit(text) + '\n')
        sys.stdout.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
