"""Remove NeoMyelin from the harnesses; the vault stays as it is.

    python uninstall.py --vault <your vault>

Takes back everything install.py wrote outside the vault, for this vault only:
1. Our hook entries in Claude Code's settings.json, Codex's hooks.json and agy's hooks file (both
   the user-level files and the vault's own, from --scope project), and the Claude status line
   when it runs this vault's script. Foreign hooks, another vault's hooks and every other key
   stay. `env.PYTHONUTF8` stays too: it is harmless and other tools may rely on it.
2. The instruction block between the NeoMyelin markers in ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md
   and agy's GEMINI.md (the import line or the copy); the rest of each file is kept, and a file
   that held only our block goes.
3. The skill links into this vault's hub (`.brain/skills/`) in every harness's skill folder (and
   in ~/.gemini/antigravity/skills/, where earlier installs linked agy's), and the copies made
   where a link could not be. A link is removed on its own, never through a recursive delete, and
   the hub folder it pointed to is checked afterwards. Each skill `install.py --adopt-skills` moved
   into the hub goes back where `.brain/skills.json` says it came from, as a real folder (a copy;
   the hub keeps its own). A link elsewhere and the user's own skills stay.
4. The entry naming this vault's hub in agy's skills.json. The file goes back to the bytes it had
   before install (from install's backup next to it) when that is what is left, and is removed
   when install created it and nothing else is in it; other keys and entries stay.
A block written for another vault on this machine is kept: the block names its vault, and that
install still uses it (and an install from before the hub, its `limit` copy).

Every harness file is backed up before it changes, as install.py does. Nothing inside the vault
is touched: notes, receipts, tasks, `.brain/` (skills and instructions included) and `brain.py`
stay, so the vault keeps working as plain files and a later install picks it up again. Running it
twice changes nothing.
"""
from __future__ import annotations

import argparse
import codecs
import os
from pathlib import Path
import sys

from install import config, render_hooks, render_instructions, skills_hub


def _hooks(vault: Path, home: Path) -> list[str]:
    report = []
    for harness in render_hooks.TARGETS:
        for scope in render_hooks.SCOPES:
            try:
                path = render_hooks.settings_path(harness, scope, home, vault)
            except render_hooks.Unsupported:
                continue  # agy as a snap: install never wrote its hook file
            if not path.is_file():
                continue
            current, raw = render_hooks.read_settings(path)
            desired = render_hooks.unregister(harness, current, vault)
            if desired == current:
                continue
            backup = render_hooks.write_settings(path, desired, raw)
            report.append(f'  {harness} hooks: removed from {path}' + (f' (backup: {backup})' if backup else ''))
    return report


def _block_vault_is(text: str, vault: Path) -> bool:
    """The block names the vault it was written for (`{vault}` in the template); a block written
    for another vault on this machine belongs to that install and stays."""
    start = text.index(render_instructions.BLOCK_START)
    block = text[start:text.index(render_instructions.BLOCK_END, start)]
    block = block.replace('\\ ', ' ')  # Claude's import line escapes each space in the path
    return f'{vault.as_posix()}/'.casefold() in block.replace('\\', '/').casefold()


def _instructions(vault: Path, home: Path, others: set[str]) -> list[str]:
    report = []
    for harness in render_instructions.FILES:
        path = (config.agy_dir(home, config.agy_snap()) / 'GEMINI.md' if harness == 'agy'
                else home / render_instructions.FILES[harness])
        if not path.is_file():
            continue
        raw = path.read_bytes()
        bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b''
        try:
            text = raw[len(bom):].decode('utf-8')
        except UnicodeError:
            continue  # install refuses such a file too, so it holds no block of ours
        if render_instructions.BLOCK_START not in text:
            continue
        try:
            ours = _block_vault_is(text, vault)
        except ValueError:  # broken markers: remove() below reports them
            ours = True
        if not ours:
            others.add(harness)
            report.append(f'  {harness} instructions: kept, the block in {path} is for another vault')
            continue
        if path.is_symlink():
            report.append(f'  {harness} instructions: skipped, {path} is a link; remove the block by hand')
            continue
        left = render_instructions.remove(text)
        backup = render_hooks._backup(path, raw)
        if left.strip():
            render_hooks._replace(path, bom + left.encode('utf-8'))
            report.append(f'  {harness} instructions: block removed from {path} (backup: {backup})')
        else:
            path.unlink()
            report.append(f'  {harness} instructions: {path} held only our block, removed (backup: {backup})')
    return report


def uninstall(vault: Path, home: Path) -> list[str]:
    # A vault deleted or moved without uninstall leaves hooks that point at missing scripts, and
    # Claude Code then refuses every prompt (Python exits 2 on a missing file, which Claude reads
    # as "block"). So a folder that is gone is cleaned up by its path; an existing folder that is
    # not a NeoMyelin vault is refused.
    gone = not os.path.lexists(vault)
    if not gone and not (vault / '.brain' / 'config.json').is_file():
        raise ValueError(f'{vault} has no NeoMyelin install (.brain/config.json is missing); nothing was removed')
    report = [f'NeoMyelin uninstall -> {vault}']
    if gone:
        report.append('  the vault folder is gone: removing every hook, block and skill link that '
                      'points at it (adopted skills cannot be put back; their only copies were in it)')
    others: set[str] = set()  # harnesses whose instruction block another vault's install wrote
    removed = (_hooks(vault, home) + _instructions(vault, home, others)
               + skills_hub.release(vault, home, others) + skills_hub.unregister_agy(vault, home))
    report += removed or ['  nothing to remove: no hook, instruction block or skill of this vault was found']
    if not gone:
        report.append(f'  vault kept as is: {vault} (notes, receipts, tasks, .brain/, brain.py); '
                      'delete the folder yourself if you want it gone')
    return report


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--vault', type=Path, required=True, help='The vault NeoMyelin was installed for.')
    parser.add_argument('--home', type=Path, default=None, help='Replaces the user home for every harness file.')
    args = parser.parse_args(argv)
    vault = args.vault.expanduser().resolve()
    home = (args.home or Path.home()).expanduser().resolve()
    try:
        report = uninstall(vault, home)
    except (ValueError, OSError) as exc:
        print(f'uninstall.py: error: {exc}', file=sys.stderr)
        return 1
    print('\n'.join(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
