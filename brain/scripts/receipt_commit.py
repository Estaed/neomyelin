#!/usr/bin/env python3
"""Stop hook: commit the vault's receipts/ and daily/ the moment a session ends, then push.

    hook:  python .brain/scripts/receipt_commit.py            (Stop; the payload on stdin is ignored)
    child: python .brain/scripts/receipt_commit.py --commit   (started by the hook, detached)
    child: python .brain/scripts/receipt_commit.py --push     (started by --commit, detached)

A receipt is a session's last step, written after the session's own commit, so it used to wait
for the nightly daily commit: lost to a crash, a reset or a second machine, or swept into a
parallel session's commit under the wrong message. This hook commits only `receipts/` and
`daily/` (vault from config.py) with the user's own git identity; anything else staged stays
staged and anything else edited stays unstaged. The commit runs in a detached child that the Stop
waits on for a few seconds at most: a slow commit (a pre-commit hook) goes on after the Stop, and
the child still sees how it ends, records it and starts the push. When the vault has a remote,
the push runs in a second detached child, so the Stop never waits on the network; it pushes the
way daily_commit.py does (sets a missing upstream, takes the first remote when none is `origin`).
The children open no console window.

It never blocks and never fails the Stop; it always exits 0. No git repository, nothing to
commit, `index.lock` held by another process, a rebase, merge, cherry-pick or revert in
progress, a detached HEAD (a commit there would leave the working tree at the next checkout): it
leaves the vault as it was and the receipts wait for a later Stop or the nightly daily commit. A
commit that does not land puts back the index entries its `git add` changed, so no receipt is
left staged for another session's commit to sweep in (when a parallel Stop's commit took them
meanwhile, nothing is left to commit and that is no failure). It does nothing in the layer's own
processes (NEOMYELIN_INVOKED_BY), so nightly children do not race the daily commit. A git call
that takes an index lock is never killed (a killed one leaves a stale `index.lock` that breaks
every later git command in the vault). Every commit, push, failure and skipped change is one line
in `.brain/.state/receipt-commit.log` (a Stop with nothing to commit writes none); a failed
commit or push also becomes a health warning that the next session start shows.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
import stage  # noqa: E402

FOLDERS = ('receipts', 'daily')  # brain.py writes these; evidence.py and doctor.py read them
INVOKED_ENV = 'NEOMYELIN_INVOKED_BY'  # memory_context.INVOKED_ENV: the layer's own processes
LOG_NAME = 'receipt-commit.log'
# The Stop is registered with a 10 s timeout; the hook returns before it, whatever git does.
BUDGET = 7.0
# A detached child waits this long on one git call (a pre-commit hook, the network).
CHILD_TIMEOUT = 300
# How long a commit that did not land waits out another process's index.lock to put the index back.
RESTORE_WAIT = 30.0
# Files in the git directory that mean another operation owns the index or HEAD right now.
BUSY = (('index.lock', 'another git process holds index.lock'),
        ('rebase-merge', 'a rebase is in progress'), ('rebase-apply', 'a rebase is in progress'),
        ('MERGE_HEAD', 'a merge is in progress'), ('CHERRY_PICK_HEAD', 'a cherry-pick is in progress'),
        ('REVERT_HEAD', 'a revert is in progress'))
# Inherited from a git hook or a wrapper, these would point git at another repository or index.
GIT_LOCATION = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE')
# DETACHED_PROCESS leaves Python with no console, so every git it starts opens its own window.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
CHILD_FLAGS = NO_WINDOW | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)


def state_dir(vault: Path) -> Path:
    return vault / '.brain' / '.state'


def log(vault: Path, message: str) -> None:
    stamp = dt.datetime.now().astimezone().isoformat(timespec='seconds')
    try:
        path = state_dir(vault) / LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(f'{stamp} {message}\n')
    except OSError:
        pass


def fail(vault: Path, message: str) -> None:
    """A failed commit or push: a log line and a health warning, never silence."""
    log(vault, message)
    try:
        stage.write_health(state_dir(vault), f'{message} (.brain/.state/{LOG_NAME})', warning=True)
    except OSError:
        pass


def git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}
    # `git status` would otherwise refresh the index under index.lock and could make another
    # session's git command fail on that lock.
    env['GIT_OPTIONAL_LOCKS'] = '0'
    env['GIT_TERMINAL_PROMPT'] = '0'  # no one is there to answer a credential prompt
    return env


def git(vault: Path, *args: str, timeout: float, kill: bool = True,
        input: str = '') -> subprocess.CompletedProcess[str] | None:
    """Run git in the vault; None when it outlived `timeout`. With kill=False it is left running.

    Its input and output go through temporary files, not pipes, so a git (or a git hook) left
    running keeps writing after this process is gone instead of dying on a closed pipe.
    """
    command = ['git', '-C', str(vault), *args]
    with tempfile.TemporaryFile() as given, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        given.write(input.encode('utf-8'))
        given.seek(0)
        process = subprocess.Popen(command, stdin=given, stdout=out, stderr=err,
                                   env=git_env(), creationflags=NO_WINDOW)
        try:
            process.wait(timeout=max(timeout, 0.1))
        except subprocess.TimeoutExpired:
            if kill:
                process.kill()
                process.wait()
            return None
        texts = []
        for stream in (out, err):
            stream.seek(0)
            texts.append(stream.read().decode('utf-8', 'replace'))
    return subprocess.CompletedProcess(command, process.returncode, *texts)


def reason(result: subprocess.CompletedProcess[str]) -> str:
    """git's own words for a failure: its fatal/error line, else its first line that says something."""
    lines = [line.strip() for line in f'{result.stderr}\n{result.stdout}'.splitlines() if line.strip()]
    chosen = (next((line for line in lines if line.startswith(('fatal:', 'error:'))), None)
              or next((line for line in lines if not line.startswith('warning:')), None)
              or f'git exited {result.returncode}')
    return chosen[:200]


