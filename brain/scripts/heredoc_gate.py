#!/usr/bin/env python3
"""PreToolUse gate: refuse a shell command whose heredoc body holds a backslash.

    hook: python .brain/scripts/heredoc_gate.py --pre-tool-use   (hook JSON on stdin)

The incident it prevents: an agent's Bash tool turned `\\\\` into one backslash and `\\n` into a
real line break inside heredoc bodies, so Python code, regexes, JSON and Windows paths written
through `cat <<EOF > file` arrived broken on disk. A written rule against it was broken six times
in four days; a rule that fails twice belongs in a tool. The refusal points to the file-writing
tools (Write / Edit / apply_patch) instead.

`<<<` (a here-string) is not a heredoc; `<<-` (tab-stripped) is. PowerShell has no heredoc and is
not checked. The gate never blocks on its own failure (exit 0, no decision). Registered by
render_hooks.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
NOT_A_SHELL_WITH_HEREDOCS = {"PowerShell"}


def backslash_heredoc(command: str) -> str | None:
    """The delimiter of the first heredoc whose body holds a backslash, or None."""
    lines = command.split("\n")
    for number, line in enumerate(lines):
        for match in HEREDOC.finditer(line):
            tabbed, name = match.group(1) == "-", match.group(3)
            body = []
            for following in lines[number + 1:]:
                if (following.lstrip("\t") if tabbed else following) == name:
                    break
                body.append(following)
            if "\\" in "\n".join(body):
                return name
    return None


def command_of(tool_input: object) -> str:
    """The shell command of a tool call; an argv list (Codex may send one) is read element by
    element, so the script of `bash -lc "<script>"` is checked as written."""
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, list):
        return "\n".join(str(part) for part in command)
    return command if isinstance(command, str) else ""


def decide(payload: object) -> dict | None:
    """The PreToolUse deny answer for a hook payload, or None to let the call through."""
    if not isinstance(payload, dict) or payload.get("tool_name") in NOT_A_SHELL_WITH_HEREDOCS:
        return None
    name = backslash_heredoc(command_of(payload.get("tool_input")))
    if name is None:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"heredoc_gate: the `{name}` heredoc body holds a backslash, and the shell tool mangles "
            "it (a double backslash becomes one, \\n becomes a line break). Write the content to a "
            "file with the file-writing tool (Write, Edit or apply_patch), then run that file."
        ),
    }}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # exit 2 would make the harness block the tool call
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pre-tool-use", action="store_true", required=True,
                        help="Read the PreToolUse hook JSON on stdin (the registered form).")
    parser.parse_args(argv)
    try:
        answer = decide(json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}"))
    except Exception:  # noqa: BLE001 - a broken gate lets the command through
        return 0
    if answer is not None:
        sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
