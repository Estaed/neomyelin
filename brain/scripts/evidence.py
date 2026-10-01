"""Check quoted evidence against marked lines in receipts."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import unicodedata

MIN_QUOTE = 10
MARKER = re.compile(r'^\s*(?:[-*]\s+)?(?:OBSERVATION|REACTION|CORRECTION):\s*', re.I)
QUOTED = re.compile(r'["“]([^"“”]+)["”]')


def normalize(value: str) -> str:
    value = unicodedata.normalize('NFC', value).translate(str.maketrans({
        '“': '"', '”': '"', '„': '"', '«': '"', '»': '"',
        '’': "'", '‘': "'", '–': '-', '—': '-', '−': '-',
    }))
    return re.sub(r'\s+', ' ', value).strip()


def valid_quote(value: str) -> bool:
    return len(normalize(value)) >= MIN_QUOTE


def exact_quote(quote: str, line: str) -> bool:
    wanted = normalize(quote).rstrip(' .,;:!?…')
    return valid_quote(wanted) and any(
        normalize(found.group(1)).rstrip(' .,;:!?…') == wanted
        for found in QUOTED.finditer(normalize(line)))


def receipt_day(text: str, metadata: dict) -> dt.date | None:
    """Use the date in the record, never the receipt filename."""
    explicit = re.search(r'(?m)^Observation day:\s*(\d{4}-\d{2}-\d{2})\s*$', text)
    if explicit:
        try:
            return dt.date.fromisoformat(explicit.group(1))
        except ValueError:
            return None
    created = metadata.get('created_at')
    if isinstance(created, str):
        try:
            return dt.datetime.fromisoformat(created.replace('Z', '+00:00')).astimezone().date()
        except ValueError:
            return None
    return None


def read_receipt(path: Path) -> tuple[dict, list[str]] | None:
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
        if len(lines) < 3 or lines[0] != '---':
            return None
        close = lines.index('---', 1)
        metadata = json.loads('\n'.join(lines[1:close]))
        if not isinstance(metadata, dict):
            return None
        return metadata, lines
    except (OSError, UnicodeError, ValueError):
        return None


def source_lines(vault: Path) -> list[dict]:
    """Return marked lines with a whole quoted phrase and a recorded day."""
    output = []
    for path in sorted((vault / 'receipts').glob('*.md')):
        parsed = read_receipt(path)
        if parsed is None:
            continue
        metadata, lines = parsed
        day = receipt_day('\n'.join(lines), metadata)
        if day is None:
            continue
        for number, line in enumerate(lines, 1):
            if not MARKER.match(line):
                continue
            for found in QUOTED.finditer(line):
                quote = normalize(found.group(1))
                if len(quote) >= MIN_QUOTE:
                    output.append({'day': day.isoformat(), 'ref': f'receipts/{path.name}:{number}',
                                   'quote': quote, 'line': line.strip()})
    return output


def daily_lines(vault: Path, *, through: dt.date | None = None,
                after: dt.date | None = None) -> list[dict]:
    """Read marked, quoted lines in daily notes."""
    output = []
    for path in sorted((vault / 'daily').glob('????-??-??.md')):
        try:
            day = dt.date.fromisoformat(path.stem)
            if (through and day > through) or (after and day <= after):
                continue
            lines = path.read_text(encoding='utf-8').splitlines()
        except (ValueError, OSError, UnicodeError):
            continue
        for number, line in enumerate(lines, 1):
            if not MARKER.match(line):
                continue
            for found in QUOTED.finditer(line):
                quote = normalize(found.group(1))
                if valid_quote(quote):
                    output.append({'day': day.isoformat(),
                                   'ref': f'{path.relative_to(vault).as_posix()}:{number}',
                                   'quote': quote, 'line': line.strip()})
    return output


def all_sources(vault: Path, *, through: dt.date | None = None,
                after: dt.date | None = None) -> list[dict]:
    sources = source_lines(vault) + daily_lines(vault, through=through, after=after)
    return [item for item in sources if (through is None or item['day'] <= through.isoformat())
            and (after is None or item['day'] > after.isoformat())]


def verified(candidate: object, sources: dict[str, list[dict]]) -> dict | None:
    """A candidate must cite the exact whole quote on its marked source line."""
    if not isinstance(candidate, dict):
        return None
    ref, quote = candidate.get('ref'), candidate.get('quote')
    if not isinstance(ref, str) or not isinstance(quote, str):
        return None
    for source in sources.get(ref, []):
        if normalize(quote) == source['quote'] and candidate.get('day') in (None, source['day']):
            return {key: source[key] for key in ('day', 'ref', 'quote')}
    return None
