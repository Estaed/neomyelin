#!/usr/bin/env python3
"""Install NeoMyelin into a vault folder: an empty one, or one that already holds notes.

    python install.py --vault <dir> --config <your.json> [--home <dir>] [--scope user|project] [--adopt-skills]

--config holds who you are: user_name, assistant_name, language and harnesses (copy
templates/config.example.json and put your own names in; the example names are refused). It is
needed once: later runs reuse <vault>/.brain/config.json. A first install without it writes a
neutral identity ("the assistant", "the user", "the user's language", every harness) and says
so on each run, so no made-up name is ever injected into a session as fact.

1. Refuses a <vault>/.brain/ that NeoMyelin did not write (no valid .brain/config.json), and a
   <vault>/brain.py that is not NeoMyelin's engine.
2. Writes <vault>/.brain/config.json; `vault` is always this vault.
3. Copies brain/ into <vault>/.brain/, skills/ into the skills hub <vault>/.brain/skills/, and the
   engine (engine/brain.py: receipts, tasks, the daily view) to <vault>/brain.py, only files whose
   bytes differ. In a skill's SKILL.md, `{script:<name>.py}` becomes the command that runs
   <vault>/.brain/scripts/<name>.py by absolute path with the platform's launcher, so the skill
   runs from any working folder.
4. Lays out the vault (`plan_layout`): config.FOLDERS, each with its note from templates/folders/;
   the companion files (COMPANION_FILES) in the companion folder and .brain/patterns.md, each
   only when absent, never overwritten; our block (the route table) between NeoMyelin's markers
   in the vault's AGENTS.md, the rest of that file untouched. Nothing else in the vault is moved
   or removed; the report names the files it kept.
5. Registers the hooks (render_hooks.HOOKS) of every harness in the config whose command (claude, codex,
   agy) is on PATH; a missing one is skipped with one line saying so, and so is a harness that
   cannot run from this vault (Windows: Codex and agy need a vault path without spaces).
6. Writes the instructions (templates/instructions.md: receipt rule, recall, friction,
   principles 1-4, one AGENTS.md per project) to <vault>/.brain/instructions/<harness>.md and a
   block between NeoMyelin's markers into the user-level instruction file of every harness in the
   config whose command is on PATH (render_instructions.FILES): Claude's block imports the file,
   Codex's and agy's are a copy of it. The rest of each file is kept. --no-instructions skips it,
   and so does --scope project, which writes no user-level file.
7. Links every skill in the hub into the user skill folder of Claude Code and Codex when configured
   (skills_hub: a junction on Windows, a symlink elsewhere, a copy where neither can be made); a
   link elsewhere or a folder of the user's own under the same name is kept. agy reads the hub
   itself: one entry naming <vault>/.brain/skills goes into agy's skills.json (every other key and
   entry kept; another NeoMyelin vault's entry is replaced, as its links are), and NeoMyelin's old
   links in ~/.gemini/antigravity/skills/, a folder agy does not read, are removed. The report
   names the user's own skills; --adopt-skills moves them into the hub and links them back (backup
   in .brain/.backup/, origins in .brain/skills.json, uninstall.py puts them back). --no-skills and
   --scope project leave the user-level skill folders and agy's skills.json alone.

Idempotent: a second run with the same inputs writes nothing. A harness hook or instruction file
is backed up before it is changed. --home replaces the user's home for every harness file (tests
never touch the real one); --scope project writes the vault's own harness files. Everything is
checked before the first write, so a bad input leaves the vault as it was.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys

sys.dont_write_bytecode = True  # importing brain/scripts must not leave caches in the repo
ROOT = Path(__file__).resolve().parent
BRAIN = ROOT / 'brain'
ENGINE = ROOT / 'engine' / 'brain.py'  # installed as <vault>/brain.py
ENGINE_MARK = b'NeoMyelin engine:'     # first docstring line: a brain.py holding it is ours
TEMPLATES = ROOT / 'templates'
SKILLS = ROOT / 'skills'  # installed into <vault>/.brain/skills/, the hub
sys.path.insert(0, str(BRAIN / 'scripts'))

import config  # noqa: E402
import render_hooks  # noqa: E402
import render_instructions  # noqa: E402
import skills_hub  # noqa: E402

# A first install without --config: wording that asserts nothing about who anyone is.
NEUTRAL = {'user_name': 'the user', 'assistant_name': 'the assistant', 'language': "the user's language"}
# The companion files: the session hook prints Rules, Core and Personality; the nightly jobs
# write into Personality, Evolution and Decisions. Placed from templates/ when absent.
COMPANION_FILES = ('Core.md', 'Rules.md', 'Personality.md', 'Evolution.md', 'Decisions.md')
FOLDER_NOTE = 'index.md'
BLOCK_START, BLOCK_END = '<!-- neomyelin:start -->', '<!-- neomyelin:end -->'
EVIDENCE = 'patterns.md'  # in .brain/


def _check_engine(vault: Path) -> None:
    """Never replace a brain.py at the vault root that is not NeoMyelin's engine."""
    if not ENGINE.is_file():
        raise ValueError(f'{ENGINE} is missing from this NeoMyelin copy; nothing was written')
    target = vault / 'brain.py'
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError(f'{target} is a link or a folder, not NeoMyelin\'s engine; nothing was written. '
                         'Move it, then run install.py again.')
    try:
        if ENGINE_MARK not in target.read_bytes():
            raise ValueError(f'{target} exists and is not NeoMyelin\'s engine; nothing was written. '
                             'Move or rename it, then run install.py again.')
    except FileNotFoundError:
        pass


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f'cannot read {path}: {exc}') from None


