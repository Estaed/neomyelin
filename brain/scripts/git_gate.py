#!/usr/bin/env python3
"""PreToolUse gate: refuse git commands that throw away the whole working tree, and commits that
change a file's line endings.

    hook: python .brain/scripts/git_gate.py --pre-tool-use   (hook JSON on stdin)

The incident it prevents: two agent sessions were open in the same folder, and one of them undid
its own mistake with `git reset --hard`, which also erased the other session's uncommitted work.
A checkpoint commit does not protect someone else's half-done work. So only the forms that touch
the whole tree are refused (reset --hard, clean -f, checkout -f / -- . / ., restore ., switch
--discard-changes, stash clear, stash without paths); undoing named files stays allowed.

An agent in its own git worktree (the git dir sits under `/worktrees/`) is left alone: nobody
else works there. The gate never blocks on its own failure (exit 0, no decision).

Line endings: on Windows, Python's `write_text` rewrites an LF file as CRLF, and a one-word change
then commits as a whole-file diff. A `git commit` that would store a text file with another kind
of line ending than HEAD's blob (LF, CRLF, mixed) is refused. What the commit would store comes
from git itself: the staged blob, and for `-a`, `commit <paths>` and a `git add` earlier in the
same command, that `git add` replayed into a temporary index and object directory (filters,
`core.autocrlf` and `.gitattributes` apply; the repository is not written). So a repository that
normalises endings is never refused for it. New, deleted and binary files pass, a file mixed in
HEAD passes, and `# eol-ok` in the command lets an intended change through. This check applies
in an agent's own worktree too.

Any tool call whose input carries a shell `command` is checked, so the same gate covers Claude
Code's Bash and PowerShell tools and Codex's shell tool. Registered by render_hooks.py.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

SEPARATOR = re.compile(r"(?:&&|\|\||;|\||\n)")
GIT_OPTIONS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
WHOLE_TREE = {".", ":/", ":", "*", "./"}

EOL_BUDGET = 7.0  # seconds for the whole line-ending check; the hook's timeout is 10
EOL_NAMED = 8     # files named in the refusal
BLOB_MODES = {b"100644", b"100755"}
CD = {"cd", "chdir", "pushd", "set-location", "sl"}
WINDOWS_PATH = re.compile(r"(?<![\w])[A-Za-z]:\\")
REDIRECT = re.compile(r"[\d*&]*(?:>>?|<<?<?-?)(.*)")
COMMIT_VALUE_LONG = {"--message", "--file", "--reuse-message", "--reedit-message", "--template",
                     "--author", "--date", "--fixup", "--squash", "--trailer", "--cleanup"}
# A commit or add whose stored content cannot be known beforehand is not checked or replayed.
COMMIT_UNKNOWN = {"--dry-run", "-p", "--patch", "--interactive", "--pathspec-from-file"}
ADD_UNKNOWN = {"-n", "--dry-run", "-p", "--patch", "-i", "--interactive", "-e", "--edit",
               "--pathspec-from-file"}
ADD_KEPT = {"-A": "-A", "--all": "-A", "-u": "-u", "--update": "-u", "-f": "-f", "--force": "-f",
            "-N": "-N", "--intent-to-add": "-N", "--renormalize": "--renormalize",
            "--no-all": "--no-all", "--ignore-removal": "--no-all", "--ignore-errors": "--ignore-errors"}


def _git_argv(part: str) -> list[str] | None:
    """The arguments after `git` (its own options skipped), or None when the part is not git."""
    try:
        words = shlex.split(part, posix=True)
    except ValueError:  # a quote left open by the split: `git reset --hard"` is still `--hard`
        words = [word.strip("\"'") for word in part.split()]
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


def _shell_parts(command: str) -> list[str]:
    """The command split on `&&`, `||`, `;`, `|` and newlines outside quotes, with `#` comments
    (a `#` starting an unquoted word) dropped: `git commit -m "#29; notes" -a` is one part that
    keeps its `-a`. A command holding a Windows path (`C:\\x`) has no backslash escapes."""
    escapes = not WINDOWS_PATH.search(command)
    parts, current, quote, at = [], [], "", 0
    while at < len(command):
        char = command[at]
        pair = char == "\\" and escapes and at + 1 < len(command) and quote != "'"
        if pair:
            current.append(command[at:at + 2])
            at += 2
            continue
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "#" and (not current or current[-1][-1:].isspace()):
            end = command.find("\n", at)
            at = len(command) if end < 0 else end
            continue
        elif command.startswith(("&&", "||"), at) or char in ";|\n":
            parts.append("".join(current))
            current = []
            at += 2 if command.startswith(("&&", "||"), at) else 1
            continue
        current.append(char)
        at += 1
    return parts + ["".join(current)]


def _words(part: str) -> tuple[list[str], bool]:
    """A command part's shell words (its comment already dropped by _shell_parts), and whether
    they split cleanly. A part holding a Windows path (`C:\\x`) splits without escapes, which
    POSIX splitting would eat."""
    lexer = shlex.shlex(part, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if WINDOWS_PATH.search(part):
        lexer.escape = ""
    try:
        words, clean = list(lexer), True
    except ValueError:
        words, clean = [word.strip("\"'") for word in part.split()], False
    return words, clean


def _program(word: str) -> str:
    return re.split(r"[/\\]", word)[-1].lower()


def _resolve(base: str, target: str) -> str:
    """A `cd` or `-C` argument as a path from `base`; Git Bash's `/c/x` is `C:/x`."""
    target = os.path.expanduser(target)
    if os.name == "nt" and re.fullmatch(r"/[A-Za-z](?:/.*)?", target):
        target = f"{target[1]}:/{target[3:]}"
    return os.path.normpath(os.path.join(base, target))


