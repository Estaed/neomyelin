#!/usr/bin/env python3
"""The vault's skills hub: `.brain/skills/<name>/` is the one source of every skill NeoMyelin manages.

Claude Code and Codex read skills from their own user folder (skill_dirs). install.py links every
hub skill into the folder of each configured one: a directory junction on Windows (no admin rights
or developer mode needed, unlike a symlink), a symlink elsewhere. Where a link cannot be made (a
drive without junctions), the skill is copied and the copy holds COPY_MARK naming the vault, so a
later run refreshes it and uninstall finds it. A skill added to the hub later is linked by the next
install run. Two copies of one skill drift apart; a link cannot.

agy reads the hub itself: install adds one entry `{"path": "<vault>/.brain/skills"}` to agy's own
`skills.json` (agy_index), whose entries agy scans one level deep for skills, so agy needs no link
(plan_agy, register_agy, unregister_agy). Earlier installs linked into ~/.gemini/antigravity/skills/,
a folder agy does not read (old_dirs); install and uninstall remove NeoMyelin's links there.

What sits under a skill's name in a harness folder decides what install does (kind()):
- nothing: linked;
- our link (it points into this hub): kept; our copy: refreshed when the hub changed;
- another NeoMyelin vault's link or copy (a link into the `.brain/skills/` of a valid NeoMyelin
  install, a broken link into a `.brain/skills/` folder that is gone, a marked copy, or the plain
  `limit` copy installs wrote before the hub): replaced by ours, as the instruction block is;
- anything else (a link somewhere else, a folder of the user's own): kept as it is and reported.

Adoption (`install.py --adopt-skills`): every real skill folder (holds SKILL.md, is not a link) in
a configured harness's folder moves into the hub and is replaced by a link. A copy goes to
`.brain/.backup/skills-<stamp>/<harness>/<name>/` first, and the hub copy is checked byte for byte
before the original goes; `.brain/skills.json` records where each skill came from before its
folder moves (a run stopped part way loses no record), and uninstall.py puts it back there. One name in two harnesses: identical content is adopted once and
linked to both; different content (from each other, or from the hub's skill of that name) is not
adopted and is reported, and both stay as they are.

Windows: `os.path.islink` is False for a junction, so a link is recognised by reading it
(`os.readlink` works for both) or by its reparse-point attribute. A link is removed with
os.unlink, never a recursive delete (one through a junction would empty the hub folder), and the
hub folder is checked afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import glob
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
import render_hooks  # noqa: E402  (reads and writes agy's skills.json as it does a hook file)

try:
    import _winapi  # Windows: CreateJunction
except ImportError:
    _winapi = None

INDEX = 'skills.json'           # in .brain/: adopted skill -> {"harnesses": [...], "origins": {harness: path}}
COPY_MARK = '.neomyelin-copy'   # inside a copy made where a link could not be; holds the vault path
# The `limit` SKILL.md carries this line; a folder holding only that file is a copy an install made
# before the hub existed.
LEGACY_MARK = b'<!-- neomyelin:skill -->'


def skill_dirs(home: Path) -> dict[str, Path]:
    """The user skill folder of each harness the hub's skills are linked into (install.skill_targets
    returns this). agy has none: it reads the hub through its skills.json (agy_index)."""
    return {
        'claude': home / '.claude' / 'skills',
        'codex': home / '.codex' / 'skills',
    }


def old_dirs(home: Path) -> dict[str, Path]:
    """Folders earlier installs linked the hub's skills into that the harness does not read: agy
    1.2.14 lists no skill from ~/.gemini/antigravity/skills/ (measured 2026-10-02, docs/harnesses.md).
    Install and uninstall remove NeoMyelin's links and copies there; anything else stays."""
    return {'agy': config.agy_dir(home, config.agy_snap()) / 'antigravity' / 'skills'}


def hub(vault: Path) -> Path:
    return vault / '.brain' / 'skills'


def hub_skills(vault: Path) -> list[str]:
    """The skills in the hub: folders holding SKILL.md."""
    folder = hub(vault)
    if not folder.is_dir():
        return []
    return sorted(path.name for path in folder.iterdir()
                  if not path.name.startswith('.') and path.is_dir() and (path / 'SKILL.md').is_file())


