#!/usr/bin/env python3
"""Recall over the vault's notes: "where did we talk about this before?"

Two engines behind one interface:

    bm25    pure Python, always there
    ollama  bge-m3 embeddings through Ollama's local HTTP API (urllib, no package); used when
            Ollama answers and has the model, never required

`NEOMYELIN_RECALL=bm25|ollama` forces one. Unset, ollama is used when it is ready, else bm25;
if ollama fails mid-query the answer comes from bm25 with a note on stderr.

WHY BOTH (measured on a 120-note vault, 20 queries, blind run):

    BM25 (index + grep)                   recall@5 0.90   hit@1 0.70
    LLM fact extraction (mem0)            recall@5 0.25   hit@1 0.15   43 min
    plain chunks + nomic-embed-text       recall@5 0.85   hit@1 0.70   18 s
    plain chunks + bge-m3                 recall@5 1.00   hit@1 0.85   26 s   <- ollama engine
    mem0 infer=False + bge-m3             recall@5 1.00   hit@1 0.85   89 s

Same embedder with and without fact extraction gave 0.25 vs 0.85, so the loss was the
extraction step: rewriting a note into ~5 short facts throws away the original words,
identifiers and context that make it findable, and a vault already has a distilled knowledge
layer, so extraction summarised a summary. With it off, mem0 matched plain chunking three
times slower. Hence no LLM on the write path, only an embedder (1.2 GB of VRAM, released when
Ollama idles), and BM25 for everyone else: Ollama-only would make every user install 1.2 GB
and leave recall dead without it.

The index is derived data. Delete `.brain/scripts/.state/recall-*.json` and the next query
rebuilds it; if deleting it could lose a line, the scope has drifted.

Usage:
    python .brain/scripts/recall.py "query text"          # search (refreshes a stale index)
    python .brain/scripts/recall.py "query" --k 8 --json
    python .brain/scripts/recall.py "query" --min-score 0.3
    python .brain/scripts/recall.py "query" --stale       # search without refreshing
    python .brain/scripts/recall.py --update              # incremental index refresh
    python .brain/scripts/recall.py --rebuild             # rebuild from scratch
    python .brain/scripts/recall.py --warm                # session start: refresh, load the model
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402

STATE_DIR = SCRIPT_DIR / '.state'
ENGINES = ('bm25', 'ollama')
ENGINE_ENV = 'NEOMYELIN_RECALL'

EMBEDDER = os.environ.get('NEOMYELIN_RECALL_EMBEDDER', 'bge-m3')
# Not `localhost`: on Windows it resolves to IPv6 first and falls back to IPv4, and the first
# embedding of a process took 2.1-4.4 s instead of 0.08 s (measured, model warm).
OLLAMA_URL = os.environ.get('NEOMYELIN_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
# A cold model load measured 5.1 s; Ollama's own default unloads after 5 minutes. Keeping it
# for a working session costs 1.2 GB of VRAM.
KEEP_ALIVE = os.environ.get('NEOMYELIN_RECALL_KEEP_ALIVE', '30m')
EMBED_BATCH = 16

# Measured with bge-m3: a nonsense query's best cosine was 0.4565, real hits 0.49-0.63. Its
# cosine floor is high, so 0.43-0.46 means "unrelated"; the threshold sits between the two so
# an unknown query abstains. BM25 scores are unbounded, so any match counts there.
MIN_SCORE = {'ollama': 0.48, 'bm25': 0.0}

# Chunk size fits the embedder's context (whole notes overflowed: `input length exceeds the
# context length`). BM25 uses the same chunks, so both engines rank the same passages.
CHUNK = 700
OVERLAP = 150
# A safety net, not a budget. A first cap of 12 silently left ~87% of long logs unsearchable;
# hitting this one is reported at the end of the build, never passed over in silence.
MAX_CHUNKS = 600

BM25_K1 = 1.2
BM25_B = 0.75
# Words are also indexed by their first five letters, so "sleeping" finds "sleep" and an
# agglutinative suffix (Turkish "kayıtları") still finds the stem ("kayıt"). An exact match
# scores on both forms, a stem-only match on one.
PREFIX = 5

# Scope: what the original indexes, on any vault. That is the numbered folders, the companion
# folder, knowledge notes, receipts and open tasks; an existing vault's other note folders count
# too, so its notes are found from the first query. Never indexed:
#   - private folders (`700-Private`, any folder named private; config.PRIVATE): official
#     papers stay out of every session.
#   - archives (`900-Archive`, any folder named archive; config.ARCHIVE): old logs and versions
#     outranked the live notes in the original's measurements. The original's one exception,
#     video transcript titles under the archive, is dropped: no archive is ever indexed here.
#   - `daily/`: brain.py fills it with generated views of the receipts indexed from `receipts/`.
#   - knowledge/index.md and knowledge/log.md (tables of the notes themselves; the original
#     indexed knowledge/concepts/ only).
#   - harness instructions at the root, dot folders (.brain, .claude, .git, .obsidian), closed
#     tasks and files marked `"generated": true`.
INSTRUCTION_FILES = {'agents.md', 'claude.md', 'gemini.md'}
SKIP_DIRS = {'node_modules', '__pycache__'}
ROOT_SKIP_DIRS = {'daily'}
SKIP_KEYS = {'knowledge/index.md', 'knowledge/log.md'}
ARCHIVE = config.ARCHIVE
PRIVATE = config.PRIVATE
CLOSED_TASK = {'done', 'cancelled'}
# A note about a retired mechanism says so in its first 20 lines; its preview says so too.
HISTORICAL = {'status: historical'}
HISTORY_LINE = ('> History',)


class RecallError(RuntimeError):
    """Recall could not answer; the message says why in one line."""


def _vault() -> Path:
    return config.vault_path(config.load())


def _index_path(engine: str) -> Path:
    return STATE_DIR / f'recall-{engine}.json'


def _engine_id(engine: str) -> str:
    # Vectors from another embedder live in another space: mixing them fails without an error.
    return f'ollama:{EMBEDDER}' if engine == 'ollama' else 'bm25:1'


# ---------------------------------------------------------------------------------------------
# Documents

def _meta(raw: str) -> dict | None:
    """JSON metadata: one object between `---` lines (tasks, receipts, generated views)."""
    if not raw.startswith('---'):
        return None
    end = raw.find('\n---', 3)
    if end == -1:
        return None
    try:
        meta = json.loads(raw[3:end])
    except ValueError:
        return None
    return meta if isinstance(meta, dict) else None


def _task_closed(path: Path) -> bool:
    try:
        meta = _meta(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError):
        return False  # an unreadable task counts as open; the build reports it unreadable
    return bool(meta) and str(meta.get('status', '')).casefold() in CLOSED_TASK


def _skip_dir(name: str, at_root: bool) -> bool:
    return (name.startswith('.') or name in SKIP_DIRS or bool(ARCHIVE.search(name))
            or bool(PRIVATE.search(name)) or (at_root and name in ROOT_SKIP_DIRS))


def documents(vault: Path) -> dict[str, Path]:
    """Every note recall covers, by vault-relative key (scope above). Open tasks only: a closed
    task's result lives in its receipt, and indexed it would come back as open work."""
    docs: dict[str, Path] = {}
    for root, dirs, files in os.walk(vault):
        at_root = Path(root) == vault
        dirs[:] = sorted(name for name in dirs if not _skip_dir(name, at_root))
        for name in sorted(files):
            if not name.casefold().endswith('.md'):
                continue
            if at_root and name.casefold() in INSTRUCTION_FILES:
                continue
            path = Path(root) / name
            key = path.relative_to(vault).as_posix()
            if key in SKIP_KEYS:
                continue
            if key.startswith('tasks/') and _task_closed(path):
                continue
            docs[key] = path
    return docs


