#!/usr/bin/env python3
"""Read-only health report for a NeoMyelin vault."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Callable

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

OK, WARNING, ERROR = "OK", "WARNING", "ERROR"
STATUSES = {OK, WARNING, ERROR}
META_LINE = re.compile(r'^\s*("[^"]+"\s*:|[{}]\s*,?\s*$|---\s*$)')


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _one_line(value: object, limit: int = 240) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _run(args: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False, timeout=20)
    except FileNotFoundError:  # the command is not installed (git): a failed run, not a crash
        return subprocess.CompletedProcess(args, 127, "", f"{args[0]} is not installed")


def _memory_module(root: Path):
    scripts = root / ".brain" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import memory_context
    return memory_context


def check_hooks(root: Path, home: Path | None = None) -> tuple[str, str]:
    """The hook scripts are there, the vault's own hook files point at existing scripts, and each
    configured harness whose command is installed has every hook of this vault in its user-level
    hook file (or, after `install.py --scope project`, in the vault's own)."""
    path = root / ".brain" / "scripts" / "render_hooks.py"
    if not path.is_file():
        return WARNING, "hook renderer is missing"
    script_dir = root / ".brain" / "scripts"
    required = ("memory_context.py", "prompt_recall.py", "receipt_gate.py")
    missing = [name for name in required if not (script_dir / name).is_file()]
    if missing:
        return WARNING, "hook scripts missing: " + ", ".join(missing)
    broken = []
    for settings_path in (root / ".claude/settings.json", root / ".codex/hooks.json",
                          root / ".agents/hooks.json"):
        if not settings_path.exists():
            continue
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            broken.append(f"{settings_path.name}: {_one_line(exc, 80)}")
            continue
        if not isinstance(data, dict):
            broken.append(f"{settings_path.name}: root is not an object")
            continue
        groups = data.get("hooks", data.get("neomyelin", {}))
        if not isinstance(groups, dict):
            broken.append(f"{settings_path.name}: hooks are not an object")
            continue
        for event, entries in groups.items():
            if not isinstance(entries, list):
                broken.append(f"{settings_path.name}: {event} is not a list")
                continue
            for group in entries:
                handlers = group.get("hooks", []) if isinstance(group, dict) else [group]
                if not isinstance(handlers, list):
                    broken.append(f"{settings_path.name}: {event} handler list is invalid")
                    continue
                for handler in handlers:
                    command = handler.get("command", "") if isinstance(handler, dict) else ""
                    match = re.search(r"\.brain[/\\]([^\s\"']+\.py)", command)
                    if match and not (root / ".brain" / Path(match.group(1).replace("\\", "/"))).is_file():
                        broken.append(f"{settings_path.name}: missing {match.group(1)}")
    if broken:
        return WARNING, "; ".join(broken[:5])
    return _registration(root, home or Path.home())


def _registration(root: Path, home: Path) -> tuple[str, str]:
    import render_hooks
    cfg = config.load(root / ".brain" / "config.json")
    found, problems = [], []
    for harness in cfg["harnesses"]:
        if shutil.which(render_hooks.CLIS[harness]) is None:
            found.append(f"{harness} skipped ({render_hooks.CLIS[harness]} not installed)")
            continue
        try:
            render_hooks.entries(harness, root, sys.executable)  # Unsupported: install skipped it too
            user = render_hooks.settings_path(harness, "user", home, root)
        except render_hooks.Unsupported as exc:
            problems.append(f"{harness}: no hooks, {_one_line(exc, 160)}")
            continue
        missing = None
        for scope in ("user", "project"):
            try:
                settings, _ = render_hooks.read_settings(render_hooks.settings_path(harness, scope, home, root))
            except ValueError as exc:
                problems.append(f"{harness}: {_one_line(exc, 120)}")
                break
            left = render_hooks.unregistered(harness, settings, root)
            if not left:
                found.append(f"{harness} ({scope})")
                break
            missing = missing if missing is not None else left
        else:
            problems.append(f"{harness}: {user} lacks {', '.join(missing)} (run install.py again)")
    detail = "registered: " + (", ".join(found) or "none")
    if problems:
        return WARNING, "; ".join(problems[:3]) + f"; {detail}"
    return OK, detail + "; session/recall/receipt scripts present"


def check_tools(root: Path) -> tuple[str, str]:
    cfg = config.load(root / ".brain" / "config.json")
    cli_names = {"claude": "claude", "codex": "codex", "agy": "agy"}
    missing = [cli_names[name] for name in cfg["harnesses"] if shutil.which(cli_names[name]) is None]
    detail = f"Python {sys.version_info.major}.{sys.version_info.minor} available"
    if missing:
        return WARNING, detail + "; configured CLI unavailable: " + ", ".join(missing)
    return OK, detail + "; configured harness CLIs available"


def check_daily(root: Path, now: dt.datetime | None = None) -> tuple[str, str]:
    today = (now or dt.datetime.now().astimezone()).date()
    entries = []
    for path in (root / "daily").glob("*.md"):
        try:
            entries.append((dt.date.fromisoformat(path.stem), path.name))
        except ValueError:
            continue
    if not entries:
        if not any((root / "receipts").glob("*.md")):
            return OK, "no receipts yet, so no daily view (brain.py writes it with the first receipt)"
        return WARNING, "daily folder has no dated entries yet (run brain.py sync)"
    day, name = max(entries)
    age = (today - day).days
    return (WARNING if age > 2 else OK), f"newest {name}, {age} days old"


def check_health(root: Path, now: dt.datetime | None = None) -> tuple[str, str]:
    path = root / ".brain/.state/health.json"
    if not path.exists():
        return OK, "no previous health warning is recorded"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("health state must be a JSON object")
    stamp = value.get("ts")
    if isinstance(stamp, (int, float)):
        when = dt.datetime.fromtimestamp(stamp, dt.timezone.utc)
    elif isinstance(stamp, str):
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    else:
        raise ValueError("health state has no readable timestamp")
    current = now or dt.datetime.now(dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    if current.astimezone(dt.timezone.utc) - when.astimezone(dt.timezone.utc) > dt.timedelta(hours=72):
        return OK, "last health record is over 72 hours old"
    items = ([str(value["error"])] if value.get("error") else []) + [str(x) for x in value.get("warnings", []) if x]
    return (WARNING, _one_line("; ".join(items))) if items else (OK, "last health record has no warnings")


def check_rules(root: Path) -> tuple[str, str]:
    module = _memory_module(root)
    companion = config.companion_dir({"vault": str(root)})
    if companion is None:
        return WARNING, "multiple possible companion folders"
    budgets = {"rules": 7000, "core": 1500}
    sizes = []
    over = False
    for key, filename in (("rules", "Rules.md"), ("core", "Core.md")):
        cap = int(getattr(module, "BUDGETS", budgets).get(key, budgets[key]))
        size = (companion / filename).stat().st_size
        sizes.append(f"{filename} {size}/{cap} bytes")
        over |= size > cap
    return (WARNING if over else OK), ", ".join(sizes)


def check_session_context(root: Path) -> tuple[str, str]:
    module = _memory_module(root)
    cfg = config.load(root / ".brain" / "config.json")
    payload = json.loads(module.session_start_context("", cfg=cfg, emit=False, warm=False))
    context = payload["hookSpecificOutput"]["additionalContext"]
    size = len(context.encode("utf-16-le")) // 2
    cap = int(module.HARNESS_CAP)
    return (ERROR if size >= cap else OK), f"session context {size}/{cap} UTF-16 units"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        raise ValueError("frontmatter is missing")
    value = json.loads(parts[1])
    if not isinstance(value, dict):
        raise ValueError("frontmatter must be an object")
    return value


def check_tasks(root: Path) -> tuple[str, str]:
    broken = []
    stale = []
    for path in sorted((root / "tasks").glob("*.md")):
        try:
            metadata = _frontmatter(path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            broken.append(f"{path.name}: {_one_line(exc, 80)}")
            continue
        if metadata.get("status") not in {"active", "waiting"}:
            continue
        relative = path.relative_to(root).as_posix()
        history = _run(["git", "log", "--format=@@COMMIT", "-p", "--unified=0", "--", relative], root)
        metadata_only = 0
        if history.returncode == 0:
            for commit in history.stdout.split("@@COMMIT")[1:]:
                changed = [line[1:] for line in commit.splitlines()
                           if line[:1] in "+-" and not line.startswith(("+++", "---"))]
                if not changed:
                    continue
                if any(line.strip() and not META_LINE.match(line) for line in changed):
                    break
                metadata_only += 1
        title_numbers = set(re.findall(r"\d{2,}(?:-\d{2}){0,2}", str(metadata.get("title", ""))))
        body = path.read_text(encoding="utf-8-sig").split("---", 2)[-1]
        absent = [number for number in title_numbers if not re.search(rf"(?<!\d){re.escape(number)}(?!\d)", body)]
        if metadata_only >= 2:
            stale.append(f"{path.stem} ({metadata_only} metadata-only updates)")
        elif absent:
            stale.append(f"{path.stem} (title values absent from body: {', '.join(absent)})")
    if broken:
        return WARNING, "unreadable task metadata: " + _one_line("; ".join(broken[:5]))
    if stale:
        return WARNING, "task body may be stale: " + "; ".join(stale[:5])
    return OK, "task frontmatter is readable; no stale open task bodies found"


def check_recall_index(root: Path) -> tuple[str, str]:
    indexes = [root / ".brain/scripts/.state" / f"recall-{engine}.json" for engine in ("bm25", "ollama")]
    present = [path for path in indexes if path.is_file()]
    if not present:
        return WARNING, "recall index is missing"
    try:
        data = json.loads(max(present, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return WARNING, f"recall index cannot be read: {_one_line(exc)}"
    if not isinstance(data, (dict, list)):
        return WARNING, "recall index has an unexpected format"
    return OK, "recall index is readable"


def check_engine(root: Path) -> tuple[str, str]:
    """The vault's engine (brain.py: receipts, tasks, daily view) and its own self-check."""
    engine = root / "brain.py"
    if not engine.is_file():
        return WARNING, "brain.py is missing at the vault root (run install.py again)"
    result = _run([sys.executable, str(engine), "doctor"], root)
    if result.returncode:
        return WARNING, "brain.py doctor failed: " + _one_line(result.stderr or result.stdout, 160)
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("brain.py doctor result must be a JSON object")
    counts = f"{value.get('receipts', 0)} receipts, {value.get('tasks', 0)} tasks"
    problems = value.get("problems") or []
    if value.get("status") != "ok":
        return WARNING, f"{counts}; " + "; ".join(str(item) for item in problems[:3])
    return OK, f"{counts}; daily view up to date"


def check_git(root: Path) -> tuple[str, str]:
    if shutil.which("git") is None:
        return WARNING, ("git is not installed, so the vault keeps no history and the nightly run "
                         "commits nothing; install git, then run git init in the vault")
    probe = _run(["git", "rev-parse", "--is-inside-work-tree"], root)
    if probe.returncode or probe.stdout.strip().lower() != "true":
        return WARNING, "vault is not a Git worktree"
    status = _run(["git", "status", "--short"], root)
    if status.returncode:
        raise RuntimeError(_one_line(status.stderr or status.stdout))
    tracked = _run(["git", "ls-files", "-z"], root)
    if tracked.returncode:
        raise RuntimeError(_one_line(tracked.stderr or tracked.stdout))
    sensitive = [item for item in tracked.stdout.split("\0") if item and
                 (Path(item).name == ".env" or Path(item).name.endswith(".bak") or ".backup" in Path(item).name)]
    if sensitive:
        return ERROR, "tracked sensitive or backup files: " + ", ".join(sensitive[:5])
    changed_count = len(status.stdout.splitlines())
    remotes = _run(["git", "remote"], root)
    if "origin" not in remotes.stdout.split():
        return WARNING, f"no origin remote; {changed_count} changed paths"
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()
    ahead = _run(["git", "log", f"origin/{branch}..HEAD", "--format=%ct"], root)
    if ahead.returncode:
        return WARNING, f"cannot compare commits with origin/{branch}"
    stamps = [int(value) for value in ahead.stdout.split()]
    old = [stamp for stamp in stamps if time.time() - stamp > 26 * 3600]
    if old:
        return WARNING, f"{len(stamps)} commits are unpushed; oldest is over 26 hours old"
    return OK, f"Git worktree present; {len(stamps)} commits awaiting push; {changed_count} changed paths"


def _receipts(root: Path):
    for path in (root / "receipts").glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8-sig")
            parts = text.split("---", 2)
            if len(parts) >= 3:
                yield path, json.loads(parts[1]), parts[2]
        except (OSError, UnicodeError, ValueError):
            continue


def check_corrections(root: Path, now: dt.datetime | None = None) -> tuple[str, str]:
    current = now or dt.datetime.now().astimezone()
    cutoff = current - dt.timedelta(days=14)
    recent = []
    for path, metadata, body in _receipts(root):
        created = metadata.get("created_at")
        try:
            stamp = dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()
        if cutoff <= stamp <= current and any(line.lstrip("-* ").startswith("CORRECTION:") for line in body.splitlines()):
            recent.append((path.stem, stamp.astimezone().date()))
    if not recent:
        return OK, "no recent corrections in receipts"
    companion = config.companion_dir({"vault": str(root)})
    rules = companion / "Rules.md" if companion else root / "Rules.md"
    changed = _run(["git", "log", "-1", "--format=%ct", "--", str(rules.relative_to(root))], root)
    if changed.returncode or not changed.stdout.strip():
        return WARNING, "recent corrections exist; Rules update could not be verified"
    committed = dt.datetime.fromtimestamp(int(changed.stdout.strip()), dt.timezone.utc).date()
    pending = [item for item, day in recent if committed < day]
    if pending:
        return WARNING, "recent correction may not be reflected in Rules: " + ", ".join(pending[:5])
    return OK, "Rules updated after recent corrections"


def check_pending_decisions(root: Path) -> tuple[str, str]:
    companion = config.companion_dir({"vault": str(root)})
    path = (companion or root) / "Decisions.md"
    if not path.exists():
        return OK, "no decisions file to check"
    text = path.read_text(encoding="utf-8-sig")
    known = {path.name for path in root.rglob("*.py") if ".git" not in path.parts}
    pending = []
    for match in re.finditer(r"(?ms)^## Decision:[ \t]*(.+?)[ \t]*$(.*?)(?=^## |\Z)", text):
        body = match.group(2)
        if re.search(r"\*\*Outcome:\*\*[ \t]*\S", body):
            continue
        note = " ".join(re.findall(r"\((?:Note |20\d\d-\d\d-\d\d)[^)]*\)", body))
        missing = sorted({name for name in re.findall(r"`(?:[^`\s]*/)?([\w-]+\.py)`", body)
                          if name not in known and name not in note})
        if missing:
            pending.append(f"{match.group(1)[:45]}: {', '.join(missing)}")
    return (WARNING, "; ".join(pending[:3])) if pending else (OK, "no unresolved decisions rely on missing scripts")


def check_retired_references(root: Path) -> tuple[str, str]:
    scripts = {path.name for path in (root / ".brain/scripts").glob("*.py")}
    instructions = [root / "AGENTS.md"]
    companion = config.companion_dir({"vault": str(root)})
    if companion:
        instructions.extend(companion.glob("*.md"))
    missing = []
    for path in instructions:
        if not path.is_file():
            continue
        for name in re.findall(r"`([\w-]+\.py)`", path.read_text(encoding="utf-8-sig")):
            if name not in scripts and not (root / name).exists():
                missing.append(name)
    return (WARNING, "possibly missing script references: " + ", ".join(sorted(set(missing))[:5])) if missing else (OK, "no missing script references found")


def _safe(name: str, function: Callable[[], tuple[str, str]]) -> Check:
    try:
        status, detail = function()
        if status not in STATUSES:
            raise ValueError(f"invalid status: {status}")
        return Check(name, status, _one_line(detail))
    except Exception as exc:
        return Check(name, ERROR, f"{type(exc).__name__}: {_one_line(exc)}")


def check_skills(root: Path, home: Path | None = None) -> tuple[str, str]:
    """Each skill in the hub (.brain/skills/) linked into the skill folder of every configured
    harness, and for agy the hub named in its skills.json."""
    import skills_hub
    cfg = config.load(root / ".brain" / "config.json")
    names = skills_hub.hub_skills(root)
    if not names:
        return WARNING, ".brain/skills/ holds no skill (run install.py again)"
    problems = skills_hub.problems(root, home or Path.home(), cfg["harnesses"])
    if problems:
        return WARNING, ("; ".join(problems[:4]) + (f"; {len(problems) - 4} more" if len(problems) > 4 else "")
                         + " (install.py links what is missing and names the hub in agy's skills.json; it "
                           "never replaces a link elsewhere or a folder of your own)")
    reach = ["named in agy's skills.json" if name == "agy" else f"linked in {name}" for name in cfg["harnesses"]]
    return OK, f".brain/skills/ ({', '.join(names)}) " + ", ".join(reach)


def run_checks(root: Path, now: dt.datetime | None = None, home: Path | None = None) -> list[Check]:
    return [
        _safe("Hooks", lambda: check_hooks(root, home)), _safe("Tools", lambda: check_tools(root)),
        _safe("Skills", lambda: check_skills(root, home)),
        _safe("Daily", lambda: check_daily(root, now)), _safe("Health", lambda: check_health(root, now)),
        _safe("Rules", lambda: check_rules(root)), _safe("Session context", lambda: check_session_context(root)),
        _safe("Tasks", lambda: check_tasks(root)), _safe("Recall index", lambda: check_recall_index(root)),
        _safe("Engine", lambda: check_engine(root)), _safe("Git", lambda: check_git(root)),
        _safe("Corrections", lambda: check_corrections(root, now)),
        _safe("Pending decisions", lambda: check_pending_decisions(root)),
        _safe("Retired references", lambda: check_retired_references(root)),
    ]


def report(checks: list[Check]) -> dict:
    status = ERROR if any(c.status == ERROR for c in checks) else WARNING if any(c.status == WARNING for c in checks) else OK
    return {"status": status, "checks": [c.__dict__ for c in checks]}


def write_health(root: Path, checks: list[Check]) -> None:
    # Its own file: health.json belongs to the nightly jobs (stage), whose warnings must survive.
    path = root / ".brain/.state/doctor.json"
    payload = {"ts": int(dt.datetime.now().timestamp()), "component": "doctor",
               "error": "; ".join(f"{c.name}: {c.detail}" for c in checks if c.status == ERROR),
               "warnings": [f"{c.name}: {c.detail}" for c in checks if c.status == WARNING]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--home", type=Path, help="Replaces the user home where the harness hook files and skill folders are.")
    args = parser.parse_args(argv)
    root = (args.vault or config.vault_path(config.load())).expanduser().resolve()
    checks = run_checks(root, home=args.home.expanduser().resolve() if args.home else None)
    if args.save:
        write_health(root, checks)
    if args.json:
        print(json.dumps(report(checks), ensure_ascii=False, indent=2))
    else:
        print("| Check | Status | Detail |\n| --- | --- | --- |")
        for check in checks:
            print(f"| {check.name} | {check.status} | {check.detail} |")
    return int(any(c.status == ERROR for c in checks))


if __name__ == "__main__":
    raise SystemExit(main())