def changed_paths(porcelain: str) -> list[str]:
    """Paths from `git status --porcelain -z`; a rename or copy carries its source as the next field."""
    fields = porcelain.split('\0')
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in 'RC':
            index += 1
    return paths


def busy(vault: Path) -> str | None:
    """Why the repository must not be committed to now, or None."""
    found = git(vault, 'rev-parse', '--absolute-git-dir', timeout=CHILD_TIMEOUT)
    if found is None or found.returncode:
        return 'the git directory could not be read'
    git_dir = Path(found.stdout.strip())
    why = next((why for name, why in BUSY if (git_dir / name).exists()), None)
    if why:
        return why
    # On a detached HEAD (a bisect, `git checkout <sha>`) the next checkout takes a committed
    # receipt out of the working tree and leaves it only in a dangling commit.
    attached = git(vault, 'symbolic-ref', '-q', 'HEAD', timeout=CHILD_TIMEOUT)
    if attached is None:
        return 'HEAD could not be read'
    return 'HEAD is detached' if attached.returncode else None


def spawn(vault: Path, flag: str) -> subprocess.Popen:
    """Start this script with `flag`, detached: no console window, its own process group, no inherited pipes."""
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    options: dict = {'creationflags': CHILD_FLAGS} if os.name == 'nt' else {'start_new_session': True}
    return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), flag], cwd=str(vault), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True, **options)


def stop(vault: Path) -> None:
    """The Stop: a quick look, and when receipts or the daily view changed, the `--commit` child."""
    if not (vault / '.git').exists():
        return  # not a git repository: nothing to commit to
    started = time.monotonic()
    status = git(vault, 'status', '--porcelain', '-z', '--untracked-files=all', '--', *FOLDERS,
                 timeout=BUDGET)
    if status is None:
        log(vault, f'skipped: git status took longer than {BUDGET:g} s')
        return
    if status.returncode:
        fail(vault, f'receipt commit failed: {reason(status)}')
        return
    if not changed_paths(status.stdout):
        return
    child = spawn(vault, '--commit')
    try:
        # Usually done well inside the budget, so the session ends with its receipt committed.
        child.wait(timeout=max(BUDGET - (time.monotonic() - started), 0.1))
    except subprocess.TimeoutExpired:
        pass  # a slow commit: the child finishes it, records how it ended and pushes