def _strip_frontmatter(text: str) -> str:
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            return text[end + 4:].strip()
    return text.strip()


def _body(key: str, raw: str) -> str:
    """Searchable body. A task's title and next step sit in its metadata; without them the
    task could not be found by its own name, so they lead the body."""
    body = _strip_frontmatter(raw)
    meta = _meta(raw) if key.startswith('tasks/') else None
    if meta:
        head = ' — '.join(str(meta[k]) for k in ('title', 'next_action') if meta.get(k))
        body = f'{head}\n\n{body}'.strip()
    return body


def _aliases(raw: str) -> list[str]:
    """Frontmatter `aliases: ["a", "b"]`, a one-line JSON-style list only."""
    for line in raw.splitlines()[:20]:
        if line.startswith('aliases:'):
            value = line.split(':', 1)[1].strip()
            if value.startswith('[') and value.endswith(']'):
                try:
                    items = json.loads(value)
                except ValueError:
                    return []
                return [str(a).strip() for a in items if str(a).strip()][:8]
            return []
    return []


def _label(key: str, raw: str) -> str:
    """Document identity prefixed to every chunk: `title:`, else the first `# ` heading, else
    the file name; plus the folder, since one topic can sit in several folders and which one
    is canonical matters. Aliases ride along: frontmatter is not embedded, so without them a
    note was never found by its short or older name."""
    title = str((_meta(raw) or {}).get('title') or '') if key.startswith('tasks/') else ''
    for line in ([] if title else raw.splitlines()[:20]):
        if line.startswith('title:'):
            title = line.split(':', 1)[1].strip().strip('"').strip("'")
            break
        if line.startswith('# '):
            title = line[2:].strip()
            break
    if not title:
        title = Path(key).stem.replace('-', ' ')
    folder = Path(key).parent.as_posix()
    label = title if folder == '.' else f'{folder} — {title}'
    aliases = _aliases(raw)
    return f"{label} ({', '.join(aliases)})" if aliases else label