def is_link(path: Path) -> bool:
    """A symlink or a Windows junction (or another reparse point we must not write through)."""
    if os.path.islink(path):
        return True
    try:
        attributes = os.lstat(path).st_file_attributes  # Windows only
    except (OSError, AttributeError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def link_target(path: Path) -> Path | None:
    """Where a junction or symlink points, without the `\\\\?\\` prefix Windows puts on a
    junction's target (left on, no path comparison would ever match); None for anything else."""
    try:
        raw = os.readlink(path)
    except (OSError, ValueError):
        return None
    if raw.startswith('\\\\?\\UNC\\'):
        raw = '\\\\' + raw[len('\\\\?\\UNC\\'):]
    elif raw.startswith('\\\\?\\'):
        raw = raw[len('\\\\?\\'):]
    target = Path(raw)
    return target if target.is_absolute() else path.parent / target


def same_path(a: Path, b: Path) -> bool:
    def norm(path: Path) -> str:
        return os.path.normcase(os.path.normpath(str(path)))
    return norm(a) == norm(b) or norm(os.path.realpath(a)) == norm(os.path.realpath(b))


def _neomyelin_hub(folder: Path) -> bool:
    """`folder` is `<vault>/.brain/skills` of a NeoMyelin install (its config.json is valid), or
    such a folder that is gone (a moved or deleted vault)."""
    if folder.name != 'skills' or folder.parent.name != '.brain':
        return False
    if not folder.exists():
        return True
    try:
        config.validate(json.loads((folder.parent / 'config.json').read_text(encoding='utf-8-sig')))
        return True
    except (OSError, UnicodeError, ValueError):
        return False


def _legacy_copy(entry: Path) -> bool:
    try:
        return ([path.name for path in entry.iterdir()] == ['SKILL.md']
                and LEGACY_MARK in (entry / 'SKILL.md').read_bytes())
    except OSError:
        return False


def kind(entry: Path, vault: Path) -> str:
    """What sits at `<harness skill folder>/<name>`, seen from this vault: 'absent', 'linked' (our
    link to the hub's skill of that name), 'copy' (our copy), 'neomyelin' (another NeoMyelin
    install's link or copy), 'link' (a link somewhere else) or 'own' (anything of the user's)."""
    if not os.path.lexists(entry):
        return 'absent'
    target = link_target(entry)
    if target is not None:
        if same_path(target, hub(vault) / entry.name):
            return 'linked'
        if target.name == entry.name and _neomyelin_hub(target.parent):
            return 'neomyelin'
        return 'link'
    if is_link(entry):
        return 'link'  # a reparse point that does not read as a link: never touched
    if not entry.is_dir():
        return 'own'
    mark = entry / COPY_MARK
    if mark.is_file():
        try:
            owner = mark.read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            owner = ''
        return 'copy' if owner and same_path(Path(owner), vault) else 'neomyelin'
    return 'neomyelin' if _legacy_copy(entry) else 'own'


def tree(root: Path) -> dict[str, bytes]:
    """Every file under `root` by relative path. Raises ValueError on a link inside: it is never
    followed, and a skill holding one is left to the user."""
    files: dict[str, bytes] = {}
    pending = [root]
    while pending:
        folder = pending.pop()
        for entry in folder.iterdir():
            if is_link(entry):
                raise ValueError(f'{entry} is a link')
            if entry.is_dir():
                pending.append(entry)
            else:
                files[entry.relative_to(root).as_posix()] = entry.read_bytes()
    return files


def _copy_current(entry: Path, source: Path) -> bool:
    try:
        copy = tree(entry)
        copy.pop(COPY_MARK, None)
        return copy == tree(source)
    except (OSError, ValueError):
        return False


def make_link(link: Path, target: Path) -> bool:
    """A junction (Windows) or symlink at the free path `link` to the folder `target`; True when it
    resolves there. A half-made link is removed again. Never raises: a caller that moved a folder
    aside puts it back on False."""
    if os.path.lexists(link):
        return False
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            os.symlink(target, link, target_is_directory=True)
        elif hasattr(_winapi, 'CreateJunction'):
            _winapi.CreateJunction(str(target), str(link))  # no shell: `&` or `%` in a path stays text
        else:
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True, check=True)
        made = same_path(link_target(link) or link, target)
    except (OSError, subprocess.CalledProcessError):
        made = False
    if not made and _is_any_link(link):
        try:
            unlink(link)
        except OSError:
            pass  # the caller's next step (copy, or the rename back) fails and reports the path
    return made


