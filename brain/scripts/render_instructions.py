#!/usr/bin/env python3
"""Write NeoMyelin's instruction block into each harness's user-level instruction file.

    python .brain/scripts/render_instructions.py [--harness claude|codex|agy ...] [--home DIR] [--check]

| harness | file, read at the start of every session in any folder |
|---------|---------------------------------------------------------|
| claude  | <home>/.claude/CLAUDE.md                                |
| codex   | <home>/.codex/AGENTS.md                                 |
| agy     | <home>/.gemini/GEMINI.md                                |

The text (templates/instructions.md, filled from config.py) is what makes every session feed the
brain: who the assistant is and where its vault lives, the receipt rule with its OBSERVATION,
REACTION and CORRECTION lines (the personality's only input), when to recall, when to record
friction with `gardener.py record`, principles 1-4 and the one-AGENTS.md-per-project convention.
Each harness sees its own `--harness` name and file-writing tool.

The text lives in the vault, `<vault>/.brain/instructions/<harness>.md`, rewritten when it changes.
The block in the harness's file holds what the harness can load (IMPORTS, measured with the real
CLIs; docs/harnesses.md): Claude Code loads `@<path>` imports from CLAUDE.md, so its block is the
one import line (a space in the path escaped as `\\ `; any other whitespace makes it a copy).
Codex has no import, and agy 1.2.14 loads GEMINI.md but does not expand `@` imports, so their
blocks are a copy of the text, generated from the same file. A block that holds the full text
from an older install is replaced by the new form.

The block sits between NeoMyelin's markers. Everything outside them stays byte for byte, so do the
file's line endings and BOM; a first write appends the block after the existing text. A changed
file is first copied next to itself as `<name>.neomyelin-<stamp>.bak`; an unchanged one is never
rewritten, a missing one is created. A file that is a link is not written (its target may belong
to another tool or repository); the report says so. Without --harness: every harness in the
config whose command is on PATH. --check writes nothing and exits 1 when a file lacks the
current block (or an instruction file in the vault that is out of date).

agy: `~/.gemini/GEMINI.md` is Antigravity's global rules file; a real agy 1.2.14 turn on Windows
read a token written there (2026-10-02). Codex reads `~/.codex/AGENTS.override.md` instead of
AGENTS.md while the override has content; the report names it.
"""
from __future__ import annotations

import argparse
import codecs
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
import render_hooks  # noqa: E402

FILES = {'claude': '.claude/CLAUDE.md', 'codex': '.codex/AGENTS.md', 'agy': '.gemini/GEMINI.md'}
CODEX_OVERRIDE = '.codex/AGENTS.override.md'
WRITER = {'claude': 'the Write tool', 'codex': 'apply_patch', 'agy': 'your file-writing tool'}
BLOCK_START = '<!-- neomyelin-instructions:start -->'
BLOCK_END = '<!-- neomyelin-instructions:end -->'
# The first line of each instruction file, and so of each copy between the markers.
NOTE = '<!-- Written by NeoMyelin install.py; a rerun replaces this text. -->'
SOURCES = 'instructions'  # <vault>/.brain/instructions/<harness>.md
IMPORTS = ('claude',)     # harnesses whose user-level file loads `@<path>` imports (measured)
TEMPLATE = 'instructions.md'
# Installed: <vault>/.brain/templates/ (install.py copies it there); in the repository: templates/.
TEMPLATE_PATHS = (SCRIPT_DIR.parent / 'templates' / TEMPLATE, SCRIPT_DIR.parent.parent / 'templates' / TEMPLATE)


def launcher() -> str:
    """How the block calls Python: each platform's default launcher, as the route table and the
    receipt reminder do. It runs the same from bash, cmd and PowerShell."""
    return 'py -3' if os.name == 'nt' else 'python3'


def template_text() -> str:
    for path in TEMPLATE_PATHS:
        if path.is_file():
            return path.read_text(encoding='utf-8')
    raise ValueError(f'the instruction template {TEMPLATE} is missing; run install.py again')


