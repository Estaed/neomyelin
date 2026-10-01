"""Small shared helpers for nightly evidence jobs and the configured model seam."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

import config
import engine

try:
    import fcntl
except ImportError:
    fcntl = None
    if os.name == 'nt':
        import msvcrt


def paths(cfg: dict) -> tuple[Path, Path]:
    vault = config.vault_path(cfg)
    companion = config.companion_dir(cfg)
    if companion is None:
        raise config.ConfigError('companion folder is ambiguous')
    return vault / '.brain' / 'patterns.md', companion / 'Personality.md'


@contextlib.contextmanager
def stage(prefix: str):
    """Create a private, disposable working folder with normal inherited permissions."""
    root = Path(tempfile.gettempdir()) / f'{prefix}{uuid.uuid4().hex}'
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def manifest(root: Path) -> dict[str, str]:
    entries = {}
    if not root.exists():
        return entries
    for path in root.rglob('*'):
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            entries[name + '/'] = 'dir'
        elif path.is_file():
            entries[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


def manifest_violation(before: dict[str, str], after: dict[str, str],
                       allowed: frozenset[str] = frozenset({'candidate.json'})) -> str | None:
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) != after.get(name) and name not in allowed:
            return name
    return None


def run_in_stage(prompt: str, stage: Path, *, job: str, cfg: dict | None = None) -> str:
    """Ask through the configured engine and reject writes outside the job's output."""
    cfg = cfg if cfg is not None else config.load()
    vault = config.vault_path(cfg)
    before_stage = manifest(stage)
    staged_cfg = {**cfg, 'vault': str(stage)}
    try:
        answer = engine.ask(prompt, engine=cfg.get(f'{job}_engine', cfg.get('engine', 'auto')),
                            cfg=staged_cfg)
    except Exception as exc:
        write_health(vault / '.brain' / '.state', f'{job} model failed: {exc}', warning=True)
        raise
    violation = manifest_violation(before_stage, manifest(stage))
    if violation:
        detail = f'model changed {violation} outside the allowed output'
        write_health(vault / '.brain' / '.state', f'{job}: {detail}', warning=True)
        raise RuntimeError(detail)
    return answer


def ask_json(prompt: str, cfg: dict, *, job: str = 'evolution',
             source_files: list[tuple[Path, str]] | None = None) -> list:
    """Read a JSON list from a staged model call; copy only the needed sources."""
    with stage(f'{job}-stage-') as work:
        atomic_write(work / 'instruction_prompt.txt', prompt)
        for source, relative in source_files or []:
            target = work / relative
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        answer = run_in_stage(prompt, work, job=job, cfg=cfg).strip()
        if not answer and (work / 'candidate.json').is_file():
            answer = (work / 'candidate.json').read_text(encoding='utf-8').strip()
    if answer.startswith('```'):
        answer = answer.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    result = json.loads(answer)
    if not isinstance(result, list):
        raise ValueError('model reply must be a JSON list')
    return result


def fresh_warnings(value: object, today: dt.date | None = None) -> list[str]:
    today = today or dt.date.today()
    output = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, str):
            continue
        try:
            date = dt.date.fromisoformat(item[:10])
        except ValueError:
            continue
        if dt.timedelta(0) <= today - date <= dt.timedelta(days=3):
            output.append(item)
    return output


def write_health(state_dir: Path, error: str, warning: bool = False) -> None:
    target = state_dir / 'health.json'
    try:
        data = json.loads(target.read_text(encoding='utf-8')) if target.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if warning:
        old = [item for item in fresh_warnings(data.get('warnings')) if item[11:] != error]
        data['warnings'] = (old + [f'{dt.date.today().isoformat()} {error}'])[-20:]
    else:
        data.update(ts=int(time.time()), component='stage', error=error)
    atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


@contextlib.contextmanager
def exclusive_lock(path: Path):
    """Yield False when another process holds the one-byte lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        acquired = False
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
        except (BlockingIOError, OSError):
            pass
        try:
            yield acquired
        finally:
            if acquired:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