def _is_any_link(path: Path) -> bool:
    return os.path.lexists(path) and (link_target(path) is not None or is_link(path))


def unlink(path: Path) -> None:
    """Remove a junction or symlink itself, never what it points to."""
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)  # a junction where unlink refuses one; rmdir removes the link, not the target
    if os.path.lexists(path):
        raise OSError(f'{path} could not be removed')


def _unlink_checked(entry: Path) -> None:
    """Remove our link and check the folder it pointed to is still there."""
    target = link_target(entry)
    existed = target is not None and target.is_dir()
    unlink(entry)
    if existed and not target.is_dir():
        raise OSError(f'{target} is gone after its link {entry} was removed; restore it from '
                      '.brain/.backup/ or git')


def rmtree(path: Path) -> None:
    """Delete a real folder (a copy of ours, or an adopted skill already backed up and linked)."""
    if is_link(path):
        raise OSError(f'{path} is a link; it is never deleted recursively')

    def retry(function, name, _error):
        os.chmod(name, stat.S_IWRITE)  # Windows refuses to delete a read-only file
        function(name)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def _copy(entry: Path, source: Path, vault: Path) -> None:
    shutil.copytree(source, entry)
    (entry / COPY_MARK).write_text(f'{vault}\n', encoding='utf-8')


def _remove(entry: Path) -> None:
    """Remove another install's link or copy (a link is unlinked, a marked copy deleted)."""
    if _is_any_link(entry):
        unlink(entry)
    else:
        rmtree(entry)


def link(vault: Path, home: Path, harnesses: list[str]) -> list[str]:
    """Link every hub skill into each harness's skill folder (agy: none, see register_agy) and clear
    NeoMyelin's links from the folders no harness reads (old_dirs); one report line per kind of
    result."""
    names, dirs = hub_skills(vault), skill_dirs(home)
    linked, copied, moved, kept, removed, failed = [], [], [], [], [], []
    for harness in harnesses:
        root = dirs.get(harness)
        if root is None:
            continue
        if is_link(root):
            kept.append(f'{harness}: {root} is itself a link (NeoMyelin does not write through it)')
            continue
        for name in names:
            entry, source = root / name, hub(vault) / name
            what = kind(entry, vault)
            try:
                if what == 'linked' or (what == 'copy' and _copy_current(entry, source)):
                    continue
                if what in ('link', 'own'):
                    target = link_target(entry)
                    kept.append(f'{harness} {name} ('
                                + (f'a link to {target}' if target else 'a link' if what == 'link'
                                   else 'a folder of your own') + ')')
                    continue
                if what == 'copy':
                    rmtree(entry)  # our own copy, out of date
                elif what == 'neomyelin':
                    target = link_target(entry)
                    _remove(entry)
                    moved.append(f'{harness} {name} (was {target or "a copy"})')
                if make_link(entry, source):
                    linked.append(f'{harness} {name}')
                else:
                    _copy(entry, source, vault)
                    copied.append(f'{harness} {name}')
            except OSError as exc:
                failed.append(f'{harness} {name}: {exc}')
        if root.is_dir():  # our links whose skill is no longer in the hub
            for entry in sorted(root.iterdir()):
                if (entry.name not in names and kind(entry, vault) == 'linked'
                        and not os.path.exists(link_target(entry))):
                    try:
                        unlink(entry)
                        removed.append(f'{harness} {entry.name}')
                    except OSError as exc:
                        failed.append(f'{harness} {entry.name}: {exc}')
    cleared = []
    for harness, root in old_dirs(home).items():
        if is_link(root) or not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            what = kind(entry, vault)
            try:
                if what == 'linked':
                    _unlink_checked(entry)
                elif what in ('copy', 'neomyelin'):
                    _remove(entry)
                else:
                    continue
                cleared.append(f'{harness} {entry.name}')
            except OSError as exc:
                failed.append(f'{harness} {entry.name} in {root}: {exc}')
    report = [f"  skills hub: {hub(vault)} ({', '.join(names) or 'empty'})"]
    report.append('  skills linked: ' + (', '.join(linked) if linked else 'unchanged'))
    if copied:
        report.append('  skills copied, a link could not be made there (a rerun refreshes the copy): '
                      + ', '.join(copied))
    if moved:
        report.append('  skills moved over from another NeoMyelin install: ' + ', '.join(moved))
    if removed:
        report.append('  skill links removed, their skill is no longer in the hub: ' + ', '.join(removed))
    if cleared:
        report.append('  old skill links removed from a folder the harness does not read (agy reads '
                      '.brain/skills/ through its skills.json): ' + ', '.join(cleared))
    if kept:
        report.append('  skills kept as they are (a link elsewhere, or a folder of your own with that name): '
                      + ', '.join(kept))
    if failed:
        report.append('  skills not linked: ' + '; '.join(failed))
    return report


