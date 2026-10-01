#!/usr/bin/env python3
"""NeoMyelin configuration: the one place paths and names come from.

install.py writes `<vault>/.brain/config.json`; every script reads it through this module.
Nothing else in the layer hard-codes a path, a user name, an assistant name or an email.

    {
      "vault": "<absolute vault path>",
      "user_name": "Alex",
      "assistant_name": "Nova",
      "language": "English",
      "harnesses": ["claude"]
    }

Optional `companion_dir` (vault-relative) names the companion folder. Without it the folder is
COMPANION, or the one folder whose name ends in "companion", or the one holding Core.md (so a
vault that already has a companion folder keeps it). Optional
`projects_root` (absolute) is the folder holding the user's project folders; the session start
in the vault names each one there without a card in PROJECTS. `NEOMYELIN_CONFIG` points at
another config file (tests).
"""
from __future__ import annotations

import json
import os
import datetime as dt
from pathlib import Path
import re
import sys

CONFIG_ENV = 'NEOMYELIN_CONFIG'
HARNESSES = ('claude', 'codex', 'agy')
REQUIRED_TEXT = ('user_name', 'assistant_name', 'language')
# The companion folder install.py creates in a vault that has none (Rules, Core, Personality, ...).
COMPANION = '850-Companion 🔮'
# An archive folder is never the live companion.
ARCHIVE = re.compile(r'(?i)archive')
# Sensitive papers: recall never indexes a folder with this word in its name.
PRIVATE = re.compile(r'(?i)\bprivate\b')
# The vault structure install.py creates, shipped as it is (Blueprint, Decisions 2026-10-01):
# 200-Goals ⚔️, 300-Education 🎓, 400-Work 💼, 500-Projects 🏰, 600-Life 🌿, 700-Private 🔐,
# 800-Arsenal 🛠️, 900-Archive 📦. Written with character names so the variation selector in two
# names cannot be lost in an edit. Each folder's note is templates/folders/<name before the space>.md.
_VS16 = '\N{VARIATION SELECTOR-16}'
FOLDERS = ('200-Goals \N{CROSSED SWORDS}' + _VS16, '300-Education \N{GRADUATION CAP}',
           '400-Work \N{BRIEFCASE}', '500-Projects \N{EUROPEAN CASTLE}', '600-Life \N{HERB}',
           '700-Private \N{CLOSED LOCK WITH KEY}', '800-Arsenal \N{HAMMER AND WRENCH}' + _VS16,
           '900-Archive \N{PACKAGE}')
# Project cards live here: a note whose frontmatter has `path:` (the project folder) and `label:`.
PROJECTS = FOLDERS[3]


class ConfigError(ValueError):
    """The config file is missing or does not describe a usable vault."""


def agy_snap(exe: str | None = None) -> str | None:
    """The snap name when `agy` is a snap (`/snap/bin/agy -> antigravity-cli`), else None."""
    import shutil  # noqa: PLC0415 - only the agy paths need it
    exe = exe if exe is not None else shutil.which('agy')
    if not exe or '/snap/bin/' not in exe.replace('\\', '/') or not os.path.islink(exe):
        return None
    return os.path.basename(os.readlink(exe)) or None


def agy_dir(home: Path, snap: str | None = None) -> Path:
    """agy's `.gemini` folder: its hooks (config/hooks.json) and global rules (GEMINI.md) live here.

    Measured in WSL, 2026-10-01: the Linux snap of agy runs with HOME moved to the snap's common
    folder, so it reads ~/snap/<snap>/common/.gemini and never ~/.gemini.
    """
    return home / 'snap' / snap / 'common' / '.gemini' if snap else home / '.gemini'


def force_utf8() -> None:
    """Make stdout/stderr UTF-8 whatever the console code page is (Windows defaults to cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV, '').strip()
    if override:
        return Path(override).expanduser()
    # Installed layout: <vault>/.brain/scripts/config.py -> <vault>/.brain/config.json
    return Path(__file__).resolve().parent.parent / 'config.json'


def validate(data: object) -> dict:
    """Return the config if it is usable, else raise ConfigError naming the bad field."""
    if not isinstance(data, dict):
        raise ConfigError('config must be a JSON object')
    vault = data.get('vault')
    if not isinstance(vault, str) or not vault.strip() or not Path(vault).expanduser().is_absolute():
        raise ConfigError('"vault" must be an absolute path')
    for key in REQUIRED_TEXT:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f'"{key}" must be a non-empty string')
    harnesses = data.get('harnesses')
    if (not isinstance(harnesses, list) or not harnesses
            or any(name not in HARNESSES for name in harnesses)
            or len(set(harnesses)) != len(harnesses)):
        raise ConfigError(f'"harnesses" must be a non-empty list of distinct names from {list(HARNESSES)}')
    companion = data.get('companion_dir')
    if companion is not None:
        relative = Path(str(companion))
        if (not isinstance(companion, str) or not companion.strip() or relative.is_absolute()
                or '..' in relative.parts):
            raise ConfigError('"companion_dir" must be a folder inside the vault, given relative to it')
    projects = data.get('projects_root')
    if projects is not None and (not isinstance(projects, str) or not projects.strip()
                                 or not Path(projects).expanduser().is_absolute()):
        raise ConfigError('"projects_root" must be an absolute path')
    nightly_at = data.get('nightly_at', '21:00')
    if (not isinstance(nightly_at, str) or not re.fullmatch(r'\d{2}:\d{2}', nightly_at)):
        raise ConfigError('"nightly_at" must be HH:MM local time')
    try:
        dt.time.fromisoformat(nightly_at)
    except ValueError:
        raise ConfigError('"nightly_at" must be HH:MM local time') from None
    engine = data.get('engine', 'auto')
    if engine not in ('auto', *HARNESSES):
        raise ConfigError('"engine" must be auto, claude, codex or agy')
    return dict(data)


def load(path: Path | None = None) -> dict:
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise ConfigError(f'NeoMyelin config not found: {path} (run install.py)') from None
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError(f'NeoMyelin config unreadable: {path}: {exc}') from None
    return validate(data)


def dumps(cfg: dict) -> str:
    """Canonical file text, so an unchanged config is byte-identical on every write."""
    return json.dumps(cfg, indent=2, ensure_ascii=False) + '\n'


def vault_path(cfg: dict) -> Path:
    return Path(cfg['vault']).expanduser().resolve()


def projects_root(cfg: dict) -> Path | None:
    """The folder holding the user's project folders, when the config names one."""
    value = cfg.get('projects_root')
    return Path(value).expanduser().resolve() if isinstance(value, str) and value.strip() else None


def companion_dir(cfg: dict) -> Path | None:
    """The companion folder; None when several folders could be it (never a guess)."""
    vault = vault_path(cfg)
    if cfg.get('companion_dir'):
        return vault / cfg['companion_dir']
    named: list[Path] = []
    candidates: list[Path] = []
    try:
        entries = sorted(vault.iterdir())
    except OSError:
        return vault / COMPANION
    for path in entries:
        if (path.name.startswith('.') or path.is_symlink() or not path.is_dir()
                or ARCHIVE.search(path.name)):
            continue
        # The last word, so an emoji before or after the name counts too: `850-Companion 🔮`.
        words = re.findall(r'[^\W\d_]+', path.name.casefold())
        if path.name == COMPANION or (words and words[-1] == 'companion'):
            named.append(path)
        elif (path / 'Core.md').is_file():
            candidates.append(path)
    found = named or candidates
    if len(found) > 1:
        return None
    return found[0] if found else vault / COMPANION