def source_path(cfg: dict, harness: str) -> Path:
    """Where the harness's instruction text lives in the vault."""
    return config.vault_path(cfg) / '.brain' / SOURCES / f'{harness}.md'


def import_line(path: Path) -> str | None:
    """Claude Code's import of `path`: `@` and the absolute path, each space escaped as `\\ `
    (measured with Claude Code 2.1.286: an unescaped space ends the path). None when the path holds
    other whitespace."""
    posix = path.as_posix()
    if re.search(r'[^\S ]', posix):
        return None
    return '@' + posix.replace(' ', '\\ ')


def render(cfg: dict, harness: str, template: str | None = None) -> str:
    """The block for one harness, markers included, LF line endings: the import line where the
    harness loads imports, else the instruction text itself."""
    line = import_line(source_path(cfg, harness)) if harness in IMPORTS else None
    body = line or text(cfg, harness, template).rstrip('\n')
    return f'{BLOCK_START}\n{body}\n{BLOCK_END}'


def text(cfg: dict, harness: str, template: str | None = None) -> str:
    """The instruction text for one harness: the content of .brain/instructions/<harness>.md, LF."""
    companion = config.companion_dir(cfg)
    if companion is None:
        raise config.ConfigError('several folders could be the companion folder; '
                                 'set "companion_dir" in .brain/config.json')
    values = {'assistant': cfg['assistant_name'], 'user': cfg['user_name'], 'language': cfg['language'],
              'vault': config.vault_path(cfg).as_posix(), 'companion': companion.as_posix(),
              'python': launcher(), 'harness': harness, 'writer': WRITER[harness]}
    body = template_text() if template is None else template
    body = re.sub(r'\{(\w+)\}', lambda match: values.get(match.group(1), match.group(0)), body)
    return f'{NOTE}\n{body.strip()}\n'


def merge(text: str, block: str) -> str:
    """`text` with `block` in place of our old block, or appended after the existing text, which
    stays a byte-identical prefix. The block takes the file's line ending. Raises ValueError when
    our markers are broken or repeated."""
    newline = '\r\n' if '\r\n' in text else '\n'
    block = block.replace('\n', newline)
    starts, ends = text.count(BLOCK_START), text.count(BLOCK_END)
    if starts != ends or starts > 1 or (starts and text.index(BLOCK_END) < text.index(BLOCK_START)):
        raise ValueError(f'its {BLOCK_START} ... {BLOCK_END} markers are broken or repeated; '
                         'fix them by hand (keep one pair, or none)')
    if starts:
        return text[:text.index(BLOCK_START)] + block + text[text.index(BLOCK_END) + len(BLOCK_END):]
    if not text.strip():
        return text + block + newline
    gap = newline if text.endswith('\n') else newline * 2
    return text + gap + block + newline


def remove(text: str) -> str:
    """`text` without our block and the blank lines `merge` put around it; '' when nothing else
    is left. Raises ValueError when our markers are broken or repeated."""
    starts, ends = text.count(BLOCK_START), text.count(BLOCK_END)
    if starts != ends or starts > 1 or (starts and text.index(BLOCK_END) < text.index(BLOCK_START)):
        raise ValueError(f'its {BLOCK_START} ... {BLOCK_END} markers are broken or repeated; '
                         'fix them by hand (keep one pair, or none)')
    if not starts:
        return text
    newline = '\r\n' if '\r\n' in text else '\n'
    before = text[:text.index(BLOCK_START)].rstrip('\r\n')
    after = text[text.index(BLOCK_END) + len(BLOCK_END):].lstrip('\r\n')
    if not before.strip():
        return after if after.strip() else ''
    return before + newline + (newline + after if after.strip() else '')


@dataclass
class Target:
    harness: str
    path: Path
    raw: bytes | None        # the file now; None when absent
    data: bytes              # the file with the current block
    source: Path             # the instruction text in the vault
    source_raw: bytes | None
    source_data: bytes
    link: bool = False       # a link is never written
    note: str = ''           # a reason the harness may still not load the block

    def current(self) -> bool:
        return self.raw == self.data and self.source_raw == self.source_data