def own_skills(vault: Path, home: Path, harnesses: list[str]
               ) -> tuple[dict[str, list[tuple[str, Path]]], dict[str, str]]:
    """The user's own skill folders in the configured harnesses that adoption would move, by
    name, and the names it would not move, with the reason."""
    dirs = skill_dirs(home)
    found: dict[str, list[tuple[str, Path]]] = {}
    for harness in harnesses:
        root = dirs.get(harness)
        if root is None or is_link(root) or not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if (not entry.name.startswith('.') and kind(entry, vault) == 'own'
                    and (entry / 'SKILL.md').is_file()):
                found.setdefault(entry.name, []).append((harness, entry))
    adoptable: dict[str, list[tuple[str, Path]]] = {}
    conflicts: dict[str, str] = {}
    for name, places in sorted(found.items()):
        where = ', '.join(harness for harness, _ in places)
        try:
            trees = [tree(path) for _, path in places]
        except (OSError, ValueError) as exc:
            conflicts[name] = f'{where}: cannot be read as plain files ({exc})'
            continue
        if any(other != trees[0] for other in trees[1:]):
            conflicts[name] = f'{where} hold different content under this name'
            continue
        source = hub(vault) / name
        if os.path.lexists(source):
            try:
                same = tree(source) == trees[0]
            except (OSError, ValueError):
                same = False
            if not same:
                conflicts[name] = f'{where}: differs from {source}'
                continue
        adoptable[name] = places
    return adoptable, conflicts


def describe_own(vault: Path, home: Path, harnesses: list[str]) -> list[str]:
    """Report lines naming the user's own skills, for install without --adopt-skills."""
    adoptable, conflicts = own_skills(vault, home, harnesses)
    report = []
    if adoptable:
        names = ', '.join(f"{name} ({', '.join(h for h, _ in places)})" for name, places in adoptable.items())
        report.append(f'  your own skills, not in the hub: {names}. `install.py --adopt-skills` moves them '
                      'into .brain/skills/ and links them back (a backup first; uninstall.py puts them back)')
    if conflicts:
        report.append('  skill conflict, not adopted (both stay as they are): '
                      + '; '.join(f'{name} ({reason})' for name, reason in conflicts.items()))
    return report


def read_index(vault: Path) -> dict:
    path = vault / '.brain' / INDEX
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f'{path} cannot be read ({exc})') from None
    if not isinstance(data, dict):
        raise ValueError(f'{path} must hold a JSON object')
    return data


def _write_index(vault: Path, index: dict) -> None:
    path = vault / '.brain' / INDEX
    temporary = path.with_name(f'.{INDEX}.neomyelin-tmp')
    temporary.write_text(json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True) + '\n',
                         encoding='utf-8', newline='\n')
    os.replace(temporary, path)


def _save_index(vault: Path, index: dict, existed: bool) -> None:
    """Write the record; an empty one that did not exist before this run is removed again."""
    path = vault / '.brain' / INDEX
    if index or existed:
        _write_index(vault, index)
    elif path.exists():
        path.unlink()


