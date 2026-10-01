#!/usr/bin/env python3
"""Register NeoMyelin's hooks in a harness's hook file: Claude Code, Codex or Antigravity (agy).

    python .brain/scripts/render_hooks.py --harness claude|codex|agy [--scope user|project] [--home DIR] [--check]

| harness | user scope (default)               | project scope                   |
|---------|------------------------------------|---------------------------------|
| claude  | <home>/.claude/settings.json       | <vault>/.claude/settings.json   |
| codex   | <home>/.codex/hooks.json           | <vault>/.codex/hooks.json       |
| agy     | <home>/.gemini/config/hooks.json   | <vault>/.agents/hooks.json      |

Hooks (HOOKS below): Claude and Codex get session context (SessionStart), per-prompt recall and
the receipt bookkeeping (UserPromptSubmit), edit counting (PostToolUse), the git and heredoc
gates (PreToolUse) and the receipt reminder (Stop). agy gets session context and the queued
receipt reminder (PreInvocation) and idle-turn counting (Stop); it has no prompt-submit event
this layer can use (docs/harnesses.md).

User scope makes memory load in every folder. Project scope is for a machine whose user-level
hooks already belong to another brain; Claude's project file is settings.json (settings.local.json
stays the user's), and in every project file our entries sit next to whatever else it holds.

Windows: Codex starts a hook command without a shell and hands it to cmd.exe unquoted, so a path
with a space breaks at the space (measured; docs/harnesses.md). Codex and agy therefore run a
generated `.cmd` shim in <vault>/.brain/hooks/ that holds the quoted Python path; a vault path
with whitespace raises Unsupported (install.py skips that harness with one line). agy's
PreInvocation needs its own output shape: .brain/hooks/agy_hook.py.

An entry is ours when its command ends with one of our hook forms from THIS vault's `.brain/`:
a script with exactly the arguments we register (`prompt_recall.py --prompt-submit`), or one of
our shims.
Everything else in the file (other hooks, other scripts in the same folders, the same script run
by hand or by another brain without our arguments, other vaults' entries, every other key) is kept;
agy entries live under the named-hook key "neomyelin". An unchanged file is never rewritten; a changed one is first copied next to itself as `<name>.neomyelin-<stamp>.bak`, and
keeps its line endings and BOM. Claude's settings also get CLAUDE_ENV in `env`, and nothing else.
"""
from __future__ import annotations

import argparse
import codecs
from dataclasses import dataclass, field
import datetime as dt
import functools
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402


@dataclass(frozen=True)
class Hook:
    event: str
    target: str                  # the file under .brain/ it runs
    args: tuple[str, ...]
    timeout: int                 # seconds
    matcher: str | None = None   # Claude/Codex tool-name matcher (PostToolUse, PreToolUse)
    name: str = ''               # shim suffix when an event runs several of our hooks
    status: str = ''             # Codex statusMessage


EDIT_TOOLS = 'Edit|Write|MultiEdit|NotebookEdit|apply_patch'  # Claude's edit tools and Codex's apply_patch


def _receipt(harness: str, event: str, timeout: int = 10, matcher: str | None = None) -> Hook:
    return Hook(event, 'scripts/receipt_gate.py', ('--harness', harness, '--event', event), timeout,
                matcher, 'receipt')


def _claude_codex(harness: str, shell_matcher: str | None, bash_matcher: str | None) -> tuple[Hook, ...]:
    return (
        Hook('SessionStart', 'scripts/memory_context.py', ('--session-start',), 15,
             status='Loading NeoMyelin memory'),
        Hook('UserPromptSubmit', 'scripts/prompt_recall.py', ('--prompt-submit',), 15, name='recall',
             status='Checking NeoMyelin memory'),
        _receipt(harness, 'UserPromptSubmit'),
        _receipt(harness, 'PostToolUse', matcher=EDIT_TOOLS),
        Hook('PreToolUse', 'scripts/git_gate.py', ('--pre-tool-use',), 10, shell_matcher, 'git'),
        Hook('PreToolUse', 'scripts/heredoc_gate.py', ('--pre-tool-use',), 10, bash_matcher, 'heredoc'),
        _receipt(harness, 'Stop'),
    )


