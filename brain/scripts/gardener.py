#!/usr/bin/env python3
"""Record friction and propose fixes after recurrence on distinct days; close as reject a
proposal still pending 30 days after its last activity."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import difflib
import hashlib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import config
import stage

DEFAULT_THRESHOLD = 2
SEMANTIC_THRESHOLD = 0.62
CANDIDATE = re.compile(r'^### Candidate ([0-9a-f]{12})\s*$')
DECISION = re.compile(r'^- Decision:\s*(\S+)', re.MULTILINE)
DECISION_DATE = re.compile(r'^- Decision note:\s*(\d{4}-\d{2}-\d{2})\b', re.MULTILINE)
FAILED = re.compile(r'^- Failed: .* \(gardener\)\s*$')
SKILL_MARKER = re.compile(r'\(skill-gardener:\s*([0-9a-f]{12}),')
# A proposal still pending this many days after its last activity is closed as reject.
STALE_DAYS = 30
PROPOSAL = re.compile(r'^### (Candidate|Idea)\s+\S')
PENDING = re.compile(r'^- Decision:\s*pending\b', re.MULTILINE | re.IGNORECASE)
# The keys a closing rewrites, matched in any case like PENDING, so a block found pending is closed.
CLOSING_KEY = re.compile(r'- (Decision note|Decision|Type):', re.IGNORECASE)
MARKER_DATE = re.compile(r'\(skill-gardener:\s*[0-9a-f]{12},\s*\d+ cases?,\s*(\d{4}-\d{2}-\d{2})')
IDEA_SOURCE = re.compile(r'^- Source:\s*receipts/[^\s/\\]+\.md\s*$', re.MULTILINE)
# A knowledge-audit idea has no date of its own; this records the day the gardener first saw it pending.
IDEAS_SEEN = Path('.brain') / '.state' / 'evolution-ideas.json'


def _paths(cfg: dict) -> tuple[Path, Path]:
    companion = config.companion_dir(cfg)
    if companion is None:
        raise config.ConfigError('companion folder is ambiguous')
    return (config.vault_path(cfg) / '.brain' / 'reports' / 'agent-experience.jsonl',
            companion / 'Evolution.md')


def normalize(text: str) -> str:
    value = unicodedata.normalize('NFKD', text.strip().casefold().replace('ı', 'i'))
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r'["\x27\x60][^"\x27\x60]{2,}["\x27\x60]', ' QUOTE ', value)
    value = re.sub(r'(?:[A-Za-z]:)?[\\/][\w.\-\\/ ]+[\\/][\w.\-]+', ' PATH ', value)
    value = re.sub(r'\b[0-9a-f]{7,40}\b', ' HASH ', value)
    value = re.sub(r'\d+', ' NUMBER ', value)
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', value)).strip()


def fingerprint(category: str, symptom: str) -> str:
    return hashlib.sha256(f'{category.strip().casefold()}|{normalize(symptom)}'.encode()).hexdigest()[:12]


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open('r', encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise ValueError(f'invalid ledger line {number}') from exc
            if not isinstance(record, dict):
                raise ValueError(f'ledger line {number} is not an object')
            records.append(record)
    return records


def _groups(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        if isinstance(row.get('fingerprint'), str):
            groups.setdefault(row['fingerprint'], []).append(row)
    return groups


def _semantic_groups(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rows = [row for row in records if isinstance(row.get('fingerprint'), str)]
    if not rows:
        return {}
    try:
        import recall
        engine, reason = recall.choose_engine()
        if engine != 'ollama':
            raise RuntimeError(reason or 'Ollama unavailable')
        vectors = recall._embed([str(row.get('symptom', '')) for row in rows])
        if len(vectors) != len(rows):
            raise ValueError('embedding count mismatch')
    except (Exception, SystemExit) as exc:
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f'WARNING: semantic clustering unavailable ({reason[:160]}); fingerprints only')
        return _groups(rows)
    parent = list(range(len(rows)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    first: dict[str, int] = {}
    for index, row in enumerate(rows):
        parent[root(index)] = root(first.setdefault(row['fingerprint'], index))
    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if recall._cosine(vectors[left], vectors[right]) >= SEMANTIC_THRESHOLD:
                parent[root(left)] = root(right)
    clusters: dict[int, list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        clusters.setdefault(root(index), []).append(row)
    return {min(group, key=lambda item: str(item.get('created_at', item.get('ts', ''))))['fingerprint']: group
            for group in clusters.values()}


def _date(row: dict[str, Any]) -> dt.date | None:
    stamp = row.get('created_at', row.get('ts'))
    try:
        return dt.date.fromisoformat(str(stamp)[:10])
    except ValueError:
        return None


def _day_count(group: list[dict[str, Any]]) -> int:
    return len({_date(row) for row in group if _date(row) is not None})


def _representative(group: list[dict[str, Any]]) -> str:
    ordered = sorted(group, key=lambda row: str(row.get('created_at', row.get('ts', ''))))
    original = [row for row in ordered if not row.get('same_reason')]
    return str((original or ordered)[0].get('symptom', ''))


def _ledger_path(args: argparse.Namespace, cfg: dict) -> Path:
    return Path(args.ledger).expanduser() if getattr(args, 'ledger', None) else _paths(cfg)[0]


def _evolution_path(args: argparse.Namespace, cfg: dict) -> Path:
    return Path(args.evolution).expanduser() if getattr(args, 'evolution', None) else _paths(cfg)[1]


@contextlib.contextmanager
def _ledger_lock(path: Path):
    deadline = time.monotonic() + 30
    while True:
        with stage.exclusive_lock(path.with_name(f'.{path.name}.lock')) as acquired:
            if acquired:
                yield
                return
        if time.monotonic() >= deadline:
            raise RuntimeError('ledger lock timed out')
        time.sleep(0.1)


def cmd_record(args: argparse.Namespace, cfg: dict) -> int:
    path = _ledger_path(args, cfg)
    record = {'created_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
              'task': args.task.strip(), 'commit': args.commit.strip(),
              'category': args.category.strip(), 'symptom': args.symptom.strip(),
              'evidence': args.evidence.strip(), 'workaround': args.workaround.strip(),
              'proposal': args.proposal.strip()}
    record['fingerprint'] = fingerprint(record['category'], record['symptom'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_lock(path):
        rows = _read_ledger(path)
        if args.same:
            if args.same not in _groups(rows):
                print(f'--same {args.same}: fingerprint absent; record not added', file=sys.stderr)
                return 1
            record['fingerprint'] = args.same
            record['same_reason'] = 'linked by user'
        else:
            possible = []
            for fp, group in _groups(rows).items():
                ratio = difflib.SequenceMatcher(None, normalize(record['symptom']),
                                                normalize(_representative(group))).ratio()
                if fp != record['fingerprint'] and ratio >= 0.55:
                    possible.append((ratio, fp, _representative(group), len(group)))
            for ratio, fp, symptom, count in sorted(possible, reverse=True)[:3]:
                print(f'  similar record ({ratio:.2f}) [{fp}] {count} cases: {symptom[:90]}')
            if possible:
                print('  If the cause is the same, retry with --same <fingerprint>.')
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    print(f"recorded: fingerprint={record['fingerprint']} category={record['category']}")
    return 0


def _candidate_blocks(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines(keepends=True)
    starts = [(index, match.group(1)) for index, line in enumerate(lines)
              if (match := CANDIDATE.match(line.rstrip('\r\n')))]
    blocks = []
    for start, fp in starts:
        end = next((index for index in range(start + 1, len(lines))
                    if re.match(r'^#{1,3}\s', lines[index])), len(lines))
        blocks.append((fp, ''.join(lines[start:end])))
    return blocks


def _decisions(text: str, groups: dict[str, list[dict[str, Any]]]) -> list[tuple[str, str, dt.date | None, list[dict[str, Any]]]]:
    by_fp = {row['fingerprint']: group for group in groups.values() for row in group}
    output = []
    for fp, block in _candidate_blocks(text):
        match = DECISION.search(block)
        if not match or match.group(1).lower() not in {'rule', 'tool', 'patch', 'promote'}:
            continue
        date_match = DECISION_DATE.search(block)
        try:
            date = dt.date.fromisoformat(date_match.group(1)) if date_match else None
        except ValueError:
            date = None
        later = sorted((row for row in by_fp.get(fp, []) if date and _date(row) and _date(row) > date),
                       key=lambda row: str(row.get('created_at', row.get('ts', ''))))
        output.append((fp, match.group(1), date, later))
    return output


def _update_failures(text: str, decisions: list[tuple[str, str, dt.date | None, list[dict[str, Any]]]]) -> tuple[str, list[str]]:
    outcomes = {fp: later for fp, _, date, later in decisions if date is not None}
    lines = text.splitlines(keepends=True)
    starts = [(index, match.group(1)) for index, line in enumerate(lines)
              if (match := CANDIDATE.match(line.rstrip('\r\n')))]
    changes = []
    for start, fp in reversed(starts):
        end = next((index for index in range(start + 1, len(lines))
                    if re.match(r'^#{1,3}\s', lines[index])), len(lines))
        before = ''.join(lines[start:end])
        block = [line for line in lines[start:end] if not FAILED.match(line.rstrip('\r\n'))]
        later = outcomes.get(fp, [])
        if later:
            stamp = max(_date(row) for row in later)
            newline = '\r\n' if '\r\n' in before else '\n'
            label = f'- Failed: {len(later)} new cases, last {stamp.isoformat()} (gardener){newline}'
            at = next((index + 1 for index, line in enumerate(block)
                       if line.startswith('- Decision note:')),
                      next((index + 1 for index, line in enumerate(block)
                            if line.startswith('- Decision:')), len(block)))
            block.insert(at, label)
        if ''.join(block) != before:
            changes.append(fp)
            lines[start:end] = block
    return ''.join(lines), changes


def cmd_report(args: argparse.Namespace, cfg: dict) -> int:
    groups = _semantic_groups(_read_ledger(_ledger_path(args, cfg)))
    ordered = sorted(groups.items(), key=lambda item: (-_day_count(item[1]), item[0]))
    print(f'=== Repeated friction (>= {args.threshold} days) ===')
    for fp, group in ordered:
        if _day_count(group) >= args.threshold:
            stamps = [str(row.get('created_at', row.get('ts', ''))) for row in group]
            category = str(group[0].get('category', ''))
            print(f'[{fp}] {category}: {len(group)} cases / {_day_count(group)} days '
                  f'(first: {min(stamps)}, last: {max(stamps)}): {_representative(group)}')
            for proposal in sorted({str(row.get('proposal', '')).strip() for row in group if row.get('proposal')}):
                print(f'  proposal: {proposal}')
    print(f'=== Below threshold (< {args.threshold} days) ===')
    below = [(fp, group) for fp, group in ordered if _day_count(group) < args.threshold]
    for fp, group in below:
        print(f'[{fp}] {len(group)} cases / {_day_count(group)} days: {_representative(group)}')
    for index, (left, a) in enumerate(below):
        for right, b in below[index + 1:]:
            ratio = difflib.SequenceMatcher(None, normalize(_representative(a)),
                                            normalize(_representative(b))).ratio()
            if ratio >= 0.75:
                print(f'possibly same: [{left}] <-> [{right}] similarity={ratio:.2f}')
    evolution = _evolution_path(args, cfg)
    if not evolution.is_file():
        raise FileNotFoundError(evolution)
    print('=== Correction results ===')
    for fp, decision, date, later in _decisions(evolution.read_text(encoding='utf-8'), groups):
        outcome = 'undated' if date is None else f'{len(later)} new cases' if later else 'held'
        print(f'[{fp}] {decision} {date or ""}: {outcome}')
        for row in later:
            print(f"  {_date(row)}: {str(row.get('symptom', ''))[:80]}")
    return 0


def _candidate(fp: str, group: list[dict[str, Any]]) -> str:
    ordered = sorted(group, key=lambda row: str(row.get('created_at', row.get('ts', ''))))
    cases = ''.join(f"  - {str(row.get('created_at', row.get('ts', '')))[:10]} "
                    f"[{row.get('category', '')}] {str(row.get('symptom', ''))[:160]} -> "
                    f"{str(row.get('workaround', ''))[:100]}\n" for row in ordered[:8])
    return (f'### Candidate {fp}\n- Decision: pending\n'
            '- Type: TODO(agent) - rule | tool | patch:<skill> | promote | placed | reject\n'
            '- Decision note: TODO(agent) - dated decision and location\n'
            f'- Cases ({len(group)} cases / {_day_count(group)} days):\n{cases}'
            f"  *(skill-gardener: {fp}, {len(group)} cases, {str(ordered[-1].get('created_at', ''))})*\n")


def _day(stamp: object) -> dt.date | None:
    try:
        return dt.date.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        return None


def _close_stale(text: str, today: dt.date,
                 seen: dict[str, str] | None = None) -> tuple[str, list[str], list[str]]:
    """Close each proposal still pending STALE_DAYS days after its last activity as reject.

    Pending proposals piled up until the session-start line about them meant nothing. A
    candidate's last activity is the date in its gardener marker (its newest case when it was
    written). A knowledge-audit idea (an `### Idea` with a `- Source: receipts/...` line) has no
    date, and its receipt can be weeks older than the idea when the audit carried the item, so its
    last activity is the day the gardener first saw it pending. `seen` maps each such idea to that
    day and is rebuilt in place to hold the ideas still pending: a new one, or one whose recorded
    day is unreadable, gets today; a closed or decided one is dropped. Without `seen` an idea has
    no date. The block stays (a deleted candidate comes back); its Decision and Type become reject
    and its decision note says why and since when. The note replaces only the template
    placeholder; a note the user wrote is kept and the gardener's follows it. Returns the new
    text, the closed proposals, and the pending ones with no readable date, which are left alone.
    Each line keeps its own line ending.
    """
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if PROPOSAL.match(line)]
    closed: list[str] = []
    undated: list[str] = []
    pending_ideas: dict[str, str] = {}
    for start in reversed(starts):
        end = next((index for index in range(start + 1, len(lines))
                    if re.match(r'^#{1,3}\s', lines[index])), len(lines))
        block = ''.join(lines[start:end])
        if not PENDING.search(block):
            continue
        name = lines[start].strip().removeprefix('### ')
        marker = MARKER_DATE.search(block)
        if marker:
            since = _day(marker.group(1))
        elif seen is not None and block.startswith('### Idea') and IDEA_SOURCE.search(block):
            since = _day(seen.get(name)) or today
            pending_ideas[name] = since.isoformat()
        else:
            since = None
        if since is None:
            undated.append(name)
            continue
        if (today - since).days < STALE_DAYS:
            continue
        pending_ideas.pop(name, None)
        note = (f'- Decision note: {today.isoformat()} - no decision within {STALE_DAYS} days of its '
                f'last activity on {since.isoformat()}; closed as reject by the gardener')
        anchor = noted = None
        for index in range(start + 1, end):
            line = lines[index]
            ending = line[len(line.rstrip('\r\n')):]
            key = CLOSING_KEY.match(line)
            field = key.group(1).lower() if key else ''
            if field == 'decision':
                lines[index] = '- Decision: reject' + ending
                anchor = index if anchor is None else anchor
            elif field == 'type':
                lines[index] = '- Type: reject' + ending
                anchor = index
            elif field == 'decision note':
                written = line[key.end():].strip()
                if not written or written.startswith('TODO'):
                    lines[index] = note + ending
                    noted = index
                else:
                    anchor = index  # the user's own text: keep it, put the note after it
        if noted is None and anchor is not None:
            newline = '\r\n' if '\r\n' in block else '\n'
            if not lines[anchor].endswith('\n'):
                lines[anchor] += newline
                lines.insert(anchor + 1, note)
            else:
                lines.insert(anchor + 1, note + newline)
        closed.append(name)
    if seen is not None:
        seen.clear()
        seen.update(pending_ideas)
    return ''.join(lines), closed[::-1], undated[::-1]


def _read_ideas_seen(path: Path) -> dict[str, str]:
    """The ideas' first-seen days; a missing or broken record restarts every idea's clock today."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return {}
    return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}