def _recorded(entry: object, harness: str, path: Path) -> dict:
    """A skill's record with `harness` adopted from `path`; `entry` itself is left unchanged."""
    if not isinstance(entry, dict) or not isinstance(entry.get('harnesses'), list) \
            or not isinstance(entry.get('origins'), dict):
        entry = {'harnesses': [], 'origins': {}}
    harnesses = entry['harnesses'] + ([] if harness in entry['harnesses'] else [harness])
    return {**entry, 'harnesses': harnesses, 'origins': {**entry['origins'], harness: str(path)}}


def adopt(vault: Path, home: Path, harnesses: list[str], now: dt.datetime | None = None) -> list[str]:
    """Move the user's own skills into the hub and link them back; report lines."""
    adoptable, conflicts = own_skills(vault, home, harnesses)
    try:
        index = read_index(vault)
    except ValueError as exc:
        return [f'  skills not adopted: {exc}; fix or remove it, then run install.py --adopt-skills again']
    had_index = (vault / '.brain' / INDEX).exists()
    backup =vault / '.brain' / '.backup' / f"skills-{(now or dt.datetime.now()).strftime('%Y%m%d-%H%M%S')}"
    adopted, failed = [], []
    for name, places in adoptable.items():
        source = hub(vault) / name
        created = False
        try:
            content = tree(places[0][1])
            for harness, path in places:
                copy = backup / harness / name
                shutil.copytree(path, copy)
                if tree(copy) != content:
                    raise OSError(f'the backup {copy} differs from {path}')
            if not os.path.lexists(source):
                staging = source.with_name(f'.{name}.neomyelin-adopt')
                if os.path.lexists(staging):
                    raise OSError(f'{staging} is in the way')
                shutil.copytree(places[0][1], staging)
                if tree(staging) != content:
                    rmtree(staging)
                    raise OSError(f'the copy in the hub differs from {places[0][1]}')
                os.replace(staging, source)
                created = True
        except (OSError, ValueError) as exc:
            failed.append(f'{name}: {exc}')
            continue
        done, stranded = [], []
        for harness, path in places:
            aside = path.with_name(f'.{name}.neomyelin-adopt')
            previous, moved = index.get(name), False
            try:
                if os.path.lexists(aside):
                    raise OSError(f'{aside} is in the way')
                # Recorded before the folder moves: a run stopped part way (Ctrl+C, a crash) still
                # tells uninstall where the skill goes back.
                index[name] = _recorded(previous, harness, path)
                _write_index(vault, index)
                os.rename(path, aside)
                moved = True
                if not make_link(path, source):
                    os.rename(aside, path)
                    moved = False
                    raise OSError(f'no link could be made at {path}; the skill stays there as it was')
            except OSError as exc:
                if moved:  # the original is at `aside`: its record stays, uninstall puts it back
                    stranded.append(harness)
                    failed.append(f'{name} ({harness}): {exc}; the skill is at {aside}, uninstall.py '
                                  f'puts it back at {path}')
                    continue
                if previous is None:  # the folder is where it was: its record goes again
                    index.pop(name, None)
                else:
                    index[name] = previous
                try:
                    _save_index(vault, index, had_index)
                except OSError:
                    pass  # a record naming a folder still in place: uninstall leaves that folder as it is
                failed.append(f'{name} ({harness}): {exc}')
                continue
            done.append((harness, path))
            try:
                rmtree(aside)  # backed up, in the hub and linked: the original folder goes
            except OSError as exc:
                failed.append(f'{name} ({harness}): linked, but {aside} could not be deleted ({exc}); '
                              'delete it by hand')
        if not done:
            if created and not stranded:  # nothing links to it: the hub copy goes again, the originals stay
                try:
                    rmtree(source)
                except OSError as exc:
                    failed.append(f'{name}: {source} could not be removed ({exc})')
            continue
        adopted.append(f"{name} ({', '.join(harness for harness, _ in done)})")
    report = [f'  skills adopted into the hub: {", ".join(adopted)} (backup: {backup}; record: '
              f'{vault / ".brain" / INDEX})' if adopted else '  skills adopted: none found']
    if conflicts:
        report.append('  skill conflict, not adopted (both stay as they are): '
                      + '; '.join(f'{name} ({reason})' for name, reason in conflicts.items()))
    if failed:
        report.append('  skills not adopted: ' + '; '.join(failed))
    return report