def commit(vault: Path) -> None:
    """The `--commit` child: commit receipts/ and daily/, record how it ended, start the push."""
    status = git(vault, 'status', '--porcelain', '-z', '--untracked-files=all', '--', *FOLDERS,
                 timeout=CHILD_TIMEOUT)
    if status is None or status.returncode:
        settle(vault, status, 'status')
        return
    changed = changed_paths(status.stdout)
    if not changed:
        return
    why = busy(vault)
    if why:
        log(vault, f'skipped: {why}; {len(changed)} changed files wait for the nightly commit')
        return
    # A pathspec that matches nothing fails the commit, so only folders that hold a change.
    folders = [name for name in FOLDERS if any(path == name or path.startswith(name + '/') for path in changed)]
    # The index as it is before `add`, so a commit that does not land can put it back.
    head = git(vault, 'rev-parse', '-q', '--verify', 'HEAD', timeout=CHILD_TIMEOUT)
    if head is None:
        settle(vault, head, 'rev-parse')
        return
    before = git(vault, 'ls-files', '-s', '-z', '--', *folders, timeout=CHILD_TIMEOUT)
    if before is None or before.returncode:
        settle(vault, before, 'ls-files')
        return

    def undo(result: subprocess.CompletedProcess[str] | None, step: str, staged: list[str]) -> None:
        problem = restore(vault, head.stdout.strip(), before.stdout, sorted({*changed, *staged}))
        if not problem and staged and result is not None and not pending(vault, staged):
            # Nothing waits: a parallel Stop's commit took these files between this add and commit.
            log(vault, f'skipped: nothing left to commit, the {len(staged)} files are committed '
                       f'or changed back ({step}: {reason(result)})')
            return
        settle(vault, result, step)
        if problem:
            fail(vault, f'receipt commit failed; {problem}')

    added = git(vault, 'add', '-A', '--', *folders, timeout=CHILD_TIMEOUT, kill=False)
    if added is None or added.returncode:
        settle(vault, added, 'add')  # a failed add writes no index
        return
    staged = git(vault, 'diff', '--cached', '--name-only', '-z', '--', *folders, timeout=CHILD_TIMEOUT)
    if staged is None or staged.returncode:
        undo(staged, 'diff', [])
        return
    names = [name for name in staged.stdout.split('\0') if name]
    if not names:
        return  # the change was one git does not record (line endings, a touched file)
    message = f'Receipts and daily view ({len(names)} file{"" if len(names) == 1 else "s"})'
    # A pathspec commit takes only these paths; what another session staged stays staged.
    committed = git(vault, 'commit', '-q', '-m', message, '--', *folders, timeout=CHILD_TIMEOUT, kill=False)
    if committed is None:
        settle(vault, committed, 'commit')  # still running: nothing to put back yet
        return
    if committed.returncode:
        undo(committed, 'commit', names)
        return
    log(vault, f'commit: {message}')
    remotes = git(vault, 'remote', timeout=20)
    if remotes is not None and not remotes.returncode and remotes.stdout.split():
        spawn(vault, '--push')


def restore(vault: Path, head: str, before: str, paths: list[str]) -> str | None:
    """Put the index entries of `paths` back as `ls-files -s` listed them; None when done, else what is left.

    A commit that landed meanwhile (a parallel Stop's, another session's) wrote the paths it
    changed into the index as it committed them, and an old entry would stage their deletion or
    an old version: those are left as it wrote them, reported only when still staged against it.
    The paths it did not change are put back. Another process's index.lock is waited out.
    """
    entries: dict[str, list[str]] = {}
    for entry in before.split('\0'):
        if '\t' in entry:
            entries.setdefault(entry.split('\t', 1)[1], []).append(entry)
    oid = head or next((entry.split()[1] for group in entries.values() for entry in group), '0' * 40)
    give_up = time.monotonic() + RESTORE_WAIT
    while True:
        now = git(vault, 'rev-parse', '-q', '--verify', 'HEAD', timeout=CHILD_TIMEOUT)
        if now is None:
            return f'{len(paths)} files left staged: git rev-parse gave no answer'
        current = now.stdout.strip()
        taken = set() if current == head else changed_between(vault, head, current)
        if taken is None:
            return f'{len(paths)} files left staged: the commit that moved HEAD could not be read'
        back = [path for path in paths if path not in taken]
        lines: list[str] = []
        for path in back:  # mode 0 drops the entry; then the entries it had before, if any
            lines += [f'0 {"0" * len(oid)}\t{path}', *entries.get(path, ())]
        if lines:
            put = git(vault, 'update-index', '-z', '--index-info', input=''.join(line + '\0' for line in lines),
                      timeout=CHILD_TIMEOUT, kill=False)
            if put is None:
                return f'{len(back)} files left staged: git update-index gave no answer in {CHILD_TIMEOUT} s'
            if put.returncode:
                if 'index.lock' not in reason(put) or time.monotonic() > give_up:
                    return f'{len(back)} files left staged: {reason(put)}'
                time.sleep(0.2)
                continue
        kept = taken.intersection(paths)
        if not kept:
            return None
        left = git(vault, 'diff', '--cached', '--name-only', '--no-renames', '-z',
                   *([current] if current else []), '--', *FOLDERS, timeout=CHILD_TIMEOUT)
        if left is None or left.returncode:
            return f'{len(kept)} files may be left staged: the index could not be compared with HEAD'
        still = kept.intersection(left.stdout.split('\0'))
        return f'{len(still)} files left staged: another commit moved HEAD meanwhile' if still else None