def _split(text: str) -> tuple[list[str], bool]:
    """Chunks and whether they were CUT. The flag is returned so the caller cannot lose it."""
    out, index = [], 0
    step = CHUNK - OVERLAP
    while index < len(text):
        out.append(text[index:index + CHUNK])
        index += step
    if len(out) > MAX_CHUNKS:
        return out[:MAX_CHUNKS], True
    return (out or [text]), False


def _historical(raw: str) -> bool:
    """Frontmatter `status: historical` in the first 20 lines: the note describes something
    retired, and a hit on it must not read as how things work today."""
    return any(line.strip() in HISTORICAL for line in raw.splitlines()[:20])


def _summary(vault: Path, key: str) -> str | None:
    """First paragraph of the note, as the preview a reader needs to judge a hit. A leading
    `> History` line (the note recalling a retired mechanism) is not the summary."""
    try:
        raw = (vault / key).read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        return None
    lines = _body(key, raw).splitlines()
    start = 0
    while start < len(lines) and (not lines[start].strip()
                                  or lines[start].lstrip().startswith(('#', *HISTORY_LINE))):
        start += 1
    paragraph = []
    for line in lines[start:]:
        if not line.strip() or line.lstrip().startswith('#'):
            break
        paragraph.append(line.strip())
    summary = re.sub(r'\s+', ' ', ' '.join(paragraph)).strip()[:400]
    if summary and _historical(raw):
        return f'(historical) {summary}'
    return summary or None


# ---------------------------------------------------------------------------------------------
# Engines

def terms(text: str) -> list[str]:
    """Folded words plus five-letter stems (`sleep*`). Folding drops accents and maps the
    dotless ı to i, so "kayit" finds "kayıt" and "cafe" finds "café"."""
    folded = unicodedata.normalize('NFKD', text.casefold().replace('ı', 'i'))
    folded = ''.join(char for char in folded if not unicodedata.combining(char))
    out = []
    for word in re.findall(r'\w+', folded):
        if len(word) < 2:
            continue
        out.append(word)
        if len(word) >= PREFIX:
            out.append(word[:PREFIX] + '*')
    return out