def release(vault: Path, home: Path, keep_legacy: set[str]) -> list[str]:
    """Uninstall: remove this vault's links and copies from every harness's skill folder, then put
    each adopted skill back where it came from as a real folder (a copy; the hub keeps its own).
    `keep_legacy`: harnesses whose pre-hub `limit` copy belongs to another vault's install. agy's
    entry in its skills.json goes with unregister_agy."""
    report = []
    for harness, root in [*skill_dirs(home).items(), *old_dirs(home).items()]:
        if is_link(root) or not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            what = kind(entry, vault)
            try:
                if what == 'linked':
                    _unlink_checked(entry)
                    report.append(f'  {harness} skill link removed: {entry}')
                elif what == 'copy':
                    rmtree(entry)
                    report.append(f'  {harness} skill copy removed: {entry}')
                elif harness not in keep_legacy and entry.name == 'limit' and _legacy_copy(entry):
                    (entry / 'SKILL.md').unlink()
                    entry.rmdir()
                    report.append(f'  {harness} limit skill: removed {entry}')
            except OSError as exc:
                report.append(f'  {harness} skill {entry.name}: not removed ({exc})')
    try:
        index = read_index(vault)
    except ValueError as exc:
        return report + [f'  adopted skills not put back: {exc}; their backups are in {vault / ".brain" / ".backup"}']
    for name, item in sorted(index.items()):
        source = hub(vault) / name
        origins = item.get('origins', {}) if isinstance(item, dict) else {}
        for harness in (item.get('harnesses', []) if isinstance(item, dict) else []):
            origin = Path(str(origins.get(harness, '')))
            if not origin.is_absolute():
                report.append(f'  {harness} skill {name}: no origin recorded in {INDEX}, not put back')
                continue
            try:
                if kind(origin, vault) == 'linked':  # an origin outside today's skill folders
                    _unlink_checked(origin)
                if os.path.lexists(origin):
                    if not (not is_link(origin) and origin.is_dir() and source.is_dir()
                            and tree(origin) == tree(source)):
                        report.append(f'  {harness} skill {name}: not put back, {origin} is taken')
                elif not source.is_dir():
                    report.append(f'  {harness} skill {name}: not put back, {source} is gone '
                                  f'(its backup is in {vault / ".brain" / ".backup"})')
                else:
                    shutil.copytree(source, origin)
                    report.append(f'  {harness} skill {name}: put back at {origin}')
            except (OSError, ValueError) as exc:
                report.append(f'  {harness} skill {name}: not put back ({exc})')
            report += _clear_aside(origin.with_name(f'.{name}.neomyelin-adopt'), source, harness, name)
    return report


def _clear_aside(aside: Path, source: Path, harness: str, name: str) -> list[str]:
    """The original folder an adoption stopped part way left beside its origin: deleted when the hub
    holds the same files (they are in the backup too), otherwise left and named."""
    if not os.path.lexists(aside) or is_link(aside) or not aside.is_dir():
        return []
    try:
        same = source.is_dir() and tree(aside) == tree(source)
    except (OSError, ValueError):
        same = False
    if same:
        try:
            rmtree(aside)
        except OSError as exc:
            return [f'  {harness} skill {name}: {aside}, left by an adoption stopped part way, could not be '
                    f'deleted ({exc}); the same files are in {source}, delete it by hand']
        return [f'  {harness} skill {name}: removed {aside}, left by an adoption stopped part way '
                f'(the same files are in {source})']
    return [f'  {harness} skill {name}: {aside} is left from an adoption stopped part way and differs '
            f'from {source}; compare and delete it by hand']