def _check_brain_folder(vault: Path) -> None:
    """Never write into a .brain/ someone else made (another brain layer lives there)."""
    brain = vault / '.brain'
    if not brain.exists() and not brain.is_symlink():
        return
    if not brain.is_dir():
        raise ValueError(f'{brain} exists and is not a folder; nothing was written')
    if not any(brain.iterdir()):
        return
    try:
        config.validate(json.loads((brain / 'config.json').read_text(encoding='utf-8-sig')))
        return
    except (OSError, UnicodeError, ValueError):
        pass
    raise ValueError(f'{brain} exists and is not a NeoMyelin install (it has no valid .brain/config.json), '
                     'so install.py will not write into it. Nothing was written. Move or rename that '
                     'folder, then run install.py again.')


def _config(vault: Path, given: Path | None) -> dict:
    installed = vault / '.brain' / 'config.json'
    if given is not None:
        source, data = str(given), _load_json(given)
    elif installed.exists():
        source, data = str(installed), _load_json(installed)
    else:
        source, data = 'the neutral default', {**NEUTRAL, 'harnesses': list(config.HARNESSES)}
    if not isinstance(data, dict):
        raise ValueError(f'{source} must hold a JSON object')
    cfg = config.validate({**data, 'vault': str(vault)})
    example = _load_json(TEMPLATES / 'config.example.json')
    if isinstance(example, dict) and (cfg['user_name'], cfg['assistant_name']) == (
            example.get('user_name'), example.get('assistant_name')):
        raise ValueError(f'{source} still has the example names from templates/config.example.json '
                         f'("{cfg["user_name"]}", "{cfg["assistant_name"]}"); put your own names in it '
                         'and pass it with --config. Nothing was written.')
    return cfg


def script_command(vault: Path, name: str) -> str:
    """The command that runs <vault>/.brain/scripts/<name> from any folder: the platform's launcher
    and the script's absolute path, quoted (it runs the same from bash, cmd and PowerShell)."""
    path = (vault / '.brain' / 'scripts' / name).as_posix()
    quoted = f'"{path}"' if os.name == 'nt' else shlex.quote(path)
    return f'{launcher()} {quoted}'


def render_skill(data: bytes, vault: Path) -> bytes:
    """A shipped SKILL.md with each `{script:<name>.py}` replaced by script_command."""
    text = re.sub(r'\{script:([\w.-]+\.py)\}', lambda match: script_command(vault, match.group(1)),
                  data.decode('utf-8'))
    return text.encode('utf-8')