def _target(cfg: dict, harness: str, home: Path, template: str | None) -> Target:
    # agy's folder moves when agy is a snap (config.agy_dir); its GEMINI.md was read there in WSL.
    path = (config.agy_dir(home, config.agy_snap()) / 'GEMINI.md' if harness == 'agy'
            else home / FILES[harness])
    source = source_path(cfg, harness)
    try:
        raw = path.read_bytes() if path.exists() else None
        source_raw = source.read_bytes() if source.exists() else None
    except OSError as exc:
        raise ValueError(f'{exc}; nothing was written') from None
    bom = codecs.BOM_UTF8 if raw and raw.startswith(codecs.BOM_UTF8) else b''
    try:
        current = (raw or b'')[len(bom):].decode('utf-8')
        data = bom + merge(current, render(cfg, harness, template)).encode('utf-8')
    except UnicodeError:
        raise ValueError(f'{path} is not UTF-8 text; nothing was written') from None
    except ValueError as exc:
        raise ValueError(f'{path}: {exc}; nothing was written') from None
    note = ''
    if harness == 'codex':
        override = home / CODEX_OVERRIDE
        try:
            if override.is_file() and override.read_text(encoding='utf-8-sig', errors='replace').strip():
                note = (f'Codex reads {override} instead while it has content, so the block loads only '
                        'after you copy it there or empty that file')
        except OSError:
            pass
    return Target(harness, path, raw, data, source, source_raw, text(cfg, harness, template).encode('utf-8'),
                  path.is_symlink(), note)


def plan(cfg: dict, harnesses: list[str], home: Path, template: str | None = None) -> list[Target]:
    """What writing would change, one Target per harness; raises ValueError, writes nothing."""
    unknown = [name for name in harnesses if name not in FILES]
    if unknown:
        raise ValueError(f'no instruction file for {", ".join(unknown)}')
    return [_target(cfg, name, home, template) for name in harnesses]


def apply(targets: list[Target]) -> list[str]:
    """Write every stale target: the vault's instruction file, then the harness's file (backup
    first); one report line per harness, and one when the vault's file was written."""
    lines = []
    for target in targets:
        if target.source_raw != target.source_data:
            render_hooks._replace(target.source, target.source_data)
            lines.append(f'  {target.harness} instructions: text written to {target.source}')
        if target.raw == target.data:
            status = f'unchanged {target.path}'
        elif target.link:
            status = f'skipped, {target.path} is a link (NeoMyelin does not write through links; add the block by hand)'
        else:
            backup = render_hooks._backup(target.path, target.raw) if target.raw is not None else None
            render_hooks._replace(target.path, target.data)
            status = (f"{'created' if target.raw is None else 'updated'} {target.path}"
                      + (f' (backup: {backup})' if backup else ''))
        lines.append(f'  {target.harness} instructions: {status}')
        if target.note:
            lines.append(f'    note: {target.note}')
    return lines


def installed(cfg: dict) -> list[str]:
    """The config's harnesses whose command is on PATH, in config order."""
    return [name for name in cfg['harnesses'] if shutil.which(render_hooks.CLIS[name])]


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--harness', action='append', choices=tuple(FILES),
                        help='Repeatable. Default: every configured harness whose command is on PATH.')
    parser.add_argument('--home', type=Path, default=None, help='Replaces the user home (tests).')
    parser.add_argument('--check', action='store_true', help='Write nothing; exit 1 if a block is missing or stale.')
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        home = (args.home or Path.home()).expanduser().resolve()
        targets = plan(cfg, list(dict.fromkeys(args.harness or installed(cfg))), home)
        if not targets:
            print('no configured harness is installed; nothing to do')
            return 0
        if args.check:
            for target in targets:
                print(f"{'OK' if target.current() else 'MISSING'}  {target.path} ({target.source})")
            return 0 if all(target.current() for target in targets) else 1
        print('\n'.join(line.strip() for line in apply(targets)))
    except (ValueError, OSError) as exc:
        print(f'render_instructions: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