def problems(vault: Path, home: Path, harnesses: list[str]) -> list[str]:
    """doctor.py: each hub skill not linked in a configured harness, each broken link or link
    pointing elsewhere, and (agy) a skills.json without the hub's entry."""
    found = []
    names, dirs = hub_skills(vault), skill_dirs(home)
    for harness in harnesses:
        root = dirs.get(harness)
        if root is None:
            if harness == 'agy':
                found += agy_problems(vault, home)
            continue
        for name in names:
            entry = root / name
            what = kind(entry, vault)
            target = link_target(entry)
            if what == 'linked':
                continue
            if what == 'copy':
                if not _copy_current(entry, hub(vault) / name):
                    found.append(f'{harness} {name}: copy out of date')
            elif what == 'absent':
                found.append(f'{harness} {name}: not linked')
            elif target is not None and not os.path.exists(target):
                found.append(f'{harness} {name}: broken link to {target}')
            elif target is not None:
                found.append(f'{harness} {name}: points elsewhere ({target})')
            else:
                found.append(f'{harness} {name}: a folder of your own has this name')
        if root.is_dir() and not is_link(root):
            for entry in sorted(root.iterdir()):
                target = link_target(entry)
                if entry.name not in names and kind(entry, vault) == 'linked' and not os.path.exists(target):
                    found.append(f'{harness} {entry.name}: broken link to {target}')
    return found


# ---------------------------------------------------------------------------------------------
# agy: its own skills.json names the hub. Format (agy's bundled agy-customizations guide,
# docs/json_configs.md): {"entries": [{"path": "<dir>", "exclude": [...], "include_only": [...]}],
# "inherits": [...]}; each entry's folder is scanned one level deep, so <hub>/<name>/SKILL.md is a
# skill. agy 1.2.14 on Windows listed skills from an absolute path written with forward slashes,
# with backslashes and with a space in it (measured 2026-10-02).

@dataclass
class AgyPlan:
    path: Path                # agy's skills.json
    raw: bytes | None         # its bytes now; None when it does not exist
    desired: dict | None      # what install writes; None when the hub is already named (or a link)
    link: bool = False        # a link is never written through
    replaced: list[str] = field(default_factory=list)  # other NeoMyelin installs' entries it drops


def agy_index(home: Path) -> Path:
    """agy's skills.json (the snap's own .gemini for an agy snap, as its hooks and GEMINI.md)."""
    return config.agy_dir(home, config.agy_snap()) / 'config' / 'skills.json'


def _entry_path(entry: object, home: Path) -> Path | None:
    """The folder an entry names, when it is absolute or under `~/`; a workspace-relative one is
    resolved per project by agy and never names the hub."""
    if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
        return None
    raw = entry['path'].strip()
    if raw == '~' or raw.startswith(('~/', '~\\')):
        return home / raw[2:]
    path = Path(raw)
    return path if path.is_absolute() else None


def _names_hub(entry: object, vault: Path, home: Path) -> bool:
    path = _entry_path(entry, home)
    return path is not None and same_path(path, hub(vault))


def _our_entry(entry: object, vault: Path, home: Path) -> bool:
    """The entry install writes: a `path` naming this vault's hub and nothing else. One with
    `exclude` or `include_only` was written by the user and stays."""
    return isinstance(entry, dict) and set(entry) == {'path'} and _names_hub(entry, vault, home)


def _entries(data: object, path: Path) -> list:
    if not isinstance(data, dict):
        raise ValueError(f'{path}: the file root is not a JSON object; fix it first, nothing was written')
    entries = data.get('entries', [])
    if not isinstance(entries, list):
        raise ValueError(f'{path}: "entries" is not a list; fix it first, nothing was written')
    return entries


def _other_install(entry: object, vault: Path, home: Path) -> bool:
    """An entry another NeoMyelin install wrote: only a `path`, naming the `.brain/skills` of
    another valid NeoMyelin vault (or of one that is gone). Replaced by ours, as that install's
    links are, so agy does not list the same skill twice."""
    path = _entry_path(entry, home)
    return (isinstance(entry, dict) and set(entry) == {'path'} and path is not None
            and not same_path(path, hub(vault)) and _neomyelin_hub(path))