def _http(path: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(OLLAMA_URL + path, data=data,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RecallError(f'Ollama at {OLLAMA_URL} did not answer {path}: {exc}') from None


def ollama_ready(timeout: float = 0.5) -> bool:
    """Ollama answers and has the embedder pulled."""
    try:
        models = _http('/api/tags', timeout=timeout).get('models') or []
    except RecallError:
        return False
    names = {str(model.get('name') or model.get('model') or '') for model in models if isinstance(model, dict)}
    wanted = {EMBEDDER} if ':' in EMBEDDER else {EMBEDDER, f'{EMBEDDER}:latest'}
    return bool(names & wanted)


def _embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        answer = _http('/api/embed', {'model': EMBEDDER, 'input': batch, 'keep_alive': KEEP_ALIVE})
        got = answer.get('embeddings')
        if not isinstance(got, list) or len(got) != len(batch):
            raise RecallError(f'Ollama returned no embeddings for {EMBEDDER}: {str(answer)[:200]}')
        vectors.extend(got)
    return vectors


def _chunks(engine: str, label: str, pieces: list[str]) -> list[dict]:
    texts = [f'{label}\n\n{piece}' for piece in pieces]
    if engine == 'ollama':
        return [{'text': piece[:200], 'v': [round(x, 6) for x in vector]}
                for piece, vector in zip(pieces, _embed(texts))]
    out = []
    for piece, text in zip(pieces, texts):
        words = terms(text)
        out.append({'text': piece[:200], 'tf': dict(Counter(words)), 'len': len(words)})
    return out


def _bm25(query: str, stored: dict) -> list[tuple[float, str, str]]:
    """(best chunk score, key, chunk preview), one row per document."""
    wanted = set(terms(query))
    chunks = [(key, chunk) for key, record in stored.items() for chunk in record.get('chunks') or []]
    if not wanted or not chunks:
        return []
    count = len(chunks)
    average = sum(chunk['len'] for _, chunk in chunks) / count or 1.0
    frequency = Counter(term for _, chunk in chunks for term in wanted if term in chunk['tf'])
    idf = {term: math.log(1 + (count - n + 0.5) / (n + 0.5)) for term, n in frequency.items()}
    best: dict[str, tuple[float, str]] = {}
    for key, chunk in chunks:
        tf = chunk['tf']
        norm = BM25_K1 * (1 - BM25_B + BM25_B * chunk['len'] / average)
        score = sum(weight * tf[term] * (BM25_K1 + 1) / (tf[term] + norm)
                    for term, weight in idf.items() if term in tf)
        if score > 0 and score > best.get(key, (0.0, ''))[0]:
            best[key] = (score, chunk['text'])
    return [(score, key, text) for key, (score, text) in best.items()]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _semantic(query: str, stored: dict) -> list[tuple[float, str, str]]:
    vector = _embed([query])[0]
    scored = []
    for key, record in stored.items():
        pieces = record.get('chunks') or []
        if pieces:
            score, text = max(((_cosine(vector, p['v']), p['text']) for p in pieces), key=lambda row: row[0])
            scored.append((score, key, text))
    return scored


# ---------------------------------------------------------------------------------------------
# Index

def _load_index(engine: str) -> dict:
    try:
        index = json.loads(_index_path(engine).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        index = {}
    if not isinstance(index, dict) or index.get('engine') != _engine_id(engine) \
            or not isinstance(index.get('docs'), dict):
        return {'engine': _engine_id(engine), 'docs': {}}
    return index


def _save_index(engine: str, index: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _index_path(engine)
    # One temporary file per process: a hook and a manual query refreshing at once once
    # collided on a shared .tmp and one died with WinError 2.
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(index, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def staleness(stored: dict, docs: dict[str, Path]) -> tuple[int, int]:
    """(changed or new, removed) documents. Hashes only, no embedding, so it runs on every
    query: a stale index raises no error, it just answers from the past, and a wrong answer
    that looks trustworthy is worse than an error."""
    changed = 0
    for key, path in docs.items():
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if stored.get(key, {}).get('hash') != digest:
            changed += 1
    removed = sum(1 for key in stored if key not in docs)
    return changed, removed


def build(engine: str, full: bool = False, log=lambda line: None) -> dict:
    """Incremental build keyed by content hash, not mtime: a checkout, a sync or an editor save
    moves mtime without a change and would re-embed the whole vault for nothing. `log` gets a
    progress line every 20 notes (a first embedding run can take minutes)."""
    vault = _vault()
    index = {'engine': _engine_id(engine), 'docs': {}} if full else _load_index(engine)
    stored = index['docs']
    docs = documents(vault)
    for key in list(stored):
        if key not in docs:
            del stored[key]
    started = time.monotonic()
    changed = written = 0
    truncated: list[str] = []
    unreadable: list[str] = []
    try:
        for order, (key, path) in enumerate(docs.items(), start=1):
            try:
                data = path.read_bytes()
            except OSError:
                unreadable.append(key)
                continue
            # Hash the RAW bytes, as staleness() does. Hashing decoded text here would never match
            # for a CRLF or BOM file: it would count as changed and be re-indexed on every run.
            digest = hashlib.sha256(data).hexdigest()
            if stored.get(key, {}).get('hash') == digest:
                continue
            try:
                raw = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                # No silent skip: the hash is kept (so the file stops counting as stale) and the
                # run says its content is not searchable.
                stored[key] = {'hash': digest, 'label': key, 'chunks': []}
                unreadable.append(key)
                continue
            body = _body(key, raw)
            if not body or (_meta(raw) or {}).get('generated') is True:
                stored[key] = {'hash': digest, 'label': key, 'chunks': []}
                continue
            pieces, clipped = _split(body)
            if clipped:
                truncated.append(key)
            label = _label(key, raw)
            # Every chunk carries the document identity ("contextual chunking"): a chunk from
            # the middle of a long note does not say what it belongs to, and without the prefix
            # it matched queries on surface words. The preview stays the raw text.
            stored[key] = {'hash': digest, 'label': label, 'chunks': _chunks(engine, label, pieces)}
            changed += 1
            written += len(pieces)
            if changed % 20 == 0:
                log(f'  [{order}/{len(docs)}] {changed} notes, {written} chunks')
    finally:
        _save_index(engine, index)
    return {'engine': engine, 'docs': len(stored), 'changed': changed, 'chunks': written,
            'seconds': round(time.monotonic() - started, 1), 'truncated': truncated,
            'unreadable': unreadable}


def build_report(summary: dict) -> list[str]:
    lines = [f"index ({summary['engine']}): {summary['docs']} notes | {summary['changed']} changed, "
             f"{summary['chunks']} chunks this run | {summary['seconds']} s"]
    if summary['truncated']:
        lines.append(f"WARNING: {len(summary['truncated'])} notes CUT at {MAX_CHUNKS} chunks, their end is "
                     f"not searchable: {', '.join(summary['truncated'][:5])}")
    if summary['unreadable']:
        lines.append(f"WARNING: {len(summary['unreadable'])} files are not UTF-8, their content is NOT "
                     f"searchable: {', '.join(summary['unreadable'][:5])}")
    return lines


# ---------------------------------------------------------------------------------------------
# Search

def forced_engine() -> str | None:
    value = os.environ.get(ENGINE_ENV, '').strip().lower()
    if value in ('', 'auto'):
        return None
    if value not in ENGINES:
        raise RecallError(f'{ENGINE_ENV} must be bm25 or ollama, not {value!r}')
    return value


def choose_engine() -> tuple[str, str]:
    """(engine, why)."""
    forced = forced_engine()
    if forced:
        return forced, f'forced by {ENGINE_ENV}'
    if ollama_ready():
        return 'ollama', f'Ollama answers with {EMBEDDER}'
    return 'bm25', f'Ollama with {EMBEDDER} not reachable at {OLLAMA_URL}'


def _search_with(engine: str, query: str, k: int, min_score: float | None, refresh: bool,
                 log) -> list[dict]:
    vault = _vault()
    stored = _load_index(engine)['docs']
    changed, removed = staleness(stored, documents(vault))
    if changed or removed or not stored:
        if refresh:
            summary = build(engine, log=log)
            if summary['truncated'] or summary['unreadable']:
                for line in build_report(summary)[1:]:
                    log(line)
            stored = _load_index(engine)['docs']
        elif not stored:
            raise RecallError('index empty: run `python .brain/scripts/recall.py --update`')
        else:
            log(f'[stale index] {changed} notes changed or new, {removed} removed — these results are OLD. '
                'Refresh: python .brain/scripts/recall.py --update')
    scored = _semantic(query, stored) if engine == 'ollama' else _bm25(query, stored)
    floor = MIN_SCORE[engine] if min_score is None else min_score
    scored = [row for row in scored if row[0] > 0 and row[0] >= floor]
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [{'score': round(score, 4), 'path': key, 'preview': _summary(vault, key) or text}
            for score, key, text in scored[:k]]


def search(query: str, k: int = 5, min_score: float | None = None, refresh: bool = True,
           log=lambda line: None) -> tuple[str, list[dict]]:
    """(engine used, results). Raises RecallError; prints nothing itself (hooks call this)."""
    engine, why = choose_engine()
    log(f'[recall] engine: {engine} ({why})')
    try:
        return engine, _search_with(engine, query, k, min_score, refresh, log)
    except RecallError as exc:
        if engine != 'ollama' or forced_engine():
            raise
        log(f'[recall] ollama failed ({exc}); answering with bm25')
        return 'bm25', _search_with('bm25', query, k, min_score, refresh, log)


def warm() -> int:
    """Session start, in the background: bring the index up to date and, for ollama, load the
    model (a cold load once pushed the first prompt hook past its time limit). The original
    only loaded the model; a new vault has no embedding index yet, and building it inside the
    first query could take minutes."""
    try:
        engine, _ = choose_engine()
        build(engine)
        if engine == 'ollama':
            _embed(['warm'])
    except (RecallError, config.ConfigError, OSError):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('query', nargs='?', help='Text to search for.')
    parser.add_argument('--k', type=int, default=5, help='How many results (default 5).')
    parser.add_argument('--json', action='store_true', help='JSON output.')
    parser.add_argument('--min-score', type=float, default=None,
                        help=f'Score floor (default: ollama {MIN_SCORE["ollama"]}, bm25 any match).')
    parser.add_argument('--stale', action='store_true', help='Search without refreshing a stale index.')
    parser.add_argument('--update', action='store_true', help='Refresh the index incrementally.')
    parser.add_argument('--rebuild', action='store_true', help='Rebuild the index from scratch.')
    parser.add_argument('--warm', action='store_true', help='Refresh the index and load the model, quietly.')
    args = parser.parse_args(argv)

    def log(line: str) -> None:
        print(line, file=sys.stderr)

    if args.warm:
        return warm()
    try:
        if args.update or args.rebuild:
            engine, why = choose_engine()
            log(f'[recall] engine: {engine} ({why})')
            for line in build_report(build(engine, full=args.rebuild, log=log)):
                print(line)
            return 0
        if not args.query:
            parser.print_help()
            return 1
        engine, results = search(args.query, args.k, args.min_score, refresh=not args.stale, log=log)
    except (RecallError, config.ConfigError) as exc:
        log(str(exc))
        return 1

    if not results:
        if args.json:
            print('[]')
        floor = MIN_SCORE[engine] if args.min_score is None else args.min_score
        log(f'not found: no result above {floor} ({engine})')
        return 0
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for row in results:
        print(f"[{row['score']:.3f}] {row['path']}")
        print(f"        {row['preview'].replace(chr(10), ' ')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
