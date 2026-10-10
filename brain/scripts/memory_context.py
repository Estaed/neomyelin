#!/usr/bin/env python3
"""Build the bounded SessionStart context from the companion sources.

Registered as a SessionStart hook (render_hooks.py). The vault and the names come from
config.py; the hook prints one JSON object whose `additionalContext` the harness adds to
the new session:

    [Memory: Identity]      who the assistant is, for whom, in which language
    [Memory: Session]       the receipt `session` value the receipt reminder (receipt_gate.py) matches
    [Memory: Rules]         <companion>/Rules.md, whole
    [Memory: Core]          <companion>/Core.md, whole
    [Memory: Personality]   <companion>/Personality.md, whole
    [Memory: Last Receipt]  newest receipt of this project (receipts/), clipped
    [Memory: Reminders]     the brain's alarms first and never cut (health: nightly job errors and
                            the last doctor --save; failed nightly steps; reaction debt; evolution;
                            knowledge debt), then due tasks, due decisions and the other open
                            tasks of this project (tasks/); five lines. A session
                            inside the vault keeps two of them for hygiene: a project folder under
                            `projects_root` with no card, a knowledge note missing from the index

Which project a session is in comes from the hook's `cwd` (else the process's): inside the vault
it is the vault; in a folder a project card in config.PROJECTS points at (frontmatter `path:` and
`label:`), that project; else the label the vault already knows from the folder's names, else the
folder's own name (see project_label). So a folder nothing names shows only its own record and the
due tasks, never another project's; the vault-level record (every open task, the hygiene lines)
is shown only inside the vault. A receipt belongs to a label when its first line starts `[label]`;
a task when its `project` is the label. Each reminder source fails alone: a broken one becomes one
warning line, never a broken hook.

Labels are English; the vault text under them stays in whatever language it was written in.
Only the growing sections are clipped. Rules, Core and Personality print whole: a byte cap
once silently cut the two strongest personality lines, so their owners keep them short and
they are squeezed only as a last resort.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402

# 1100 -> 1600: long receipts lost their closing "open items" part.
CAPS = {
    'last_receipt': 1600,
    'reminders': 900,
}
TOTAL_CAP = 11500
# The harness wall is not bytes but the JS string `.length`, i.e. UTF-16 code units (~10000).
# The byte budget keeps the sections fair to each other; this cap keeps the whole block off the
# wall. The 200-unit margin is deliberate: an emoji in a path counts two units in JS, one in
# Python's len().
HARNESS_CAP = 9800
STALE_DAYS = 14
# A process the layer starts itself (recall warm-up and nightly model calls) sets this;
# such a session must not load memory or open a record of its own.
INVOKED_ENV = 'NEOMYELIN_INVOKED_BY'
# scripts/smoke_harness.py sets a random hex token here; the hook echoes it so the smoke run can
# prove the block reached the model. Only hex is accepted, so nothing else can ride along.
SMOKE_ENV = 'NEOMYELIN_SMOKE_NONCE'
SMOKE_RE = re.compile(r'[0-9a-f]{8,64}')

HEADINGS = {
    'rules': '[Memory: Rules]',
    'core': '[Memory: Core]',
    'personality': '[Memory: Personality]',
    'last_receipt': '[Memory: Last Receipt]',
    'reminders': '[Memory: Reminders]',
}
FILES = {'rules': 'Rules.md', 'core': 'Core.md', 'personality': 'Personality.md'}


def local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _read(path: Path, warnings: list[str]) -> str:
    try:
        return path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return ''
    except (OSError, UnicodeError) as exc:
        warnings.append(f'[Memory warning] {path.name} unreadable: {exc}')
        return ''


def _more(source: str) -> str:
    return f'[clipped — full text: {source}]'


def _harness_length(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def _clip(text: str, limit: int, note: str) -> str:
    if len(text.encode('utf-8')) <= limit:
        return text
    note_bytes = note.encode('utf-8')
    keep = max(0, limit - len(note_bytes) - 1)
    if keep <= 0:
        return note
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        line_bytes = len(line.encode('utf-8'))
        added = line_bytes if not kept else line_bytes + 1
        if used + added > keep:
            break
        kept.append(line)
        used += added
    return note if not kept else '\n'.join(kept) + '\n' + note


def _frontmatter_lines(text: str) -> list[str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return []
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == '---')
    except StopIteration:
        return []
    return lines[1:end]


def _frontmatter_body(text: str) -> str:
    lines = text.splitlines()
    if len(lines) < 2 or lines[0].strip() != '---':
        return text.strip()
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == '---')
    except StopIteration:
        return text.strip()
    return '\n'.join(lines[end + 1:]).strip()


def _simple_frontmatter(text: str) -> dict[str, str]:
    """`key: value` lines of a YAML-style frontmatter (a project card); quotes around a value go.
    A JSON object frontmatter (brain.py's own format) is read as it is."""
    lines = _frontmatter_lines(text)
    if '\n'.join(lines).lstrip().startswith('{'):
        try:
            data = json.loads('\n'.join(lines))
        except ValueError:
            data = None
        if isinstance(data, dict):
            return {str(key): value for key, value in data.items() if isinstance(value, str)}
    values: dict[str, str] = {}
    for line in lines:
        match = re.match(r'^([A-Za-z_][\w-]*):\s*(.*?)\s*$', line)
        if not match:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[match.group(1)] = value
    return values


def _norm_path(path: Path | str) -> str:
    return str(Path(path).expanduser().resolve()).replace('\\', '/').rstrip('/').casefold()


def _inside(path: Path | str, parent: Path | str) -> bool:
    child = _norm_path(path)
    base = _norm_path(parent)
    return child == base or child.startswith(base + '/')


def _task_metadata(path: Path) -> dict | None:
    """Task metadata: one JSON object between the `---` lines (brain.py writes it)."""
    try:
        value = json.loads('\n'.join(_frontmatter_lines(path.read_text(encoding='utf-8-sig'))))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _project_cards(vault: Path) -> list[tuple[Path, str]]:
    """(project folder, label) of every card in config.PROJECTS, sub-folders included.

    A card is a note whose frontmatter has `path:` (the project folder, absolute; forward
    slashes and `~` are fine) and `label:`. A card without both, or with a relative path, is no
    card.
    """
    cards: list[tuple[Path, str]] = []
    root = vault / config.PROJECTS
    if not root.is_dir():
        return cards
    for path in sorted(root.rglob('*.md')):
        try:
            fields = _simple_frontmatter(path.read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError):
            continue
        folder = (fields.get('path') or '').strip()
        label = (fields.get('label') or '').strip()
        if folder and label and Path(folder).expanduser().is_absolute():
            cards.append((Path(folder).expanduser(), label))
    return cards


def _known_labels(vault: Path, cards: list[tuple[Path, str]]) -> set[str]:
    labels = {label for _, label in cards if label}
    tasks = vault / 'tasks'
    if tasks.is_dir():
        for path in tasks.glob('*.md'):
            metadata = _task_metadata(path)
            project = metadata.get('project') if metadata else None
            if isinstance(project, str) and project.strip():
                labels.add(project.strip())
    return labels


def _whole_phrase(text: str, label: str) -> bool:
    return re.search(rf'(?<![^\W_]){re.escape(label)}(?![^\W_])', text,
                     flags=re.IGNORECASE) is not None


def project_label(cwd: Path | str, vault: Path | str,
                  cards: list[tuple[Path, str]] | None = None) -> str:
    """The receipt/task label for a working directory.

    Inside the vault it is the vault's own folder name. Elsewhere the label of the project card
    whose `path` holds the folder (the deepest one wins); else the longest known label (card
    labels and task `project`s) found, as a whole phrase, in the folder name or the first
    heading of its AGENTS.md/CLAUDE.md, then in its parent folders' names; else the folder name.
    """
    cwd_path = Path(cwd).expanduser().resolve()
    vault_path = Path(vault).expanduser().resolve()
    if _inside(cwd_path, vault_path):
        return vault_path.name

    cards = _project_cards(vault_path) if cards is None else cards
    card_matches = [(path, label) for path, label in cards if _inside(cwd_path, path)]
    if card_matches:
        return max(card_matches, key=lambda item: len(_norm_path(item[0])))[1]

    labels = _known_labels(vault_path, cards)
    own_texts = [cwd_path.name]
    for name in ('AGENTS.md', 'CLAUDE.md'):
        try:
            heading = next(
                (line[2:].strip() for line in (cwd_path / name).read_text(encoding='utf-8-sig').splitlines()
                 if line.startswith('# ')),
                '',
            )
        except (OSError, UnicodeError):
            heading = ''
        if heading:
            own_texts.append(heading)
            break

    levels = [own_texts]
    levels.extend([[parent.name] for parent in cwd_path.parents if parent.name])
    for texts in levels:
        matches = [label for label in labels if any(_whole_phrase(text, label) for text in texts)]
        if matches:
            return max(matches, key=lambda label: (len(label), label.casefold()))
    return cwd_path.name


def _slug(value: str) -> str:
    output: list[str] = []
    separator = False
    for char in value.casefold():
        if char.isalnum():
            if separator and output:
                output.append('-')
            output.append(char)
            separator = False
        else:
            separator = True
    return ''.join(output).strip('-')


def _latest_receipt(vault: Path, label: str) -> tuple[str, str] | None:
    """Newest receipt whose first line starts `[label]` or whose event_id carries the label."""
    candidates: list[tuple[dt.datetime, Path, str]] = []
    label_prefix = f'[{label}]'.casefold()
    label_slug = _slug(label)
    for path in (vault / 'receipts').glob('*.md'):
        try:
            text = path.read_text(encoding='utf-8-sig')
            metadata = json.loads('\n'.join(_frontmatter_lines(text)))
            body = _frontmatter_body(text)
            created = dt.datetime.fromisoformat(str(metadata['created_at']).replace('Z', '+00:00'))
            if created.tzinfo is None:
                created = created.replace(tzinfo=dt.timezone.utc)
            event_id = str(metadata.get('event_id') or '').casefold()
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            continue
        # A Hermes agent reads the open web: its receipts stay in receipts/ but are never
        # injected into a session as trusted memory.
        if str(metadata.get('harness') or '').casefold() == 'hermes':
            continue
        first = next((line.strip() for line in body.splitlines() if line.strip()), '')
        # Word boundary: a label "note" must not match the receipt "2026-01-01-notebook-...".
        if not (first.casefold().startswith(label_prefix)
                or (label_slug and f'-{label_slug}-' in f'-{_slug(event_id)}-')):
            continue
        candidates.append((created, path, body))
    if not candidates:
        return None
    created, path, body = max(candidates, key=lambda item: item[0])
    local = created.astimezone().strftime('%Y-%m-%d %H:%M')
    return f'{local}\n{body}', f'receipts/{path.name}'


def _old(path: Path) -> bool:
    try:
        return path.stat().st_mtime < time.time() - STALE_DAYS * 24 * 3600
    except OSError:
        return False


def _task_lines(vault: Path, today: dt.date, label: str,
                vault_mode: bool) -> tuple[list[str], list[str], list[str], str]:
    """(due, unreadable, the rest, stale) task lines; the rest nearest date first.

    Stale: an undated open task untouched for 14+ days. It gets one line of its own because
    the rest are sorted by date into a five-line cap and an old undated task was always cut.
    A dated task is reminded on its date anyway and is not counted here.
    """
    tasks_dir = vault / 'tasks'
    if not tasks_dir.is_dir():
        return [], [], [], ''

    due: list[tuple[dt.date, Path, dict]] = []
    active: list[tuple[dt.date | None, Path, dict]] = []
    malformed: list[str] = []
    for path in sorted(tasks_dir.glob('*.md')):
        relative = path.relative_to(vault).as_posix()
        metadata = _task_metadata(path)
        if metadata is None:
            malformed.append(f'- unreadable: {relative}')
            continue

        status = str(metadata.get('status', '')).casefold()
        if status in {'done', 'cancelled'}:
            continue
        raw_due = metadata.get('due_at')
        due_date = None
        if isinstance(raw_due, str) and raw_due.strip():
            try:
                due_date = dt.date.fromisoformat(raw_due[:10])
            except ValueError:
                pass
        task_project = str(metadata.get('project') or '')
        if (not vault_mode and task_project.casefold() != label.casefold()
                and not (due_date is not None and due_date <= today)):
            continue
        if due_date is not None and due_date <= today:
            due.append((due_date, path, metadata))
        elif status in {'active', 'waiting', 'blocked'}:
            active.append((due_date, path, metadata))

    def render(path: Path, metadata: dict, due_date: dt.date | None = None) -> str:
        title = str(metadata.get('title') or path.stem)
        next_action = str(metadata.get('next_action') or 'no next step given')
        task_project = str(metadata.get('project') or 'no project')
        prefix = f'- [due {due_date.isoformat()}] ' if due_date is not None else '- '
        line = f'{prefix}{title} — {next_action} ({task_project})'
        return line + (f' — {STALE_DAYS}+ days: close or refresh' if _old(path) else '')

    stale_names = [path.stem for due_date, path, _ in active if due_date is None and _old(path)]
    stale = (f"- [stale?] undated task untouched for {STALE_DAYS}+ days: {', '.join(stale_names)} — "
             'still valid? close it, date it or refresh it') if stale_names else ''
    rank = {'active': 0, 'waiting': 1, 'blocked': 2}
    active.sort(key=lambda item: (item[0] or dt.date.max,
                                  rank.get(str(item[2].get('status', '')).casefold(), 3), item[1].name))
    return ([render(path, metadata, due_date) for due_date, path, metadata in sorted(due)],
            malformed,
            [render(path, metadata, due_date) for due_date, path, metadata in active],
            stale)


def _line(line: str, cap: int = 160) -> str:
    """One reminder stays one short line; the detail lives in the task. A single long
    next_action once ate the whole block and the clip dropped every other reminder."""
    if len(line) <= cap:
        return line
    tail = re.search(r' \([^()]{1,40}\)$', line)  # the trailing project label
    keep = tail.group(0) if tail else ''
    return line[:cap - len(keep) - 1].rstrip() + '…' + keep


HEALTH_DAYS = 3


def _health_lines(vault: Path, today: dt.date) -> list[str]:
    """At most two lines: a recent nightly-job error or warning, and the last doctor --save.

    Without them a broken layer stays silent: doctor only helps if someone reads its result.
    """
    state = vault / '.brain' / '.state'
    lines = []
    try:
        jobs = json.loads((state / 'health.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        jobs = {}
    if isinstance(jobs, dict):
        stamp = jobs.get('ts')
        recent = isinstance(stamp, (int, float)) and today - dt.date.fromtimestamp(stamp) <= dt.timedelta(days=HEALTH_DAYS)
        warnings = [w for w in jobs.get('warnings') or [] if isinstance(w, str) and w[:10] >= (today - dt.timedelta(days=HEALTH_DAYS)).isoformat()]
        if jobs.get('error') and recent:
            lines.append(f"Health: {jobs.get('component', 'nightly')} error: {jobs['error']}")
        elif warnings:
            lines.append(f'Health: {warnings[-1][11:]}')
    try:
        doctor = json.loads((state / 'doctor.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        doctor = {}
    if isinstance(doctor, dict):
        # The check names, not the first detail: a long detail filled the 160-character line and
        # the other checks never showed. Older doctor.json files have no titles.
        titles = doctor.get('titles')
        if isinstance(titles, list) and titles:
            lines.append(f"Health check: {', '.join(str(title) for title in titles)} "
                         '(python .brain/scripts/doctor.py)')
        elif doctor.get('error'):
            lines.append(f"Health check error: {doctor['error']}")
        elif doctor.get('warnings'):
            count = len(doctor['warnings'])
            lines.append(f"Health check: {count} warning(s), first: {doctor['warnings'][0]} "
                         '(python .brain/scripts/doctor.py)')
    return lines


def _nightly_line(vault: Path) -> str:
    """The steps that failed in the last nightly run (nightly.log), a run still going included. A
    failed step that is not a model call (recall, the daily commit) reaches health.json nowhere else."""
    try:
        lines = (vault / '.brain' / '.state' / 'nightly.log').read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        return ''
    failed: list[str] = []
    for line in reversed(lines):
        parts = line.split(' ', 2)
        if len(parts) < 3:
            continue
        if parts[1] == '[START]':
            break
        if parts[1] == '[ERROR]':
            failed.append(parts[2].split(':', 1)[0])
    if not failed:
        return ''
    return (f"Nightly run: failed step(s) {', '.join(dict.fromkeys(reversed(failed)))} "
            '(.brain/.state/nightly.log)')


REACTION_STATE = '.brain/.state/reactions.json'


def _reaction_debt_line(vault: Path) -> str:
    """Praise or objections the nightly reaction audit (reactions.py) found written nowhere."""
    try:
        open_items = json.loads((vault / REACTION_STATE).read_text(encoding='utf-8-sig')).get('open', [])
        if not isinstance(open_items, list) or not open_items:
            return ''
        first = str(open_items[0].get('quote') or '?') if isinstance(open_items[0], dict) else '?'
    except (OSError, UnicodeError, ValueError, AttributeError):
        return ''
    short = first if len(first) <= 45 else first[:44].rstrip() + '…'
    return (f'- [reaction debt] {len(open_items)} praise/objection(s) not written down ("{short}") — '
            'python .brain/scripts/reactions.py list')


DECISION_RE = re.compile(r'(?ms)^## Decision:[ \t]*(.+?)[ \t]*$(.*?)(?=^## |\Z)')


def _decision_lines(companion: Path | None, today: dt.date) -> list[str]:
    """Decisions whose check date has come and that have no outcome yet (Decisions.md).

    Only a pointer: the expectation stays in the file, where it does not eat the budget.
    Quoted lines (the template's example) never match, because they start with `>`.
    """
    if companion is None:
        return []
    path = companion / 'Decisions.md'
    try:
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        return []
    lines = []
    for match in DECISION_RE.finditer(text):
        body = match.group(2)
        if re.search(r'(?m)^\*\*Outcome:\*\*[ \t]*\S', body):
            continue
        check = re.search(r'(?m)^\*\*Check:\*\*[ \t]*(\S+)', body)
        try:
            due = dt.date.fromisoformat(check.group(1)[:10]) if check else None
        except ValueError:
            due = None
        if due is None:
            lines.append(f'Decision "{match.group(1)}": check date unreadable in {path.name}')
        elif due <= today:
            lines.append(f'Decision check due ({due.isoformat()}): "{match.group(1)}". Ask the user '
                         f'how it went and fill **Outcome:** in {path.name}')
    return lines


def _evolution_line(companion: Path | None) -> str:
    """One line when proposed fixes in Evolution.md still wait for the user's decision."""
    if companion is None:
        return ''
    try:
        text = (companion / 'Evolution.md').read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        return ''
    blocks = re.findall(r'(?ms)^### (?:Candidate|Idea)[^\n]*\n(.*?)(?=^### |^## |\Z)', text)
    pending = sum(1 for body in blocks if re.search(r'(?mi)^- Decision:\s*pending\b', body))
    if not pending:
        return ''
    return (f'Evolution: {pending} proposed fix(es) wait for a decision in Evolution.md '
            '(accept as rule, tool, patch or promote; or reject)')


AUDIT_STATE = '.brain/.state/knowledge-audit.json'


def _knowledge_debt_line(vault: Path) -> str:
    """What the nightly knowledge audit (knowledge_audit.py) left for a person to check.

    Lessons to verify and possible conflicts with a note; when the audit failed, the lessons
    still waiting to be filed.
    """
    try:
        payload = json.loads((vault / AUDIT_STATE).read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError):
        return ''
    if not isinstance(payload, dict):
        return ''

    def count(key: str) -> int:
        value = payload.get(key)
        return len(value) if isinstance(value, list) else 0

    if payload.get('status') == 'error':
        return (f"- [knowledge debt] The nightly knowledge audit failed; {count('pending_items')} "
                f'lesson(s) wait — {AUDIT_STATE}')
    parts = []
    gaps = count('gaps')
    if gaps:
        parts.append(f'{gaps} lesson(s) to verify')
    if count('conflicts'):
        parts.append(f"{count('conflicts')} possible conflict(s) to check")
    return (f"- [knowledge debt] {'; '.join(parts)} — {AUDIT_STATE}; "
            'checked: knowledge_audit.py close <id>') if parts else ''


def _hidden(path: Path) -> bool:
    """A dot folder, a folder Windows marks hidden, or one that cannot be read: not a project."""
    try:
        attributes = getattr(path.stat(), 'st_file_attributes', 0)
    except OSError:
        return True
    return path.name.startswith('.') or bool(attributes & getattr(stat, 'FILE_ATTRIBUTE_HIDDEN', 0))


def _hygiene_lines(vault: Path, cards: list[tuple[Path, str]], projects: Path | None) -> list[str]:
    """How the vault drifts unnoticed: a folder under `projects_root` that no card in
    config.PROJECTS points at (sessions there never learn their project), and a knowledge note
    with no row in knowledge/index.md."""
    lines: list[str] = []
    if projects is not None:
        known = {_norm_path(path) for path, _ in cards} | {_norm_path(vault)}
        try:
            missing = sorted(path.name for path in projects.iterdir()
                             if path.is_dir() and not _hidden(path) and _norm_path(path) not in known)
        except OSError:
            missing = []
            lines.append(f'- [hygiene] projects_root unreadable: {projects.as_posix()} (.brain/config.json)')
        if missing:
            lines.append(f"- [hygiene] project folder(s) with no card in {config.PROJECTS}: {', '.join(missing)}")
    try:
        index = (vault / 'knowledge' / 'index.md').read_text(encoding='utf-8-sig').casefold()
        unindexed = sorted(path.stem for path in (vault / 'knowledge' / 'concepts').glob('*.md')
                           if f'[[{path.stem}'.casefold() not in index)
    except (OSError, UnicodeError):
        unindexed = []
    if unindexed:
        lines.append(f"- [hygiene] knowledge note(s) with no row in knowledge/index.md: {len(unindexed)} "
                     f"(e.g. {', '.join(unindexed[:2])})")
    return lines


def _quiet(default, warnings: list[str] | None, function, *args):
    """function(*args), or `default` when it raises: one broken source costs its own line (and
    leaves a warning), never the session start."""
    try:
        return function(*args)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        if warnings is not None:
            warnings.append(f"[Memory warning] {getattr(function, '__name__', 'a memory source')} failed: "
                            f'{type(exc).__name__}: {str(exc)[:120]}')
        return default


def _reminders(vault: Path, today: dt.date, label: str, vault_mode: bool,
               companion: Path | None = None, cards: list[tuple[Path, str]] | None = None,
               projects: Path | None = None, warnings: list[str] | None = None) -> str:
    """Order: the brain's own alarms (health, failed nightly steps, reaction debt, evolution,
    knowledge debt), due tasks, due decisions, unreadable tasks, stale, the rest. Five lines at
    most, but the alarms are never cut: on the original brain, overdue tasks pushed the health
    line out for four days and a broken check reached no one. In a session inside the vault up
    to two hygiene lines keep their place at the end."""
    due, malformed, rest, stale = _quiet(([], [], [], ''), warnings, _task_lines, vault, today, label, vault_mode)
    alarms = [line for line in (*_quiet([], warnings, _health_lines, vault, today),
                                _quiet('', warnings, _nightly_line, vault),
                                _quiet('', warnings, _reaction_debt_line, vault),
                                _quiet('', warnings, _evolution_line, companion),
                                _quiet('', warnings, _knowledge_debt_line, vault)) if line]
    lines = (alarms + due + _quiet([], warnings, _decision_lines, companion, today)
             + malformed + ([stale] if stale else []) + rest)
    if vault_mode:
        hygiene = _quiet([], warnings, _hygiene_lines, vault, cards or [], projects)[:2]
        lines = lines[:max(len(alarms), 5 - len(hygiene))] + hygiene
    else:
        lines = lines[:max(len(alarms), 5)]
    return '\n'.join(_line(line) for line in lines)


def _start_recall_warmup(warnings: list[str]) -> None:
    """Warm recall in the background so the first query of the session is fast."""
    script = SCRIPT_DIR / 'recall.py'
    if not script.is_file():
        return
    env = os.environ.copy()
    env[INVOKED_ENV] = 'memory_context'
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    try:
        subprocess.Popen([sys.executable, str(script), '--warm'], cwd=SCRIPT_DIR, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    except OSError as exc:
        warnings.append(f'[Memory warning] recall warm-up did not start: {exc}')


def _start_nightly(cfg: dict, warnings: list[str]) -> None:
    """Detach the due run; the child owns the work and completion stamp.

    Due means the last run is older than the latest `nightly_at`, so a night with no session
    after that time is caught up by the next session, morning included (someone who works only
    mornings never got a run). Before the first run ever, it waits for `nightly_at`.
    """
    if os.environ.get(INVOKED_ENV):
        return
    now = local_now()
    hour, minute = map(int, cfg.get('nightly_at', '21:00').split(':'))
    latest = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < latest:
        latest -= dt.timedelta(days=1)
    state = config.vault_path(cfg) / '.brain' / '.state'
    stamp = state / 'nightly.last-run'
    try:
        last = dt.datetime.fromisoformat(stamp.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        last = None
    if last is None:
        if now.time() < dt.time(hour, minute):
            return
    else:
        if last.tzinfo is None:
            last = last.astimezone()
        if last >= latest:
            return
    if (state / 'nightly.lock').exists():
        return
    script = SCRIPT_DIR / 'nightly.py'
    if not script.is_file():
        return
    try:
        state.mkdir(parents=True, exist_ok=True)
        starting = state / 'nightly.starting'
        try:
            with starting.open('x', encoding='utf-8', newline='\n') as handle:
                handle.write(now.isoformat() + '\n')
        except FileExistsError:
            if now.timestamp() - starting.stat().st_mtime < 4 * 3600:
                return
            starting.unlink(missing_ok=True)
            with starting.open('x', encoding='utf-8', newline='\n') as handle:
                handle.write(now.isoformat() + '\n')
        env = os.environ.copy()
        env[INVOKED_ENV] = 'memory_context'
        env['PYTHONUTF8'] = '1'
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        subprocess.Popen([sys.executable, str(script)], cwd=config.vault_path(cfg), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    except OSError as exc:
        (state / 'nightly.starting').unlink(missing_ok=True)
        warnings.append(f'[Memory warning] nightly run did not start: {exc}')


def _identity(cfg: dict, vault: Path) -> str:
    return (f"[Memory: Identity] You are {cfg['assistant_name']}, {cfg['user_name']}'s assistant. "
            f"Speak and write vault notes in {cfg['language']}. Memory vault: {vault.as_posix()}")


def _emit(context: str) -> str:
    payload = {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': context}}
    return json.dumps(payload, ensure_ascii=False)


def session_start_context(hook_input: str, *, cfg: dict | None = None, emit: bool = True,
                          warm: bool = True) -> str:
    cfg = cfg if cfg is not None else config.load()
    warnings: list[str] = []
    vault = config.vault_path(cfg)
    companion = config.companion_dir(cfg)
    if companion is None:
        warnings.append('[Memory warning] several folders could be the companion folder; '
                        'set "companion_dir" in .brain/config.json')
    today = local_now().date()
    try:
        payload = json.loads(hook_input) if hook_input.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # The receipt reminder (receipt_gate.py) counts a receipt only with this session's value,
    # sha256(session_id)[:24]. The agent cannot compute it, so the hook prints it.
    session_id = payload.get('session_id')
    session_line = (f'[Memory: Session] Add "session": '
                    f'"{hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]}" to the receipt JSON.'
                    if isinstance(session_id, str) and session_id else '')
    nonce = os.environ.get(SMOKE_ENV, '').strip()
    check_line = f'[Memory: Check] {nonce}' if SMOKE_RE.fullmatch(nonce) else ''
    raw_cwd = payload.get('cwd')
    cwd = Path(raw_cwd).expanduser().resolve() if isinstance(raw_cwd, str) and raw_cwd else Path.cwd().resolve()
    vault_mode = _inside(cwd, vault)
    cards = _quiet([], warnings, _project_cards, vault)
    label = project_label(cwd, vault, cards)
    receipt = _latest_receipt(vault, label)

    def companion_file(key: str) -> str:
        return _frontmatter_body(_read(companion / FILES[key], warnings)) if companion else ''

    raw = {
        'rules': companion_file('rules'),
        'core': companion_file('core'),
        'personality': companion_file('personality'),
        'last_receipt': receipt[0] if receipt else '',
        'reminders': _reminders(vault, today, label, vault_mode, companion, cards,
                                config.projects_root(cfg), warnings),
    }
    companion_name = companion.name if companion else 'companion folder'
    sources = {
        'rules': f'{companion_name}/{FILES["rules"]}',
        'core': f'{companion_name}/{FILES["core"]}',
        'personality': f'{companion_name}/{FILES["personality"]}',
        'last_receipt': receipt[1] if receipt else 'receipts/',
        'reminders': 'tasks/',
    }
    values = {key: _clip(value, CAPS[key], _more(sources[key])) if key in CAPS else value
              for key, value in raw.items()}
    head = [_identity(cfg, vault)] + ([check_line] if check_line else [])

    if warm:
        _start_recall_warmup(warnings)
        _start_nightly(cfg, warnings)

    def build() -> str:
        sections = head + warnings + ([session_line] if session_line else [])
        for key, value in values.items():
            if value:
                sections.append(f'{HEADINGS[key]}\n{value}')
        return '\n\n'.join(sections)

    def squeeze(context: str, cap: int, measure) -> str:
        # Growing sections shrink first; the whole-printed files only as a last resort.
        for key in ('last_receipt', 'reminders', 'core', 'personality'):
            overflow = measure(context) - cap
            if overflow <= 0:
                break
            if not values[key]:
                continue
            note = _more(sources[key])
            allowance = len(values[key].encode('utf-8')) - overflow
            values[key] = (_clip(raw[key], allowance, note)
                           if allowance > len(note.encode('utf-8')) + 1 else note)
            context = build()
        return context

    context = build()
    context = squeeze(context, TOTAL_CAP, lambda text: len(text.encode('utf-8')))
    context = squeeze(context, HARNESS_CAP, _harness_length)
    if _harness_length(context) > HARNESS_CAP or len(context.encode('utf-8')) > TOTAL_CAP:
        context = '\n\n'.join(head + [
            f'[Memory warning] The session context did not fit in {TOTAL_CAP} bytes: '
            f'{FILES["rules"]} is over budget; shorten it.'] + ([session_line] if session_line else []))

    output = _emit(context)
    if emit:
        sys.stdout.write(output + '\n')
        sys.stdout.flush()
    return output


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--session-start', action='store_true',
                        help='Read the hook JSON on stdin and print the session context.')
    args = parser.parse_args(argv)
    if not args.session_start:
        parser.print_usage(sys.stderr)
        return 2
    if os.environ.get(INVOKED_ENV):
        return 0
    hook_input = '' if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read().decode('utf-8', 'replace')
    try:
        session_start_context(hook_input)
    except Exception as exc:  # noqa: BLE001 - a broken hook must say so in the session, not vanish
        sys.stdout.write(_emit(f'[Memory warning] NeoMyelin session context failed: '
                               f'{type(exc).__name__}: {exc}') + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