def plan_agy(vault: Path, home: Path) -> AgyPlan:
    """What naming the hub in agy's skills.json would write; raises ValueError, writes nothing.
    Every other key and entry stays (another NeoMyelin install's entry is replaced); an entry
    that already names the hub, in any form, is kept and nothing is added."""
    path = agy_index(home)
    if is_link(path):
        return AgyPlan(path, None, None, link=True)
    current, raw = render_hooks.read_settings(path)
    entries = _entries(current, path)
    kept = [entry for entry in entries if not _other_install(entry, vault, home)]
    replaced = [str(entry['path']) for entry in entries if _other_install(entry, vault, home)]
    if any(_names_hub(entry, vault, home) for entry in kept):
        return AgyPlan(path, raw, {**current, 'entries': kept} if replaced else None, replaced=replaced)
    return AgyPlan(path, raw, {**current, 'entries': [*kept, {'path': hub(vault).as_posix()}]},
                   replaced=replaced)


def register_agy(planned: AgyPlan, vault: Path) -> list[str]:
    """Write a plan_agy plan (backup first); one report line."""
    if planned.link:
        return [f'  agy skills: skipped, {planned.path} is a link (NeoMyelin does not write through links; '
                f'add {{"path": "{hub(vault).as_posix()}"}} to its "entries" by hand)']
    if planned.desired is None:
        return [f'  agy skills: unchanged {planned.path} (it names {hub(vault)})']
    backup = render_hooks.write_settings(planned.path, planned.desired, planned.raw)
    return [f"  agy skills: {'created' if planned.raw is None else 'updated'} {planned.path}, "
            f'agy reads {hub(vault)} from it' + (f' (backup: {backup})' if backup else '')
            + (f"; replaced another NeoMyelin install's entry: {', '.join(planned.replaced)}"
               if planned.replaced else '')]


def _bare(data: dict) -> dict:
    """`data` without an empty `entries` list: `{}` and `{"entries": []}` say the same to agy."""
    return {key: value for key, value in data.items() if not (key == 'entries' and value == [])}


def _before_install(path: Path, wanted: dict) -> bytes | None:
    """The bytes of the newest backup next to `path` that holds `wanted` (compared as JSON): the
    user's own file as it was before install, so uninstall gives it back byte for byte."""
    pattern = f'{glob.escape(path.name)}.neomyelin-*.bak'
    for backup in sorted(path.parent.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            raw = backup.read_bytes()
            data = json.loads(raw.decode('utf-8-sig'))
        except (OSError, UnicodeError, ValueError):
            continue
        if isinstance(data, dict) and _bare(data) == wanted:
            return raw
    return None


def unregister_agy(vault: Path, home: Path) -> list[str]:
    """Uninstall: take this vault's entry out of agy's skills.json (backup first). The file goes
    back to the bytes it had before install when a backup holds exactly what is left; it is removed
    when nothing else is left and no such backup exists (install created it)."""
    path = agy_index(home)
    if is_link(path) or not path.is_file():
        return []
    try:
        current, raw = render_hooks.read_settings(path)
        entries = _entries(current, path)
    except ValueError as exc:
        return [f'  agy skills: {exc}; remove the entry naming {hub(vault)} by hand']
    kept = [entry for entry in entries if not _our_entry(entry, vault, home)]
    if len(kept) == len(entries):
        return []
    left = _bare({**current, 'entries': kept})
    before = _before_install(path, left)
    if before is not None:
        backup = render_hooks._backup(path, raw)
        render_hooks._replace(path, before)
        status = 'entry removed, the file is as it was before install'
    elif not left:
        backup = render_hooks._backup(path, raw)
        path.unlink()
        status = 'removed, it held only our entry'
    else:
        backup = render_hooks.write_settings(path, {**current, 'entries': kept}, raw)
        status = 'entry removed'
    return [f'  agy skills: {status} ({path}; backup: {backup})']


def agy_problems(vault: Path, home: Path) -> list[str]:
    """doctor.py: agy sees the hub's skills only through an entry in its skills.json."""
    path = agy_index(home)
    try:
        current, _ = render_hooks.read_settings(path)
        entries = _entries(current, path)
    except ValueError as exc:
        return [f'agy: {exc}']
    if any(_names_hub(entry, vault, home) for entry in entries):
        return []
    return [f'agy: {path} does not name {hub(vault)}, so agy does not see these skills']