def _options(args: list[str], short_value: str, long_value: set[str],
             short_attached: str = "") -> tuple[set[str], list[str]]:
    """The flags and the other words of a git subcommand's arguments. A short option in
    `short_value` takes the rest of its word or else the next word, one in `short_attached` only
    the rest of its word; a long option in `long_value` written without `=` takes the next word.
    A redirection (`> log`, `2>&1`, `<<EOF`) is skipped with its target."""
    flags, words, at, literal = set(), [], 0, False
    while at < len(args):
        word = args[at]
        at += 1
        redirect = REDIRECT.fullmatch(word)
        if redirect:
            at += not redirect.group(1)
        elif literal or not word.startswith("-") or word == "-":
            words.append(word)
        elif word == "--":
            literal = True
        elif word.startswith("--"):
            name = word.split("=", 1)[0]
            flags.add(name)
            at += name in long_value and "=" not in word
        else:
            for position, letter in enumerate(word[1:], 1):
                flags.add("-" + letter)
                if letter in short_value:
                    at += position == len(word) - 1
                    break
                if letter in short_attached:
                    break
    return flags, words


def _commit_stores(args: list[str], clean: bool) -> tuple[str, list[str]] | None:
    """What `git commit <args>` stores: ("index" | "all" | "only" | "include", pathspecs), or None
    when it cannot be known beforehand. Pathspecs count only from a part that split cleanly (a
    heredoc message splits into broken words)."""
    flags, paths = _options(args, "mFCct", COMMIT_VALUE_LONG, "Su")
    if flags & COMMIT_UNKNOWN:
        return None
    if paths and clean:
        return ("include" if flags & {"-i", "--include"} else "only"), paths
    return ("all" if flags & {"-a", "--all"} else "index"), []


def _add_replay(args: list[str], clean: bool) -> list[str] | None:
    """The arguments that replay `git add <args>` without a prompt, or None when it stages
    nothing predictable (dry run, interactive, no pathspec)."""
    flags, paths = _options(args, "", {"--chmod", "--pathspec-from-file"})
    if flags & ADD_UNKNOWN or (paths and not clean):
        return None
    kept = sorted({ADD_KEPT[flag] for flag in flags if flag in ADD_KEPT})
    if not paths and not {"-A", "-u"} & set(kept):
        return None
    return ["add", *kept, *(["--", *paths] if paths else [])]