# Codex's shell tool name is not measured here, so its gates get every tool call and read the
# shell `command` themselves (docs/harnesses.md).
HOOKS = {
    'claude': _claude_codex('claude', 'Bash|PowerShell', 'Bash'),
    'codex': _claude_codex('codex', None, None),
    'agy': (
        Hook('PreInvocation', 'hooks/agy_hook.py', (), 15),
        _receipt('agy', 'Stop'),
    ),
}
TARGETS = tuple(HOOKS)
SCOPES = ('user', 'project')
CLIS = {'claude': 'claude', 'codex': 'codex', 'agy': 'agy'}
FILES = {
    'user': {'claude': '.claude/settings.json', 'codex': '.codex/hooks.json', 'agy': '.gemini/config/hooks.json'},
    'project': {'claude': '.claude/settings.json', 'codex': '.codex/hooks.json', 'agy': '.agents/hooks.json'},
}
AGY_KEY = 'neomyelin'
# The only key written into Claude's settings `env`: Python runs in UTF-8 mode in every command
# Claude starts (on Windows it would use the ANSI code page and garble non-English text).
CLAUDE_ENV = {'PYTHONUTF8': '1'}
SHIMMED = ('codex', 'agy')  # on Windows these run a generated .cmd shim
# What a shim prints when the Python it was written with is gone: a visible warning, not silence,
# once per session (the session context hook). The other shims exit quietly then: a gate or a
# Stop hook must not print another event's output shape.
SHIM_WARNING = {
    ('codex', 'SessionStart'): '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}',
    ('agy', 'PreInvocation'): '{"injectSteps":[{"ephemeralMessage":"%s"}]}',
}
SHIM_WARNING_TEXT = ('[Memory warning] NeoMyelin: the Python this hook was installed with is gone; '
                     'run install.py again.')


class Unsupported(ValueError):
    """This harness cannot be registered for this vault on this machine; install.py skips it."""


def _kebab(event: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '-', event).lower()


def shim_name(harness: str, hook: Hook) -> str:
    return f'{harness}-{_kebab(hook.event)}' + (f'-{hook.name}' if hook.name else '') + '.cmd'


def settings_path(harness: str, scope: str, home: Path, vault: Path) -> Path:
    if harness not in HOOKS:
        raise ValueError(f'no hook target for {harness}')
    if scope not in SCOPES:
        raise ValueError(f'unknown scope {scope}')
    if harness == 'agy' and scope == 'user':
        snap = config.agy_snap()
        if snap:
            # Measured in WSL, 2026-10-01: the snap loads the hook file but its confinement hides
            # the system Python ("/usr/bin/python3: not found"), so every hook would fail.
            raise Unsupported(f'agy is the {snap} snap, which cannot run hooks that need the system '
                              'Python; install agy outside snap for hooks (the instruction block '
                              'still works)')
        return config.agy_dir(home) / 'config' / 'hooks.json'
    return (home if scope == 'user' else vault) / FILES[scope][harness]


def _quote(part: str) -> str:
    if os.name != 'nt':
        return shlex.quote(part)
    # Claude Code runs hook commands through Git Bash on Windows; double quotes also hold in cmd.
    if '"' in part:
        raise ValueError(f'path contains a double quote: {part}')
    return f'"{part}"'


def command(python: str, script: Path, *args: str) -> str:
    parts = [Path(python).as_posix(), script.as_posix()]
    return ' '.join([_quote(part) for part in parts] + list(args))


def _shim_path(vault: Path, harness: str, hook: Hook) -> Path:
    return vault / '.brain' / 'hooks' / shim_name(harness, hook)


def _shim_text(harness: str, python: str, hook: Hook) -> str:
    py = python.replace('%', '%%')  # a batch file expands %...% even inside quotes
    script = hook.target.replace('/', '\\')
    run = f'"{py}" "%~dp0..\\{script}"' + ''.join(f' {arg}' for arg in hook.args)
    warning = SHIM_WARNING.get((harness, hook.event))
    lines = [
        '@echo off',
        'rem NeoMyelin hook shim, written by .brain/scripts/render_hooks.py (install.py); a rerun overwrites it.',
        f'rem The {harness} hook command is this file (a path without spaces); it starts Python on the hook input.',
        'setlocal',
        'set "PYTHONUTF8=1"',
        'set "PYTHONIOENCODING=utf-8"',
        f'if exist "{py}" goto run',
        *(['echo ' + warning % SHIM_WARNING_TEXT] if warning else []),
        'exit /b 0',
        ':run',
        run,
        'exit /b %errorlevel%',
    ]
    return '\r\n'.join(lines) + '\r\n'