def _write_ideas_seen(path: Path | None, before: dict[str, str], after: dict[str, str] | None) -> None:
    if path is not None and after is not None and after != before:
        stage.atomic_write(path, json.dumps(after, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def _vault_evolution(evolution: Path, cfg: dict) -> bool:
    """Whether `evolution` is the vault's own Evolution file, the only one the first-seen record covers."""
    try:
        return evolution.resolve() == _paths(cfg)[1].resolve()
    except (config.ConfigError, OSError):
        return False


def cmd_candidates(args: argparse.Namespace, cfg: dict) -> int:
    evolution = _evolution_path(args, cfg)
    if not evolution.is_file():
        raise FileNotFoundError(evolution)
    groups = _semantic_groups(_read_ledger(_ledger_path(args, cfg)))
    text = evolution.read_bytes().decode('utf-8')
    existing = set(SKILL_MARKER.findall(text))
    # The older generic candidate format had no marker; keep its idempotence while
    # allowing a rule candidate and a skill candidate to coexist.
    existing |= {fp for fp, block in _candidate_blocks(text)
                 if block.startswith('### Candidate ') and not SKILL_MARKER.search(block)}
    additions = [_candidate(fp, group) for fp, group in sorted(groups.items())
                 if _day_count(group) >= args.threshold
                 and not ({row.get('fingerprint') for row in group} & existing)]
    updated, failures = _update_failures(text, _decisions(text, groups))
    # The record belongs to the vault's Evolution file: another file (`--evolution`) would rebuild it
    # from its own ideas and restart the vault ideas' clocks, so that file's ideas stay undated.
    seen_path = config.vault_path(cfg) / IDEAS_SEEN if _vault_evolution(evolution, cfg) else None
    recorded = _read_ideas_seen(seen_path) if seen_path is not None else {}
    seen = dict(recorded) if seen_path is not None else None
    updated, closed, undated = _close_stale(updated, dt.date.today(), seen)
    if undated:
        print(f'{len(undated)} pending proposal(s) have no readable date and stay open: '
              + ', '.join(undated))
    if closed:
        action = ('would be closed as reject (dry run, nothing closed)' if args.dry_run
                  else 'closed as reject')
        print(f'{len(closed)} proposal(s) with no decision within {STALE_DAYS} days of their last '
              f'activity {action}: ' + ', '.join(closed))
    if not additions and not failures and not closed:
        print('no new evolution candidates or correction results')
        if not args.dry_run:
            _write_ideas_seen(seen_path, recorded, seen)
        return 0
    for item in additions:
        print(item)
    for fp in failures:
        print(f'correction result changed: {fp}')
    if args.dry_run:
        print('dry run: no file written')
        return 0
    if additions:
        newline = '\r\n' if '\r\n' in text else '\n'
        updated += ('' if updated.endswith('\n') else newline) + newline
        updated += newline.join(item.replace('\n', newline) for item in additions)
    stage.atomic_write(evolution, updated)
    # After Evolution.md: if that write fails, the closed ideas stay recorded and close next night.
    _write_ideas_seen(seen_path, recorded, seen)
    print(f'updated: {evolution}')
    return 0


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    save = sub.add_parser('record')
    for name in ('category', 'symptom', 'evidence', 'workaround', 'proposal'):
        save.add_argument(f'--{name}', required=True)
    save.add_argument('--task', default='')
    save.add_argument('--commit', default='')
    save.add_argument('--same')
    save.add_argument('--ledger')
    for name in ('report', 'candidates'):
        command = sub.add_parser(name)
        command.add_argument('--threshold', type=int, default=DEFAULT_THRESHOLD)
        command.add_argument('--ledger')
        command.add_argument('--evolution')
        if name == 'candidates':
            command.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        if args.command == 'record':
            return cmd_record(args, cfg)
        if args.command == 'report':
            return cmd_report(args, cfg)
        return cmd_candidates(args, cfg)
    except (config.ConfigError, OSError, ValueError, RuntimeError) as exc:
        print(f'gardener: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