def _commit_plan(command: str, cwd: str) -> tuple | None:
    """The command's first `git commit`: (its directory, its git options, its arguments, whether
    its part split cleanly, the `git add` calls before it as (directory, options, arguments,
    clean)), or None. `cd` and `git -C` move the directory as the shell would."""
    directory, adds = cwd or os.getcwd(), []
    for part in _shell_parts(command):
        words, clean = _words(part.strip())
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]  # VAR=value prefix
        if not words:
            continue
        program = _program(words[0])
        if program in CD:
            targets = [word for word in words[1:] if not word.startswith("-")]
            if targets:
                directory = _resolve(directory, targets[-1])
            continue
        if program not in {"git", "git.exe"}:
            continue
        here, options, args = directory, [], words[1:]
        while args and args[0].startswith("-"):
            option, name = args[0], args[0].split("=", 1)[0]
            if option in GIT_OPTIONS_WITH_VALUE and len(args) > 1:
                value, args = args[1], args[2:]
                if option == "-C":
                    here = _resolve(here, value)
                elif option == "-c":
                    options += ["-c", value]
                elif option in {"--git-dir", "--work-tree"}:
                    options.append(f"{option}={_resolve(here, value)}")
                continue
            if name in {"--git-dir", "--work-tree"} and "=" in option:
                options.append(f"{name}={_resolve(here, option.split('=', 1)[1])}")
            args = args[1:]
        if args and args[0] == "add":
            adds.append((here, options, args[1:], clean))
        elif args and args[0] == "commit":
            return here, options, args[1:], clean, adds
    return None


def _kind(data: bytes) -> str | None:
    """The kind of line ending (lf, crlf or mixed); None for content without one."""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    return "mixed" if crlf and lf else "crlf" if crlf else "lf" if lf else None


def _same_path(first: str, second: str) -> bool:
    return os.path.normcase(os.path.normpath(first)) == os.path.normcase(os.path.normpath(second))