def _batch_bytes(text: str, python: str) -> bytes:
    """cmd.exe reads a batch file in the console's OEM code page, not in UTF-8."""
    try:
        return text.encode('ascii')
    except UnicodeEncodeError:
        pass
    import ctypes  # Windows only; reached only for a non-ASCII Python path
    codepage = f'cp{ctypes.windll.kernel32.GetOEMCP()}'
    try:
        return text.encode(codepage)
    except (UnicodeEncodeError, LookupError):
        raise Unsupported(f'the Python path {python} cannot be written into a .cmd file in code page '
                          f'{codepage}; run install.py with a Python whose path has only plain letters') from None


def shims(harness: str, vault: Path, python: str) -> dict[Path, bytes]:
    """The .cmd files this harness's commands run (Windows, Codex and agy); empty elsewhere."""
    if os.name != 'nt' or harness not in SHIMMED:
        return {}
    return {_shim_path(vault, harness, hook): _batch_bytes(_shim_text(harness, python, hook), python)
            for hook in HOOKS[harness]}


def entries(harness: str, vault: Path, python: str) -> dict[str, list]:
    """event -> our groups for this harness, in order (agy: handlers, its file has no groups)."""
    found: dict[str, list] = {}
    for hook in HOOKS[harness]:
        script = vault / '.brain' / hook.target
        if harness == 'claude':
            text = command(python, script, *hook.args)
        elif os.name == 'nt':
            text = _shim_path(vault, harness, hook).as_posix()
            if re.search(r'\s', text):
                raise Unsupported(
                    f'on Windows its hook command is a .cmd path that must have no spaces (Codex hands it '
                    f'to cmd.exe unquoted), and this vault path has one: {vault}. Move the vault to a path '
                    f'without spaces to use {harness}.')
        else:
            text = shlex.join([Path(python).as_posix(), script.as_posix(), *hook.args])
        handler = {'type': 'command', 'command': text, 'timeout': hook.timeout}
        if harness == 'codex' and hook.status:
            handler['statusMessage'] = hook.status
        if harness == 'agy':
            group = handler
        else:
            group = {'matcher': hook.matcher, 'hooks': [handler]} if hook.matcher else {'hooks': [handler]}
        found.setdefault(hook.event, []).append(group)
    return found


def _forms(harness: str, hook: Hook) -> list[str]:
    """The command endings (regex, casefolded) of one of our hooks: the script with exactly our
    arguments, and for a shimmed harness its shim."""
    # `memory_context_old.py`, `x.py.bak` or the script without our arguments is someone else's.
    forms = [re.escape(hook.target.casefold()) + r'''["']?'''
             + ''.join(r'\s+' + re.escape(arg.casefold()) for arg in hook.args)]
    if harness in SHIMMED:
        forms.append(re.escape(f'hooks/{shim_name(harness, hook)}'.casefold()) + r'''["']?''')
    return forms


def _folder(vault: Path) -> str:
    return re.escape((vault.as_posix().rstrip('/') + '/.brain/').casefold())


@functools.lru_cache(maxsize=8)
def _our_forms(vault: Path) -> re.Pattern:
    """Every command ending we write: a script with exactly our arguments, or a shim."""
    forms = {form for harness, hooks in HOOKS.items() for hook in hooks for form in _forms(harness, hook)}
    return re.compile(_folder(vault) + '(?:' + '|'.join(sorted(forms)) + r')\s*$')