def changed_between(vault: Path, old: str, new: str) -> set[str] | None:
    """The receipts/ and daily/ paths that differ between commits `old` and `new` ('' is an unborn HEAD)."""
    if old and new:
        listed = git(vault, 'diff', '--name-only', '--no-renames', '-z', old, new, '--', *FOLDERS,
                     timeout=CHILD_TIMEOUT)
    else:  # from or to nothing: every path the one commit holds
        listed = git(vault, 'ls-tree', '-r', '--name-only', '-z', old or new, '--', *FOLDERS,
                     timeout=CHILD_TIMEOUT)
    if listed is None or listed.returncode:
        return None
    return {name for name in listed.stdout.split('\0') if name}


def pending(vault: Path, names: list[str]) -> bool:
    """Whether any of `names` still differs from HEAD, staged or not; True when git cannot say."""
    status = git(vault, 'status', '--porcelain', '-z', '--untracked-files=all', '--', *FOLDERS,
                 timeout=CHILD_TIMEOUT)
    if status is None or status.returncode:
        return True
    return not set(names).isdisjoint(changed_paths(status.stdout))


def settle(vault: Path, result: subprocess.CompletedProcess[str] | None, step: str) -> None:
    """Record a git step that did not succeed: another process's index.lock is a skip, anything else a failure."""
    if result is None:
        fail(vault, f'receipt commit failed: git {step} gave no answer in {CHILD_TIMEOUT} s')
        return
    why = reason(result)
    if 'index.lock' in why:
        log(vault, f'skipped: another git process holds index.lock ({step})')
        return
    fail(vault, f'receipt commit failed: {why}')


def push(vault: Path) -> None:
    """Push the current branch the way daily_commit.py does."""
    remotes = git(vault, 'remote', timeout=20)
    if remotes is None or remotes.returncode:
        fail(vault, 'receipt push failed: ' + ('git remote timed out' if remotes is None else reason(remotes)))
        return
    names = remotes.stdout.split()
    if not names:
        log(vault, 'push skipped: no remote')
        return
    branch = git(vault, 'symbolic-ref', '--quiet', '--short', 'HEAD', timeout=20)
    if branch is None or branch.returncode:
        log(vault, 'push skipped: no current branch')
        return
    upstream = git(vault, 'rev-parse', '--abbrev-ref', '@{upstream}', timeout=20)
    # A remote added by hand after `git init` leaves the branch without an upstream, and a bare
    # `git push` then refuses; the first push sets it.
    if upstream is None or upstream.returncode:
        remote = 'origin' if 'origin' in names else names[0]
        pushed = git(vault, 'push', '-q', '-u', remote, branch.stdout.strip(), timeout=CHILD_TIMEOUT)
    else:
        pushed = git(vault, 'push', '-q', timeout=CHILD_TIMEOUT)
    if pushed is None:
        fail(vault, f'receipt push failed: no answer in {CHILD_TIMEOUT} s')
    elif pushed.returncode:
        fail(vault, f'receipt push failed: {reason(pushed)}')
    else:
        log(vault, f'push: {branch.stdout.strip()}')


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config.force_utf8()
    if '-h' in argv or '--help' in argv:
        print(__doc__)
        return 0
    if os.environ.get(INVOKED_ENV):
        return 0
    try:
        vault = config.vault_path(config.load())
    except (config.ConfigError, OSError) as exc:
        print(f'receipt commit skipped: {exc}', file=sys.stderr)
        return 0
    pushing = '--push' in argv
    try:
        push(vault) if pushing else commit(vault) if '--commit' in argv else stop(vault)
    except Exception as exc:  # noqa: BLE001 - a Stop hook must never break the session
        fail(vault, f'receipt {"push" if pushing else "commit"} failed: {type(exc).__name__}: {exc}'[:300])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