def _brain_files(vault: Path) -> dict[Path, bytes]:
    files = {}
    for path in sorted(BRAIN.rglob('*')):
        relative = path.relative_to(BRAIN)
        if path.is_file() and '__pycache__' not in relative.parts and '.state' not in relative.parts \
                and path.suffix != '.pyc':
            files[relative] = path.read_bytes()
    # render_instructions.py, run from the vault, reads its template here.
    template = Path('templates') / render_instructions.TEMPLATE
    files[template] = (TEMPLATES / render_instructions.TEMPLATE).read_bytes()
    # The skills NeoMyelin ships, into the hub every harness links to.
    for path in sorted(SKILLS.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts:
            data = path.read_bytes()
            files[Path('skills') / path.relative_to(SKILLS)] = (render_skill(data, vault)
                                                                 if path.name == 'SKILL.md' else data)
    return files


def skill_targets(home: Path) -> dict[str, Path]:
    """Each harness's user skill folder, where install links the hub's skills (uninstall.py and
    doctor.py read the same folders). agy has none: it reads the hub through its skills.json."""
    return skills_hub.skill_dirs(home)


def _write_if_changed(path: Path, data: bytes) -> bool:
    try:
        if path.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.neomyelin-tmp')
    temporary.write_bytes(data)
    temporary.replace(path)
    return True


# ---------------------------------------------------------------------------------------------
# Vault layout: the numbered folders, the companion files the brain writes into, the route table.

@dataclass
class Layout:
    """What install will write into the vault; built by plan_layout before any write."""
    folders: list[Path] = field(default_factory=list)          # to create
    files: dict[Path, bytes] = field(default_factory=dict)     # absent notes and companion files
    kept: list[Path] = field(default_factory=list)             # companion files already there
    agents: tuple[Path, bytes] | None = None                   # AGENTS.md and its new bytes
    agents_status: str = 'NeoMyelin block unchanged'


def folder_template(name: str) -> Path:
    """`200-Goals ⚔️` -> templates/folders/200-Goals.md (file names without emoji)."""
    return TEMPLATES / 'folders' / f"{name.split(' ')[0]}.md"


def launcher() -> str:
    """How the vault's instructions call Python: the launcher each platform has by default."""
    return 'py -3' if os.name == 'nt' else 'python3'


def render_block(cfg: dict, companion: Path) -> str:
    """Our AGENTS.md block, markers included, LF line endings. `{200}` .. `{900}` in the
    template become the folder names, `{companion}`, `{python}` and `{language}` their values."""
    values = {name.split('-', 1)[0]: name for name in config.FOLDERS}
    values.update(companion=companion.relative_to(config.vault_path(cfg)).as_posix(),
                  python=launcher(), language=cfg['language'])
    text = (TEMPLATES / 'agents-block.md').read_text(encoding='utf-8')
    text = re.sub(r'\{(\w+)\}', lambda match: values.get(match.group(1), match.group(0)), text)
    return f'{BLOCK_START}\n{text.strip()}\n{BLOCK_END}'


def merge_block(text: str, block: str) -> str:
    """`text` with `block` in place of our old block, or after everything else when there is
    none. Nothing outside our markers changes, and the block takes the file's own line ending.
    Raises ValueError when our markers are broken."""
    newline = '\r\n' if '\r\n' in text else '\n'
    block = block.replace('\n', newline)
    starts, ends = text.count(BLOCK_START), text.count(BLOCK_END)
    if starts != ends or starts > 1 or (starts and text.index(BLOCK_END) < text.index(BLOCK_START)):
        raise ValueError(f'its {BLOCK_START} ... {BLOCK_END} markers are broken or repeated; '
                         'fix them by hand (keep one pair, or none)')
    if starts:
        return text[:text.index(BLOCK_START)] + block + text[text.index(BLOCK_END) + len(BLOCK_END):]
    body = text.rstrip('\r\n')
    return (body + newline * 2 if body else '') + block + newline


def plan_layout(vault: Path, cfg: dict, companion: Path) -> Layout:
    """Everything the layout step will write, checked first: raises ValueError, writes nothing."""
    layout = Layout()
    for name in config.FOLDERS:
        folder = vault / name
        if folder.exists() and not folder.is_dir():
            raise ValueError(f'{folder} exists and is not a folder; nothing was written')
        if not folder.exists():
            layout.folders.append(folder)
        if not (folder / FOLDER_NOTE).exists():
            layout.files[folder / FOLDER_NOTE] = folder_template(name).read_bytes()
    for name in COMPANION_FILES:
        path = companion / name
        if path.exists():
            layout.kept.append(path)
        else:
            layout.files[path] = (TEMPLATES / name).read_bytes()
    evidence = vault / '.brain' / EVIDENCE
    if evidence.exists():
        layout.kept.append(evidence)
    else:
        layout.files[evidence] = (TEMPLATES / EVIDENCE).read_bytes()
    agents = vault / 'AGENTS.md'
    if agents.is_symlink():
        layout.agents_status = 'skipped, it is a link (add the route table by hand)'
        return layout
    try:
        current = agents.read_bytes() if agents.exists() else b''
        text = current.decode('utf-8')
        merged = merge_block(text, render_block(cfg, companion)).encode('utf-8')
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f'{agents}: {exc}; nothing was written') from None
    if merged != current:
        layout.agents = (agents, merged)
        layout.agents_status = f"NeoMyelin block {'updated' if BLOCK_START in text else 'added'}"
    return layout