def _changed_endings(directory: str, options: list[str], stores: tuple[str, list[str]],
                     adds: list[tuple], expand_globs: bool = True) -> list[str]:
    mode, paths = stores
    deadline = time.monotonic() + EOL_BUDGET
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}

    def git(*argv: str, cwd: str = directory, opts: list[str] = options, data: bytes | None = None,
            check: bool = True) -> bytes:
        feed = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
        result = subprocess.run(["git", *opts, *argv], cwd=cwd, env=env, capture_output=True,
                                timeout=max(deadline - time.monotonic(), 0.1), **feed)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, argv[:1])
        return result.stdout

    # Fails without a HEAD (a first commit: every file is new) or outside a repository.
    top, index, objects = git("rev-parse", "--show-toplevel", "--git-path", "index", "--git-path",
                              "objects", "--verify", "-q", "HEAD").decode("utf-8").splitlines()[:3]
    index, objects = os.path.join(directory, index), os.path.join(directory, objects)
    replays = []
    if mode != "only":  # `commit <paths>` ignores what is staged
        for here, add_options, add_args, add_clean in adds:
            argv = _add_replay(add_args, add_clean)
            if argv and not _same_path(here, directory):  # an add in another repository is not this commit's
                found = git("rev-parse", "--show-toplevel", cwd=here, opts=add_options, check=False)
                argv = argv if _same_path(found.decode("utf-8").strip(), top) else None
            if argv:
                # Bash expands an unquoted glob to the matching names in that folder; git would
                # match the same pathspec in every sub-folder too. PowerShell (and a shell we cannot
                # name) passes it to git as it is.
                argv = [match for word in argv for match in
                        (sorted(glob.glob(word, root_dir=here)) or [word]
                         if expand_globs and re.search(r"[*?[]", word) and not word.startswith(":")
                         else [word])]
                replays.append((here, add_options, argv))
    if mode == "all":
        replays.append((directory, options, ["add", "-u", "--", ":/"]))
    elif mode in {"only", "include"}:
        replays.append((directory, options, ["add", "--", *paths]))
    with tempfile.TemporaryDirectory(prefix="git-gate-eol-", ignore_cleanup_errors=True) as temp:
        if replays:
            # `git add` itself computes the blobs, into a copy of the index and a scratch object
            # directory that falls back on the repository's objects.
            alternates = [objects, os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES", "")]
            env.update(GIT_INDEX_FILE=os.path.join(temp, "index"),
                       GIT_OBJECT_DIRECTORY=os.path.join(temp, "objects"),
                       GIT_ALTERNATE_OBJECT_DIRECTORIES=os.pathsep.join(filter(None, alternates)))
            os.mkdir(env["GIT_OBJECT_DIRECTORY"])
            if mode == "only":
                git("read-tree", "HEAD")
            elif os.path.isfile(index):
                shutil.copyfile(index, env["GIT_INDEX_FILE"])
            for here, opts, argv in replays:
                git(*argv, cwd=here, opts=opts, check=False)  # a failing add fails the real one too
        raw = git("diff-index", "--cached", "-z", "--no-renames", "HEAD", cwd=top).split(b"\0")
        pairs = []
        for at in range(0, len(raw) - 1, 2):
            meta = raw[at].lstrip(b":").split()
            if len(meta) == 5 and meta[4] == b"M" and meta[0] in BLOB_MODES and meta[1] in BLOB_MODES:
                pairs.append((raw[at + 1], meta[2], meta[3]))
        if not pairs:
            return []
        attributes = git("check-attr", "-z", "--stdin", "text", "diff", cwd=top,
                         data=b"".join(name + b"\0" for name, _, _ in pairs)).split(b"\0")
        unset: dict[bytes, set[bytes]] = {}
        for at in range(0, len(attributes) - 2, 3):
            if attributes[at + 2] == b"unset":
                unset.setdefault(attributes[at], set()).add(attributes[at + 1])
        # The `binary` macro (`-text -diff -merge`); a bare `-text` only stops git converting
        # endings, so such a file's endings reach history as written and are checked.
        declared_binary = {name for name, names in unset.items() if {b"text", b"diff"} <= names}
        pairs = [pair for pair in pairs if pair[0] not in declared_binary]
        output = git("cat-file", "--batch", cwd=top,
                     data=b"".join(sha + b"\n" for _, old, new in pairs for sha in (old, new))) if pairs else b""
    blobs, at = {}, 0
    while at < len(output):
        end = output.index(b"\n", at)
        header, at = output[at:end].split(), end + 1
        if len(header) == 3:  # `<sha> <type> <size>`; a missing object has no body
            blobs[header[0]] = output[at:at + int(header[2])]
            at += int(header[2]) + 1
    changed = []
    for name, old, new in pairs:
        before, after = blobs.get(old), blobs.get(new)
        if before is None or after is None or b"\0" in before[:8000] or b"\0" in after[:8000]:
            continue  # binary by git's own test
        was, now = _kind(before), _kind(after)
        if was in {"lf", "crlf"} and now and now != was:
            changed.append(f"{name.decode('utf-8', 'replace')} ({was} -> {now})")
    return changed


def eol_changes(command: str, cwd: str, tool: str = "Bash") -> list[str]:
    """`<file> (<HEAD kind> -> <stored kind>)` for each text file whose line endings the command's
    first `git commit` would change; [] when it commits nothing checkable, carries `eol-ok`, or the
    check fails (the gate never blocks on its own failure)."""
    if "eol-ok" in command:
        return []
    try:
        plan = _commit_plan(command, cwd)
        if plan is None:
            return []
        directory, options, args, clean, adds = plan
        stores = _commit_stores(args, clean)
        return (_changed_endings(directory, options, stores, adds, expand_globs=tool == "Bash")
                if stores else [])
    except Exception:  # noqa: BLE001 - a broken check lets the commit through
        return []


def _eol_reason(changed: list[str]) -> str:
    named = ", ".join(changed[:EOL_NAMED])
    if len(changed) > EOL_NAMED:
        named += f" and {len(changed) - EOL_NAMED} more"
    return (f"git_gate: this commit would store other line endings than HEAD for {named}. Put back each "
            "file's HEAD endings at byte level (read 'rb'; to lf: replace b'\\r\\n' with b'\\n'; to crlf: that, "
            "then b'\\n' with b'\\r\\n'; write 'wb', never write_text, which writes CRLF on Windows), stage "
            "it and commit again; if the change is intended, add `# eol-ok` to the command.")


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


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
    cwd = str(payload.get("cwd") or "")
    found = danger(command)
    if found is not None and not (" -C " not in command and "cd " not in command and _own_worktree(cwd)):
        return _deny(
            f"git_gate: `{found}` throws away the whole working tree, including uncommitted work of "
            "any other session open in this folder. Undo only the files you changed, by path "
            "(`git restore -- <file>`, `git checkout -- <file>`, `git stash push -- <file>`). If the "
            "whole tree really has to go, the user runs it in their own terminal."
        )
    changed = eol_changes(command, cwd, str(payload.get("tool_name") or ""))
    return _deny(_eol_reason(changed)) if changed else None


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