def unregistered(harness: str, settings: object, vault: Path) -> list[str]:
    """doctor.py: our hooks for `harness` that its parsed hook file holds no entry of this vault
    for, as `<Event> <script>`; [] when every one is there."""
    hooks = settings.get(AGY_KEY if harness == 'agy' else 'hooks') if isinstance(settings, dict) else None
    hooks = hooks if isinstance(hooks, dict) else {}
    missing = []
    for hook in HOOKS[harness]:
        pattern = re.compile(_folder(vault) + '(?:' + '|'.join(_forms(harness, hook)) + r')\s*$')
        groups = hooks.get(hook.event)
        groups = groups if isinstance(groups, list) else []
        handlers = groups if harness == 'agy' else [
            entry for group in groups if isinstance(group, dict) and isinstance(group.get('hooks'), list)
            for entry in group['hooks']]
        if not any(isinstance(entry, dict)
                   and pattern.search(str(entry.get('command', '')).replace('\\', '/').casefold())
                   for entry in handlers):
            missing.append(f'{hook.event} {Path(hook.target).name}')
    return missing


def _ours(entry: object, vault: Path) -> bool:
    if not isinstance(entry, dict):
        return False
    normalized = str(entry.get('command', '')).replace('\\', '/').casefold()
    return _our_forms(vault).search(normalized) is not None


def _holds_ours(harness: str, group: object, vault: Path) -> bool:
    if harness == 'agy':  # named hooks: event -> list of handlers, no groups
        return _ours(group, vault)
    return (isinstance(group, dict) and isinstance(group.get('hooks'), list)
            and any(_ours(entry, vault) for entry in group['hooks']))


def merge(harness: str, settings: object, vault: Path, python: str, statusline: bool = False) -> dict:
    """The file's data with exactly our entries for this vault; everything else as it was."""
    if not isinstance(settings, dict):
        raise ValueError('the file root is not a JSON object')
    key = AGY_KEY if harness == 'agy' else 'hooks'
    hooks = settings.get(key, {})
    if not isinstance(hooks, dict) or any(not isinstance(groups, list) for groups in hooks.values()):
        raise ValueError(f'"{key}" is not an object of event lists')
    wanted = entries(harness, vault, python)
    found = {event: [group for group in groups if _holds_ours(harness, group, vault)]
             for event, groups in hooks.items()}
    extra_env = CLAUDE_ENV if harness == 'claude' else {}
    env = settings.get('env', {})
    if harness == 'claude' and not isinstance(env, dict):
        raise ValueError('"env" must be an object')
    env_ready = not extra_env or all(env.get(key) == value for key, value in extra_env.items())
    status_ready = not statusline or settings.get('statusLine') == _statusline(python, vault)
    if {event: groups for event, groups in found.items() if groups} == wanted and env_ready and status_ready:
        return settings  # already registered: keep the user's order untouched
    cleaned = _without_ours(harness, hooks, vault)
    for event, groups in wanted.items():
        cleaned.setdefault(event, []).extend(groups)
    result = {**settings, key: cleaned}
    if extra_env:
        result['env'] = {**env, **extra_env}
    if statusline:
        result['statusLine'] = _statusline(python, vault)
    return result


def _without_ours(harness: str, hooks: dict, vault: Path) -> dict[str, list]:
    """`hooks` minus this vault's entries; an event left empty is dropped."""
    cleaned: dict[str, list] = {}
    for event, groups in hooks.items():
        kept = []
        for group in groups:
            if harness == 'agy':
                if not _ours(group, vault):
                    kept.append(group)
                continue
            if isinstance(group, dict) and isinstance(group.get('hooks'), list):
                remaining = [entry for entry in group['hooks'] if not _ours(entry, vault)]
                if not remaining:
                    continue
                if len(remaining) != len(group['hooks']):
                    group = {**group, 'hooks': remaining}
            kept.append(group)
        if kept:
            cleaned[event] = kept
    return cleaned


def unregister(harness: str, settings: object, vault: Path) -> dict:
    """The file's data without this vault's entries (and its status line); the rest as it was.

    `env.PYTHONUTF8` stays: other tools may rely on it and it changes nothing else.
    """
    if not isinstance(settings, dict):
        raise ValueError('the file root is not a JSON object')
    key = AGY_KEY if harness == 'agy' else 'hooks'
    hooks = settings.get(key, {})
    if not isinstance(hooks, dict) or any(not isinstance(groups, list) for groups in hooks.values()):
        raise ValueError(f'"{key}" is not an object of event lists')
    result = dict(settings)
    cleaned = _without_ours(harness, hooks, vault)
    if cleaned != hooks:
        if cleaned:
            result[key] = cleaned
        else:
            result.pop(key, None)
    status = settings.get('statusLine')
    script = (vault / '.brain' / 'scripts' / 'statusline.py').as_posix().casefold()
    if (harness == 'claude' and isinstance(status, dict)
            and script in str(status.get('command', '')).replace('\\', '/').casefold()):
        result.pop('statusLine')
    return result


