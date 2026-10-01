#!/usr/bin/env python3
"""Evolve quoted observations into binding, sleeping, or vetoed personality patterns.

Only marked observation, reaction, and correction lines supply evidence. Three distinct
days and exact whole quotes make a new pattern binding. Existing patterns keep collecting
evidence while sleeping and may wake when they outrank the context budget. Vetoes persist
with their evidence fingerprint. Stale status, attitude area, positive direction, and
counter evidence remain in the evidence ledger. Only binding patterns reach Personality.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import config
import evidence
import stage

THRESHOLD = 3
DEFAULT_BUDGET = 1800
BLOCK = re.compile(r'(?ms)^## Pattern: ([^\r\n]+)\r?\n(.*?)(?=^## Pattern: |\Z)')
MARK = re.compile(r'<!-- neomyelin:([0-9a-f]{12}) -->')
EMPTY = '# Evidence patterns\n\nOnly verified receipt quotes are stored here.\n'
EVIDENCE = re.compile(r'^- (\d{4}-\d{2}-\d{2}) `([^`]+)` "([^"]+)"$')
SCAN = re.compile(r'(?m)^Last scan: (\d{4}-\d{2}-\d{2})$')


def key(text: str) -> str:
    return evidence.normalize(text).casefold()


def identifier(title: str) -> str:
    return hashlib.sha256(key(title).encode('utf-8')).hexdigest()[:12]


def fingerprint(title: str, proofs: list[dict]) -> str:
    refs = sorted(item['ref'] for item in proofs)
    return hashlib.sha256((key(title) + '|' + '|'.join(refs)).encode('utf-8')).hexdigest()[:12]


def _field(body: str, name: str) -> str:
    found = re.search(rf'(?m)^{re.escape(name)}: (.*)$', body)
    return found.group(1).strip() if found else ''


def entries(text: str) -> list[dict]:
    output = []
    for found in BLOCK.finditer(text):
        body = found.group(2)
        proofs = []
        in_evidence = False
        for line in body.splitlines():
            if line == 'Evidence:':
                in_evidence = True
                continue
            if line == 'Counter evidence:':
                in_evidence = False
                continue
            if not in_evidence:
                continue
            match = EVIDENCE.fullmatch(line)
            if match:
                proofs.append({'day': match.group(1), 'ref': match.group(2),
                               'quote': match.group(3)})
        output.append({'title': found.group(1).strip(), 'status': _field(body, 'Status'),
                       'behavior': _field(body, 'Behavior'), 'area': _field(body, 'Area'),
                       'direction': _field(body, 'Direction'), 'id': _field(body, 'ID'),
                       'fingerprint': _field(body, 'Fingerprint'), 'evidence': proofs,
                       'block': found.group(0), 'start': found.start(), 'end': found.end()})
    return output


def last_scan(text: str) -> dt.date | None:
    found = SCAN.search(text)
    if not found:
        return None
    try:
        return dt.date.fromisoformat(found.group(1))
    except ValueError:
        return None


def advance_scan(text: str, through: dt.date) -> str:
    prior = last_scan(text)
    if prior and prior >= through:
        return text
    line = f'Last scan: {through.isoformat()}'
    if SCAN.search(text):
        return SCAN.sub(line, text, count=1)
    first = text.find('\n')
    return text[:first + 1] + line + '\n' + text[first + 1:] if first >= 0 else text + '\n' + line + '\n'


def binding_rules(cfg: dict, existing: list[dict]) -> list[str]:
    """Give the model the existing rules so narrower duplicates are not proposed."""
    vault = config.vault_path(cfg)
    companion = config.companion_dir(cfg)
    paths = [vault / 'AGENTS.md']
    if companion is not None:
        paths.insert(0, companion / 'Rules.md')
    output = []
    for path in paths:
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            found = re.match(r'^\s*(?:[-*]|\d+\.)\s+\*\*(.+?)\*\*', line)
            if found:
                output.append(found.group(1).strip())
    output.extend(item['title'] for item in existing if item['status'] == 'active')
    return list(dict.fromkeys(output))


def prompt(sources: list[dict], existing: list[dict], cfg: dict) -> str:
    data = [{'day': item['day'], 'ref': item['ref'], 'line': item['line']} for item in sources]
    known = [{'title': item['title'], 'behavior': item['behavior'], 'status': item['status'],
              'area': item['area'], 'direction': item['direction']} for item in existing]
    binding = binding_rules(cfg, existing)
    binding_text = '\n'.join(f'- {item}' for item in binding)
    if len(binding_text) > 6000:
        binding_text = binding_text[:5990].rsplit('\n', 1)[0] + '\n- ...'
    return (f"Write in {cfg['language']}. Use only the marked OBSERVATION, REACTION, or CORRECTION "
            'lines below. A new pattern needs exact whole quoted phrases from three different '
            'finished days. Do not infer feelings or motives. Keep attitude as an area when '
            'appropriate; set direction to positive for a beneficial preference. If an '
            'observation supports an existing active or sleeping pattern, return kind=evidence '
            'with that exact title and the new quotes instead of a duplicate pattern. Use '
            'kind=counter for evidence against an existing pattern. Never '
            'propose a vetoed pattern. Return a JSON array of objects with title, area, '
            'direction (positive or negative), behavior, kind (pattern, evidence, or counter), and '
            'evidence [{day,ref,quote}]. A quote must be the entire quoted phrase on its '
            'marked line. A restatement or narrower version of a binding rule is not a new '
            'pattern. [] is valid.\nBinding rules:\n'
            f'{binding_text}\nExisting patterns: '
            f'{json.dumps(known, ensure_ascii=False)}\nObservations: '
            f'{json.dumps(data, ensure_ascii=False)}')


def candidate(raw: object, sources: dict[str, list[dict]]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get('kind', 'pattern')
    if kind not in ('pattern', 'evidence', 'counter'):
        return None
    title, area, behavior = (raw.get(name) for name in ('title', 'area', 'behavior'))
    if not isinstance(title, str) or not title.strip() or '\n' in title or title.startswith('#'):
        return None
    if kind == 'pattern' and (not all(isinstance(value, str) and value.strip()
                                      and '\n' not in value and '\r' not in value
                                      for value in (area, behavior))
                              or any(value.startswith('#') for value in (area, behavior))):
        return None
    proofs = raw.get('evidence')
    if not isinstance(proofs, list) or not proofs:
        return None
    seen, valid = set(), []
    for proof in proofs:
        item = evidence.verified(proof, sources)
        if item is None:
            if kind == 'pattern':
                return None
            continue
        if item['ref'] not in seen:
            seen.add(item['ref'])
            valid.append(item)
    if not valid or (kind == 'pattern' and len({item['day'] for item in valid}) < THRESHOLD):
        return None
    return {'title': title.strip(), 'area': str(area or '').strip(),
            'behavior': str(behavior or '').strip(),
            'direction': str(raw.get('direction') or '').strip().casefold(), 'kind': kind,
            'evidence': sorted(valid, key=lambda item: (item['day'], item['ref']))}


def block(item: dict, *, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    lines = [f"## Pattern: {item['title']}", 'Status: active',
             f"ID: {identifier(item['title'])}",
             f"Fingerprint: {fingerprint(item['title'], item['evidence'])}",
             f"Binding date: {today.isoformat()}", f"Area: {item['area']}"]
    if item.get('direction') in ('positive', 'negative'):
        lines.append(f"Direction: {item['direction']}")
    lines.extend([f"Behavior: {item['behavior']}", 'Evidence:'])
    lines.extend(f"- {proof['day']} `{proof['ref']}` \"{proof['quote']}\""
                 for proof in item['evidence'])
    lines.append('Counter evidence:')
    return '\n'.join(lines) + '\n'


def _read(path: Path, fallback: str = '') -> str:
    try:
        return path.read_bytes().decode('utf-8')
    except FileNotFoundError:
        return fallback


def _replace_block(text: str, item: dict, changed: str) -> str:
    return text[:item['start']] + changed + text[item['end']:]


def add_evidence(text: str, item: dict, proofs: list[dict]) -> tuple[str, bool]:
    if item['status'] not in ('active', 'sleeping', 'candidate'):
        return text, False
    seen = {proof['ref'] for proof in item['evidence']}
    additions = [proof for proof in proofs if proof['ref'] not in seen]
    if not additions:
        return text, False
    raw = item['block']
    heading = re.search(r'(?m)^Evidence:[ \t]*\r?\n', raw)
    if not heading:
        return text, False
    position = heading.end()
    following = re.search(r'(?m)^[A-Z][A-Za-z ]+:', raw[position:])
    if following:
        position += following.start()
    else:
        position = len(raw.rstrip('\n'))
    lines = ''.join(f"- {proof['day']} `{proof['ref']}` \"{proof['quote']}\"\n"
                    for proof in additions)
    if position and raw[position - 1] != '\n':
        lines = '\n' + lines
    changed = raw[:position] + lines + raw[position:]
    updated_fingerprint = fingerprint(item['title'], item['evidence'] + additions)
    changed = re.sub(r'(?m)^Fingerprint: [0-9a-f]{12}$',
                     f'Fingerprint: {updated_fingerprint}', changed, count=1)
    if item['status'] == 'candidate' and len({p['day'] for p in item['evidence'] + additions}) >= THRESHOLD:
        changed = changed.replace('Status: candidate', 'Status: active', 1)
        changed = changed.replace('Area:', f"Binding date: {dt.date.today().isoformat()}\nArea:", 1)
    return _replace_block(text, item, changed), True


def add_counter_evidence(text: str, item: dict, proofs: list[dict]) -> tuple[str, bool]:
    if item['status'] not in ('active', 'sleeping', 'stale'):
        return text, False
    raw = item['block']
    present = {(line.group(1), line.group(2)) for value in raw.splitlines()
               if (line := EVIDENCE.fullmatch(value))}
    additions = [proof for proof in proofs if (proof['day'], proof['ref']) not in present]
    if not additions:
        return text, False
    if 'Counter evidence:' not in raw:
        raw = raw.rstrip('\n') + '\nCounter evidence:\n'
    lines = ''.join(f"- {proof['day']} `{proof['ref']}` \"{proof['quote']}\"\n"
                    for proof in additions)
    return _replace_block(text, item, raw.rstrip('\n') + '\n' + lines), True


def _line(item: dict) -> str:
    days = len({proof['day'] for proof in item['evidence']})
    direction = ', positive' if item['direction'] == 'positive' else ''
    return (f"- {item['title']}: {item['behavior']} ({item['area']}, {days} days{direction}; "
            f"evidence: .brain/patterns.md) <!-- neomyelin:{item['id'] or identifier(item['title'])} -->")


def rank(text: str, budget: int = DEFAULT_BUDGET) -> tuple[str, list[str], list[str]]:
    """Keep the strongest fitting patterns binding; sleeping evidence can wake them."""
    pool = [item for item in entries(text) if item['status'] in ('active', 'sleeping')]
    ordered = sorted(pool, key=lambda item: (-len(item['evidence']), item['status'] != 'active',
                                             item['start']))
    selected, used = set(), 0
    for index, item in enumerate(ordered):
        size = len((_line(item) + '\n').encode('utf-8'))
        if index == 0 or used + size <= budget:
            selected.add(item['start'])
            used += size
    sleeping = [item['title'] for item in pool if item['status'] == 'active'
                and item['start'] not in selected]
    waking = [item['title'] for item in pool if item['status'] == 'sleeping'
              and item['start'] in selected]
    for item in reversed(pool):
        desired = 'active' if item['start'] in selected else 'sleeping'
        if item['status'] != desired:
            changed = item['block'].replace(f"Status: {item['status']}", f'Status: {desired}', 1)
            text = _replace_block(text, item, changed)
    return text, sleeping, waking


def personality_text(current: str, evidence_text: str) -> str:
    """Regenerate marked lines, leaving every unmarked user line byte-identical."""
    original = current.splitlines(keepends=True)
    kept = [line for line in original if not MARK.search(line)]
    active = [_line(item) for item in entries(evidence_text) if item['status'] == 'active']
    newline = '\r\n' if '\r\n' in current else '\n'
    base = ''.join(kept)
    if active and base and not base.endswith(('\n', '\r')):
        base += newline
    return base + ''.join(line + newline for line in active)


def _write_pair(evidence_path: Path, personality_path: Path, before: str,
                current: str, personality: str) -> None:
    existed = evidence_path.exists()
    prior_personality = _read(personality_path)
    try:
        if current != before:
            stage.atomic_write(evidence_path, current)
        if personality != prior_personality:
            stage.atomic_write(personality_path, personality)
    except OSError:
        if current != before:
            if existed:
                stage.atomic_write(evidence_path, before)
            else:
                evidence_path.unlink(missing_ok=True)
        raise


def write_personality(cfg: dict | None = None, *, dry_run: bool = False) -> str:
    cfg = cfg if cfg is not None else config.load()
    evidence_path, personality_path = stage.paths(cfg)
    result = personality_text(_read(personality_path), _read(evidence_path, EMPTY))
    if dry_run:
        print(result, end='')
    elif result != _read(personality_path):
        stage.atomic_write(personality_path, result)
    return result


def _log(state: Path, message: str) -> None:
    target = state / 'patterns.log'
    stamp = dt.datetime.now().astimezone().isoformat(timespec='seconds')
    lines = _read(target).splitlines()[-199:]
    stage.atomic_write(target, '\n'.join(lines + [f'{stamp} {message}']) + '\n')


def run(cfg: dict | None = None, *, dry_run: bool = False,
        today: dt.date | None = None) -> int:
    cfg = cfg if cfg is not None else config.load()
    today = today or dt.date.today()
    finished = today - dt.timedelta(days=1)
    evidence_path, personality_path = stage.paths(cfg)
    vault = config.vault_path(cfg)
    before = _read(evidence_path, EMPTY)
    pending = evidence.all_sources(vault, through=finished, after=last_scan(before))
    if not pending:
        print('No unscanned observations.')
        return 0
    all_sources = evidence.all_sources(vault, through=finished)
    if len({source['day'] for source in all_sources}) < THRESHOLD:
        print(f'No pattern: observations cover fewer than {THRESHOLD} days.')
        return 0
    if dry_run:
        print(f'Dry run: {len(pending)} pending observations; no model call or write.')
        return 0
    state = vault / '.brain' / '.state'
    with stage.exclusive_lock(state / 'patterns.lock') as acquired:
        if not acquired:
            print('Pattern evolution is already running.')
            return 0
        try:
            before = _read(evidence_path, EMPTY)
            pending = evidence.all_sources(vault, through=finished, after=last_scan(before))
            if not pending:
                print('No unscanned observations.')
                return 0
            sources_by_ref: dict[str, list[dict]] = {}
            for source in all_sources:
                sources_by_ref.setdefault(source['ref'], []).append(source)
            source_files = []
            for ref in sorted(sources_by_ref):
                relative = ref.rsplit(':', 1)[0]
                path = vault / relative
                if path.is_file():
                    source_files.append((path, f'sources/{relative}'))
            proposals = stage.ask_json(prompt(all_sources, entries(before), cfg), cfg,
                                       job='evolution', source_files=source_files)
            # Verify again against the source files as they stand after the model call.
            # A receipt can be edited while the model is thinking.
            fresh_sources = evidence.all_sources(vault, through=finished)
            sources_by_ref = {}
            for source in fresh_sources:
                sources_by_ref.setdefault(source['ref'], []).append(source)
            current = _read(evidence_path, EMPTY)
            old = current
            known = entries(current)
            known_titles = {key(item['title']) for item in known}
            known_behaviors = {key(item['behavior']) for item in known}
            vetoed_fingerprints = {item['fingerprint'] for item in known
                                   if item['status'] == 'vetoed'}
            additions, rejected, updated = [], 0, 0
            for raw in proposals:
                item = candidate(raw, sources_by_ref)
                if item is None:
                    rejected += 1
                    stage.write_health(state, 'evolution evidence gate rejected a proposal', warning=True)
                    continue
                title = key(item['title'])
                same = next((row for row in entries(current) if key(row['title']) == title), None)
                if item['kind'] == 'evidence':
                    if same is not None:
                        current, changed = add_evidence(current, same, item['evidence'])
                        updated += int(changed)
                    continue
                if item['kind'] == 'counter':
                    if same is not None:
                        current, changed = add_counter_evidence(current, same, item['evidence'])
                        updated += int(changed)
                    continue
                if same or title in known_titles or key(item['behavior']) in known_behaviors:
                    continue
                if fingerprint(item['title'], item['evidence']) in vetoed_fingerprints:
                    continue
                if key(item['behavior']) in key(_read(personality_path)):
                    continue
                known_titles.add(title)
                known_behaviors.add(key(item['behavior']))
                additions.append(block(item, today=today))
            if additions:
                current = current.rstrip('\r\n') + '\n\n' + '\n\n'.join(additions)
            current, sleeping, waking = rank(current, int(cfg.get('pattern_budget', DEFAULT_BUDGET)))
            if not rejected:
                current = advance_scan(current, finished)
            personality = personality_text(_read(personality_path), current)
            _write_pair(evidence_path, personality_path, old, current, personality)
            message = (f'Patterns: {len(additions)} added, {updated} evidence updates, '
                       f'{rejected} rejected, {len(sleeping)} sleeping, {len(waking)} waking.')
            print(message)
            _log(state, message)
            return 0
        except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as exc:
            stage.write_health(state, f'evolution failed: {exc}', warning=True)
            print(f'Pattern evolution failed: {exc}', file=sys.stderr)
            _log(state, f'Pattern evolution failed: {exc}')
            if isinstance(exc, OSError):
                raise
            return 1


def veto(title: str, cfg: dict | None = None, *, reason: str = '',
         who: str = 'user', today: dt.date | None = None) -> int:
    cfg = cfg if cfg is not None else config.load()
    evidence_path, personality_path = stage.paths(cfg)
    state = config.vault_path(cfg) / '.brain' / '.state'
    with stage.exclusive_lock(state / 'patterns.lock') as acquired:
        if not acquired:
            print('Pattern evolution is already running.', file=sys.stderr)
            return 1
        current = _read(evidence_path)
        wanted = next((item for item in entries(current) if key(item['title']) == key(title)), None)
        if wanted is None:
            print(f'Pattern not found: {title}', file=sys.stderr)
            return 1
        if wanted['status'] == 'vetoed':
            print(f'Already vetoed: {title}')
            return 0
        clean_reason = ' '.join(reason.split()) or 'no reason supplied'
        record = f"- {(today or dt.date.today()).isoformat()} {who} veto: {clean_reason}"
        changed = wanted['block'].replace(f"Status: {wanted['status']}", 'Status: vetoed', 1)
        if 'Counter evidence:' not in changed:
            changed = changed.rstrip('\n') + '\nCounter evidence:\n'
        changed = changed.rstrip('\n') + '\n' + record + '\n'
        updated = _replace_block(current, wanted, changed)
        personality = personality_text(_read(personality_path), updated)
        _write_pair(evidence_path, personality_path, current, updated, personality)
        print(f'Vetoed: {title}')
        return 0


def status(cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else config.load()
    evidence_path, _ = stage.paths(cfg)
    text = _read(evidence_path, EMPTY)
    pending = evidence.all_sources(config.vault_path(cfg), through=dt.date.today() - dt.timedelta(days=1),
                                   after=last_scan(text))
    print('Status | Evidence | Area | Direction | Title')
    for item in entries(text):
        print(f"{item['status']} | {len(item['evidence'])} | {item['area']} | "
              f"{item['direction']} | {item['title']}")
    print(f'Pending observations: {len(pending)}; last scan: {last_scan(text) or "never"}')
    return 0


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action')
    run_parser = actions.add_parser('run')
    run_parser.add_argument('--dry-run', action='store_true')
    veto_parser = actions.add_parser('veto')
    veto_parser.add_argument('title')
    veto_parser.add_argument('--reason', default='')
    veto_parser.add_argument('--who', default='user')
    personality_parser = actions.add_parser('write-personality')
    personality_parser.add_argument('--dry-run', action='store_true')
    actions.add_parser('status')
    args = parser.parse_args(argv)
    try:
        if args.action == 'veto':
            return veto(args.title, reason=args.reason, who=args.who)
        if args.action == 'write-personality':
            write_personality(dry_run=args.dry_run)
            return 0
        if args.action == 'status':
            return status()
        return run(dry_run=getattr(args, 'dry_run', False))
    except (config.ConfigError, OSError, UnicodeError) as exc:
        print(f'Pattern evolution failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