def apply_layout(layout: Layout, vault: Path) -> list[str]:
    def names(paths: list[Path]) -> str:
        return ', '.join(path.relative_to(vault).as_posix() for path in paths)

    for folder in layout.folders:
        folder.mkdir(parents=True, exist_ok=True)
    created = [path for path, data in layout.files.items()
               if not path.exists() and _write_if_changed(path, data)]
    report = [f'  folders: created {names(layout.folders)}' if layout.folders
              else f'  folders: all {len(config.FOLDERS)} present']
    report.append(f'  files created: {names(created)}' if created else '  files created: none')
    if layout.kept:
        report.append(f'  files kept as they are: {names(layout.kept)}')
    if layout.agents is not None:
        _write_if_changed(*layout.agents)
    report.append(f'  AGENTS.md: {layout.agents_status}')
    return report


def install(vault: Path, given_config: Path | None, home: Path, scope: str,
            statusline: bool = False, skills: bool = True, instructions: bool = True,
            adopt: bool = False) -> list[str]:
    _check_brain_folder(vault)
    _check_engine(vault)
    cfg = _config(vault, given_config)
    companion = config.companion_dir(cfg)
    if companion is None:
        raise ValueError('several folders could be the companion folder; '
                         'pass --config with "companion_dir" set')
    python = sys.executable
    plans, skipped = [], []
    for name in cfg['harnesses']:
        cli = render_hooks.CLIS[name]
        if shutil.which(cli) is None and not (statusline and name == 'claude'):
            skipped.append(f'  {name} hooks: skipped, the `{cli}` command is not on PATH')
            continue
        try:
            plans.append(render_hooks.plan(name, vault, python, scope, home,
                                           statusline=statusline and name == 'claude'))  # raises before any write
        except render_hooks.Unsupported as exc:
            skipped.append(f'  {name} hooks: skipped, {exc}')
    layout = plan_layout(vault, cfg, companion)  # raises before any write
    targets, instructions_status = [], ''
    if not instructions:
        instructions_status = '  instructions: skipped (--no-instructions)'
    elif scope != 'user':
        instructions_status = '  instructions: skipped (--scope project writes no user-level file)'
    else:
        present = render_instructions.installed(cfg)
        targets = render_instructions.plan(cfg, present, home)  # raises before any write
        absent = [name for name in cfg['harnesses'] if name not in present]
        if absent:
            skipped.append(f"  instructions: skipped for {', '.join(absent)} (command not on PATH)")
    agy_skills = (skills_hub.plan_agy(vault, home)  # raises before any write
                  if skills and scope == 'user' and 'agy' in cfg['harnesses'] else None)

    report = [f'NeoMyelin -> {vault}']
    # The config goes first: it is what marks .brain/ as ours on the next run.
    config_file = vault / '.brain' / 'config.json'
    changed = _write_if_changed(config_file, config.dumps(cfg).encode('utf-8'))
    report.append(f"  config: {'written' if changed else 'unchanged'} {config_file}")
    if all(cfg[key] == value for key, value in NEUTRAL.items()):
        report.append('  identity: not set, so the session hook says "the assistant" and "the user". '
                      'Set yours: copy templates/config.example.json, put your names in, '
                      'run install.py again with --config <that file>.')
    written = [str(rel) for rel, data in _brain_files(vault).items()
               if _write_if_changed(vault / '.brain' / rel, data)]
    report.append(f'  brain: {len(written)} files written' + (f" ({', '.join(written)})" if written else ''))
    changed = _write_if_changed(vault / 'brain.py', ENGINE.read_bytes())
    report.append(f"  engine: brain.py {'written' if changed else 'unchanged'} (receipts, tasks, daily view)")
    report += apply_layout(layout, vault)
    for planned in plans:
        status, backup, shims = render_hooks.apply(planned)
        report.append(f'  {planned.harness} hooks: {status} {planned.path}'
                      + (f' (backup: {backup})' if backup else ''))
        report += [f'    shim written: {path}' for path in shims]
    report += render_instructions.apply(targets) + ([instructions_status] if instructions_status else [])
    if skills and scope == 'project':
        # Project scope exists for a machine whose user-level files belong to another brain;
        # its skill folders are theirs too.
        report.append('  skills: not linked (--scope project leaves user-level skill folders alone)')
    elif skills:
        if adopt:
            report += skills_hub.adopt(vault, home, cfg['harnesses'])
        report += skills_hub.link(vault, home, cfg['harnesses'])
        if agy_skills is not None:
            report += skills_hub.register_agy(agy_skills, vault)
        if not adopt:
            report += skills_hub.describe_own(vault, home, cfg['harnesses'])
    else:
        report.append('  skills: not linked (--no-skills)')
    if statusline:
        if any(plan.harness == 'claude' for plan in plans):
            report.append('  Claude status line: enabled')
        else:
            report.append('  Claude status line: skipped (Claude is not in configured harnesses)')
    return report + skipped


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--vault', type=Path, required=True,
                        help='The vault folder: an empty one, or one that already holds notes.')
    parser.add_argument('--config', type=Path, default=None,
                        help='Your config JSON: user_name, assistant_name, language, harnesses. Copy '
                             'templates/config.example.json and put your own names in (the example '
                             'names are refused). Needed once; without it and without an installed '
                             'config the identity stays neutral.')
    parser.add_argument('--home', type=Path, default=None, help='Replaces the user home for every harness file.')
    parser.add_argument('--scope', choices=render_hooks.SCOPES, default='user',
                        help="user: the harnesses' user-level hook files (default); project: the vault's own.")
    parser.add_argument('--statusline', action='store_true', help='Enable the Claude Code status line.')
    parser.add_argument('--no-skills', action='store_true',
                        help="Do not link the hub's skills (.brain/skills/) into the harnesses.")
    parser.add_argument('--adopt-skills', action='store_true',
                        help="Move the user's own skills from each configured harness's skill folder into "
                             '.brain/skills/ and link them back (backed up first; uninstall.py puts them back).')
    parser.add_argument('--no-instructions', action='store_true',
                        help="Do not write the instruction block into the harnesses' user-level instruction files.")
    args = parser.parse_args(argv)
    if args.adopt_skills and (args.no_skills or args.scope == 'project'):
        parser.error('--adopt-skills works on the user-level skill folders; '
                     'it cannot be combined with --no-skills or --scope project')
    vault = args.vault.expanduser().resolve()
    home = (args.home or Path.home()).expanduser().resolve()
    try:
        if not vault.is_dir():
            raise ValueError(f'{vault} is not a folder')
        report = install(vault, args.config, home, args.scope, args.statusline, not args.no_skills,
                         not args.no_instructions, args.adopt_skills)
    except (ValueError, OSError) as exc:
        print(f'install.py: error: {exc}', file=sys.stderr)
        return 1
    print('\n'.join(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