def _statusline(python: str, vault: Path) -> dict:
    script = vault / '.brain' / 'scripts' / 'statusline.py'
    return {'type': 'command', 'command': command(python, script), 'padding': 0}


def read_settings(path: Path) -> tuple[dict, bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    try:
        return json.loads(raw.decode('utf-8-sig')), raw
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f'{path} is not valid JSON ({exc}); fix it first, nothing was written') from None


def _backup(path: Path, raw: bytes) -> Path:
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    for counter in range(1000):
        suffix = f'-{counter}' if counter else ''
        candidate = path.with_name(f'{path.name}.neomyelin-{stamp}{suffix}.bak')
        try:
            with candidate.open('xb') as handle:
                handle.write(raw)
            return candidate
        except FileExistsError:
            continue
    raise OSError(f'no free backup name next to {path}')


def _replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.neomyelin-', suffix=path.suffix)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_settings(path: Path, payload: dict, old: bytes | None) -> Path | None:
    """Write payload; back the old bytes up first. Returns the backup path, if any."""
    newline = '\r\n' if old and b'\r\n' in old else '\n'
    bom = codecs.BOM_UTF8 if old and old.startswith(codecs.BOM_UTF8) else b''
    data = bom + (json.dumps(payload, indent=2, ensure_ascii=False) + '\n').replace('\n', newline).encode('utf-8')
    backup = _backup(path, old) if old is not None else None
    _replace(path, data)
    return backup


@dataclass
class Plan:
    harness: str
    path: Path            # the harness's hook file
    current: dict
    desired: dict
    raw: bytes | None     # its bytes now; None when it does not exist
    shims: dict[Path, bytes] = field(default_factory=dict)

    def stale_shims(self) -> list[Path]:
        return [path for path, data in self.shims.items()
                if not (path.is_file() and path.read_bytes() == data)]

    def file_unchanged(self) -> bool:
        return self.raw is not None and self.desired == self.current

    def registered(self) -> bool:
        return self.file_unchanged() and not self.stale_shims()


def plan(harness: str, vault: Path, python: str, scope: str, home: Path,
         statusline: bool = False) -> Plan:
    """What registering would write; raises ValueError, writes nothing."""
    path = settings_path(harness, scope, home, vault)
    current, raw = read_settings(path)
    return Plan(harness, path, current, merge(harness, current, vault, python, statusline), raw,
                shims(harness, vault, python))


def apply(planned: Plan) -> tuple[str, Path | None, list[Path]]:
    """Write a plan: (hook file 'unchanged' | 'created' | 'updated', backup path, shims written)."""
    written = []
    for path in planned.stale_shims():
        _replace(path, planned.shims[path])
        written.append(path)
    if planned.file_unchanged():
        return 'unchanged', None, written
    backup = write_settings(planned.path, planned.desired, planned.raw)
    return ('created' if planned.raw is None else 'updated'), backup, written


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--harness', choices=TARGETS, default='claude')
    parser.add_argument('--scope', choices=SCOPES, default='user')
    parser.add_argument('--home', type=Path, default=None, help='Replaces the user home (tests).')
    parser.add_argument('--check', action='store_true', help='Write nothing; exit 1 if not registered.')
    parser.add_argument('--statusline', action='store_true', help='Enable Claude Code status line.')
    args = parser.parse_args(argv)
    try:
        vault = config.vault_path(config.load())
        home = (args.home or Path.home()).expanduser().resolve()
        planned = plan(args.harness, vault, sys.executable, args.scope, home,
                       statusline=args.statusline and args.harness == 'claude')
        if args.check:
            ok = planned.registered()
            print(f"{'OK' if ok else 'MISSING'}  {planned.path}")
            return 0 if ok else 1
        status, backup, written = apply(planned)
    except (ValueError, OSError) as exc:
        print(f'render_hooks: {exc}', file=sys.stderr)
        return 1
    print(f'{status}  {planned.path}' + (f'  (backup: {backup})' if backup else ''))
    for path in written:
        print(f'written  {path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
