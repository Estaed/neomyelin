#!/usr/bin/env python3
"""PreToolUse gate: stop once before touching a folder another live session is working in.

    hook: python .brain/scripts/session_gate.py --pre-tool-use   (hook JSON on stdin)

The incident it prevents: one session rewrote a task another live session was working on without
asking, and took a folder that session had deleted on purpose for data loss. The prompt hook had
announced the other session; the rule was known and still skipped at the moment of the edit.

The source is prompt_recall.py's session markers (folder, topic, time of the last prompt). When
the target of a tool call lies in a folder where another session prompted within the last 45
minutes (prompt_recall.PARALLEL_STALE_SECONDS), and that folder is not this session's own
workspace (the same folder, or one holding the other), the first touch is denied: ask that
session first, here is its topic. A second attempt on the same folder in the same session passes:
it asked, or the change is its own to make. The vault root, the home folder and filesystem roots
are shared ground: a session opened there neither locks them nor owns every folder below them
(the hooks are user-level). A task file has its own guard (task_update.py).

Targets:
- Edit, Write and MultiEdit by `file_path`, NotebookEdit by `notebook_path`.
- A patch (`*** Begin Patch`: apply_patch, or a shell command carrying one) by its
  `*** Add/Update/Delete File:` and `*** Move to:` lines, resolved against the call's `workdir`
  when it has one, else `cwd`.
- Any call whose input carries a shell `command` (a string or an argv list; Codex's gates get
  every call), when the command writes (WRITING: a redirect, rm/mv/cp/tee, sed -i, a writing git
  subcommand, PowerShell's *-Item/*-Content) and names the folder: as written, `x:/` also as
  `/x/` and back, a folder under home also as `~/` or `$HOME/`. A writing command that carries a
  `workdir` (Codex's shell tools take one) also counts by that folder, since it runs there.
  Reading commands (`cat`, `ls`, `git status`) pass.

The gate never blocks on its own error: a broken marker is skipped; a missing state folder,
a bad payload or an unreadable config exits 0 with no decision. Registered by render_hooks.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
from git_gate import command_of  # noqa: E402

ACK_DIR = SCRIPT_DIR / '.state' / 'session_gate'
FILE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path',
              'NotebookEdit': 'notebook_path'}
PATCH_START = '*** Begin Patch'
PATCH_FILE = re.compile(r'^[ \t]*\*\*\* (?:(?:Add|Update|Delete) File|Move to):[ \t]*(.+?)[ \t\r]*$', re.M)
# A writing shell command. `git -C "<a path with spaces>"` is read as one value.
WRITING = re.compile(
    r"sed\s+-i|>>?|\brm\s|\bmv\s|\bcp\s|\btee\b"
    r"|\bgit(\s+-[Cc]\s+(\"[^\"]*\"|'[^']*'|\S+))*\s+(commit|add|checkout|reset|rm|mv|stash|restore|clean|push)\b"
    r"|Set-Content|Add-Content|Out-File|Remove-Item|Move-Item|Copy-Item|New-Item", re.I)
# Swallowing errors or output is not writing: `2>/dev/null`, `2>&1`, `>$null`, `> nul`.
SWALLOWED = re.compile(r"\d?>>?\s*(&\d|/dev/null|\$null|nul\b)", re.I)
# A folder named in a command counts only where its name ends: `/work/app` is not `/work/app-api`.
NAME_GOES_ON = re.compile(r'[\w.-]')


def _norm(path: str, base: str = '') -> str:
    path = os.path.expanduser(path.strip())
    if base and not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # different drives, or one relative and one absolute
        return False


def _same_workspace(left: str, right: str) -> bool:
    return bool(left and right) and (_inside(left, right) or _inside(right, left))


def _shared(folder: str, vault: str, home: str) -> bool:
    """The vault root, the home folder and a filesystem root belong to no session."""
    return folder in (vault, home) or os.path.dirname(folder) == folder


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def targets(payload: dict) -> tuple[list[str], str]:
    """(the paths the call writes, its shell command when the command writes, else '')."""
    tool = payload.get('tool_name')
    tool_input = payload.get('tool_input')
    if not isinstance(tool_input, dict):
        return [], ''
    cwd = payload.get('cwd') if isinstance(payload.get('cwd'), str) else ''
    if tool in FILE_TOOLS:
        path = tool_input.get(FILE_TOOLS[tool])
        return ([_norm(path, cwd)] if isinstance(path, str) and path.strip() else []), ''
    # A Codex shell call may run in a `workdir` of its own: its relative names and writes land there.
    workdir = tool_input.get('workdir')
    workdir = _norm(workdir, cwd) if isinstance(workdir, str) and workdir.strip() else ''
    paths = [_norm(name, workdir or cwd) for text in _strings(tool_input) if PATCH_START in text
             for name in PATCH_FILE.findall(text)]
    command = command_of(tool_input)
    if not command or not WRITING.search(SWALLOWED.sub(' ', command)):
        return paths, ''
    return paths + ([workdir] if workdir else []), command


def _named_in(command: str, folder: str, home: str) -> bool:
    """The command names the folder, in any of the forms a shell may write it."""
    text = command.replace('\\', '/').lower()
    wanted = folder.replace('\\', '/').lower().rstrip('/')
    forms = {wanted}
    if re.match(r'[a-z]:/', wanted):
        forms.add(f'/{wanted[0]}{wanted[2:]}')       # x:/work -> /x/work (Git Bash, MSYS)
    elif re.match(r'/[a-z]/', wanted):
        forms.add(f'{wanted[1]}:{wanted[2:]}')       # /x/work -> x:/work
    home = home.replace('\\', '/').lower().rstrip('/')
    if home and wanted.startswith(home + '/'):
        forms |= {'~' + wanted[len(home):], '$home' + wanted[len(home):]}
    for form in forms:
        start = text.find(form)
        while start != -1:
            end = start + len(form)
            if end == len(text) or not NAME_GOES_ON.match(text[end]):
                return True
            start = text.find(form, start + 1)
    return False


def owners(payload: dict, now: float | None = None) -> list[dict]:
    """The other live sessions whose folder this call writes into, the most recent first."""
    session = payload.get('session_id')
    if not isinstance(session, str) or not session:
        return []
    paths, command = targets(payload)
    if not paths and not command:
        return []
    # Imported here: this gate runs on every tool call, and only a writing one needs the markers
    # (prompt_recall imports recall, about 40 ms).
    import prompt_recall  # noqa: PLC0415

    now = time.time() if now is None else now
    vault = _norm(str(config.vault_path(config.load())))
    home = _norm('~')
    cwd = prompt_recall.normal_cwd(payload.get('cwd'))
    own = '' if not cwd or _shared(cwd, vault, home) else cwd
    mine = prompt_recall._marker(session).name
    found = []
    for marker in prompt_recall.STATE_DIR.glob('*.json'):
        if marker.name == mine or not prompt_recall.MARKER_RE.fullmatch(marker.name):
            continue
        try:
            data = json.loads(marker.read_text(encoding='utf-8'))
            updated = float(data.get('updated_at'))
        except (OSError, TypeError, ValueError, AttributeError):
            continue
        folder = prompt_recall.normal_cwd(data.get('cwd'))
        if (updated < now - prompt_recall.PARALLEL_STALE_SECONDS or not folder
                or _shared(folder, vault, home) or _same_workspace(own, folder)):
            continue
        if any(_inside(path, folder) for path in paths) or (command and _named_in(command, folder, home)):
            topic = ' '.join(str(data.get('topic') or 'no topic').split())[:prompt_recall.TOPIC_CHARS]
            found.append({'folder': folder, 'topic': topic, 'updated_at': updated,
                          'minutes': max(0, int((now - updated) // 60))})
    found.sort(key=lambda owner: -owner['updated_at'])
    return found


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        temporary.write_text(text, encoding='utf-8', newline='\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _forget_old(stale_seconds: float) -> None:
    cutoff = time.time() - stale_seconds
    for old in ACK_DIR.glob('*.json'):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            continue


def record(session: str, folders: list[str]) -> list[str]:
    """The folders this session has not been stopped for yet; records them, so the retry passes."""
    import prompt_recall  # noqa: PLC0415 - see owners()

    path = ACK_DIR / f"{hashlib.sha256(session.encode('utf-8')).hexdigest()}.json"
    try:
        seen = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        seen = []
    if not isinstance(seen, list):
        seen = []
    new = list(dict.fromkeys(folder for folder in folders if folder not in seen))
    if new:
        _forget_old(prompt_recall.SESSION_STALE_SECONDS)
        _atomic_write(path, json.dumps(seen + new, ensure_ascii=False))
    return new


def decide(payload: object, now: float | None = None) -> dict | None:
    """The PreToolUse deny answer for a hook payload, or None to let the call through."""
    if not isinstance(payload, dict):
        return None
    found = owners(payload, now)
    new = record(payload['session_id'], [owner['folder'] for owner in found]) if found else []
    if not new:
        return None
    listed = [owner for owner in found if owner['folder'] in new]
    where = '; '.join(f"{owner['folder']} (topic: '{owner['topic']}', active {owner['minutes']} min ago)"
                      for owner in listed)
    who, whom, whose = (('another live session is', 'that session', 'its') if len(listed) == 1
                        else ('other live sessions are', 'those sessions', 'their'))
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"session_gate: {who} working in {where}. Ask {whom} first, before touching {whose} "
            "files (in Claude Code ListAgents finds a session and SendMessage asks it; otherwise "
            "ask the user). If you asked, or this change is yours to make, retry the same call: "
            "this gate stops only once per folder."
        ),
    }}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # exit 2 would make the harness block the tool call
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pre-tool-use", action="store_true", required=True,
                        help="Read the PreToolUse hook JSON on stdin (the registered form).")
    parser.parse_args(argv)
    try:
        answer = decide(json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}"))
    except Exception:  # noqa: BLE001 - a broken gate lets the call through
        return 0
    if answer is not None:
        sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
