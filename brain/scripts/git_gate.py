#!/usr/bin/env python3
"""PreToolUse gate: refuse git commands that throw away the whole working tree.

    hook: python .brain/scripts/git_gate.py --pre-tool-use   (hook JSON on stdin)

The incident it prevents: two agent sessions were open in the same folder, and one of them undid
its own mistake with `git reset --hard`, which also erased the other session's uncommitted work.
A checkpoint commit does not protect someone else's half-done work. So only the forms that touch
the whole tree are refused (reset --hard, clean -f, checkout -f / -- . / ., restore ., switch
--discard-changes, stash clear, stash without paths); undoing named files stays allowed.

An agent in its own git worktree (the git dir sits under `/worktrees/`) is left alone: nobody
else works there. The gate never blocks on its own failure (exit 0, no decision).

Any tool call whose input carries a shell `command` is checked, so the same gate covers Claude
Code's Bash and PowerShell tools and Codex's shell tool. Registered by render_hooks.py.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys

SEPARATOR = re.compile(r"(?:&&|\|\||;|\||\n)")
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
WHOLE_TREE = {".", ":/", ":", "*", "./"}


def _git_argv(part: str) -> list[str] | None:
    """The arguments after `git` (its own options skipped), or None when the part is not git."""
    try:
        words = shlex.split(part, posix=True)
    except ValueError:
        words = part.split()
    while words and ("=" in words[0] and not words[0].startswith("-")):
        words = words[1:]  # VAR=value prefix
    if not words or words[0].rsplit("/", 1)[-1].lower() not in {"git", "git.exe"}:
        return None
    args = words[1:]
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in GIT_OPTIONS_WITH_VALUE else args[1:]
    return args


def danger(command: str) -> str | None:
    """The first whole-tree git form in the command, or None."""
    for part in SEPARATOR.split(command):
        argv = _git_argv(part.strip())
        if not argv:
            continue
        sub, rest = argv[0], argv[1:]
        paths = rest[rest.index("--") + 1:] if "--" in rest else [a for a in rest if not a.startswith("-")]
        if sub == "reset" and "--hard" in rest:
            return "git reset --hard"
        if sub == "clean":
            short = "".join(a[1:] for a in rest if a.startswith("-") and not a.startswith("--"))
            force = "f" in short or "--force" in rest
            dry_run = "n" in short or "--dry-run" in rest
            limited = paths and not any(p in WHOLE_TREE for p in paths)
            if force and not dry_run and not limited:
                return "git clean -f"
        if sub == "checkout":
            if any(a in {"-f", "--force"} for a in rest):
                return "git checkout -f"
            if "--" in rest and any(p in WHOLE_TREE for p in paths):
                return "git checkout -- ."
            if any(a in WHOLE_TREE for a in rest):
                return "git checkout ."
        if sub == "restore":
            staged_only = "--staged" in rest and not any(a in {"--worktree", "-W"} for a in rest)
            if not staged_only and any(p in WHOLE_TREE for p in paths):
                return "git restore ."
        if sub == "switch" and any(a in {"-f", "--force", "--discard-changes"} for a in rest):
            return "git switch --discard-changes"
        if sub == "stash":
            action = rest[0] if rest and not rest[0].startswith("-") else "push"
            if action == "clear":
                return "git stash clear"
            if action in {"push", "save"} and "--" not in rest:
                return "git stash (no paths)"
    return None


def command_of(tool_input: object) -> str:
    """The shell command of a tool call. An argv list (Codex may send one) is checked both as one
    command line (`git reset --hard`) and element by element (`bash -lc "<script>"`)."""
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, list):
        parts = [str(part) for part in command]
        return " ".join(parts) + "\n" + "\n".join(parts)
    return command if isinstance(command, str) else ""


def _own_worktree(cwd: str) -> bool:
    try:
        result = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=cwd or None, capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "/worktrees/" in result.stdout.replace("\\", "/")


def decide(payload: object) -> dict | None:
    """The PreToolUse deny answer for a hook payload, or None to let the call through."""
    if not isinstance(payload, dict):
        return None
    command = command_of(payload.get("tool_input"))
    found = danger(command)
    if found is None:
        return None
    if " -C " not in command and "cd " not in command and _own_worktree(str(payload.get("cwd") or "")):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"git_gate: `{found}` throws away the whole working tree, including uncommitted work of "
            "any other session open in this folder. Undo only the files you changed, by path "
            "(`git restore -- <file>`, `git checkout -- <file>`, `git stash push -- <file>`). If the "
            "whole tree really has to go, the user runs it in their own terminal."
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
