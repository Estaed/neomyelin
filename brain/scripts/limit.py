#!/usr/bin/env python3
"""Show current Claude, Codex and agy usage from live and local sources.

agy: `agy -p /usage --output-format json` answers without a model turn since agy 1.1.11 (its
changelog); an older agy would send `/usage` to the model and spend quota, so it is not run. Its
`command.data.groups[].buckets[]` hold `{id, name, window ("weekly"|"5h"), remaining_fraction,
reset_time}`. The call runs with its log file in a temporary folder that is deleted afterwards
(that log holds the account email) and is cached for AGY_CACHE_MIN minutes in the vault's
`.brain/.state/`. statusline.py never calls it (an agy that is logged out may open a login).
"""
from __future__ import annotations

import glob
import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_MAX_AGE_MIN = 45
CODEX_MAX_AGE_MIN = 45
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / ".state"
AGY_MIN_VERSION = (1, 1, 11)
AGY_CACHE = SCRIPT_DIR.parent / ".state" / "limit-agy.json"  # <vault>/.brain/.state/ when installed
AGY_CACHE_MIN = 5
AGY_TIMEOUT = 45  # seconds for the whole call; agy itself stops after --print-timeout 30s
AGY_SPANS = {"5h": "5 hour", "weekly": "weekly"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _now() -> float:
    return time.time()


def _parse_iso(value: str | None) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _fmt_delta(seconds: float) -> str:
    seconds = max(0, int(abs(seconds)))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days}d {hours}h" if days and hours else f"{days}d" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _home() -> Path:
    return Path.home()


def _claude_credentials() -> dict:
    try:
        return json.loads((_home() / ".claude" / ".credentials.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}


def _claude_token() -> str | None:
    oauth = _claude_credentials().get("claudeAiOauth") or {}
    expiry = oauth.get("expiresAt")
    if isinstance(expiry, (int, float)) and expiry / 1000 < _now():
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def _claude_plan() -> str | None:
    plan = (_claude_credentials().get("claudeAiOauth") or {}).get("subscriptionType")
    return plan if isinstance(plan, str) else None


def _fetch_claude_live_detailed(timeout: float = 15) -> tuple[dict | None, str | None]:
    token = _claude_token()
    if not token:
        return None, "Claude credentials unavailable or expired"
    request = urllib.request.Request(
        CLAUDE_USAGE_URL + "?cedar_ember=1&skip_spend=1&at_wall=1",
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
                 "User-Agent": "claude-cli/2.1.280 (external, cli)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        return None, f"usage endpoint returned HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"usage endpoint unavailable ({type(exc).__name__})"
    except (UnicodeError, ValueError):
        return None, "usage endpoint returned invalid JSON"


def _append_windows(out: dict, utilization: dict) -> None:
    names = (("five_hour", "5 hour"), ("seven_day", "7 day"))
    known = {key for key, _ in names}
    names += tuple((key, key.replace("_", " ")) for key in utilization if key not in known)
    for key, label in names:
        window = utilization.get(key)
        if not isinstance(window, dict) or window.get("utilization") is None:
            continue
        reset = _parse_iso(window.get("resets_at"))
        expired = reset is not None and reset < _now()
        out["windows"].append({"name": label, "percent": window.get("utilization"),
            "resets_at": window.get("resets_at"), "remaining": _fmt_delta(reset - _now()) if reset and not expired else None,
            "expired": expired})


def model_windows(usage: dict) -> list[dict]:
    output = []
    for item in usage.get("limits", []) if isinstance(usage, dict) else []:
        scope = item.get("scope", {}) if isinstance(item, dict) else {}
        model = scope.get("model", {}) if isinstance(scope, dict) else {}
        name, percent = model.get("display_name"), item.get("percent") if isinstance(item, dict) else None
        if not isinstance(name, str) or isinstance(percent, bool) or not isinstance(percent, (int, float)):
            continue
        span = "7 day" if item.get("group") == "weekly" else "5 hour" if item.get("group") == "session" else ""
        reset = item.get("resets_at")
        timestamp = _parse_iso(reset)
        output.append({"name": f"{name} {span}".strip(), "model": name, "span": span,
                       "percent": float(percent), "resets_at": reset, "remaining": _fmt_delta(timestamp - _now()) if timestamp and timestamp > _now() else None,
                       "expired": bool(timestamp and timestamp < _now())})
    return output


def statusline_model_windows(max_age_min: float = CLAUDE_MAX_AGE_MIN) -> list[dict]:
    path = Path(tempfile.gettempdir()) / "cc-sl-claude-models.cache"
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
        stamp = float(rows[0])
    except (OSError, ValueError, IndexError):
        return []
    if not 0 <= _now() - stamp <= max_age_min * 60:
        return []
    out = []
    for row in rows[1:]:
        parts = row.split("\t")
        if len(parts) >= 3:
            try:
                out.append({"name": parts[2], "model": parts[2], "span": parts[0][-2:],
                            "percent": float(parts[1]), "resets_at": None, "remaining": None, "expired": False})
            except ValueError:
                continue
    return out


def claude_banked(usage: dict) -> list[dict]:
    out = []
    cedar = usage.get("cedar_ember", {}) if isinstance(usage, dict) else {}
    for grant in cedar.get("grants", []) if isinstance(cedar, dict) else []:
        count = grant.get("resets_left") if isinstance(grant, dict) else None
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            out.append({"name": str(grant.get("label") or grant.get("id") or "reset"), "count": count,
                        "ends_at": _parse_iso(grant.get("ends_at")), "clears": grant.get("clears", [])})
    tide = usage.get("juniper_tide", {}) if isinstance(usage, dict) else {}
    if isinstance(tide, dict) and tide.get("available") is True:
        out.append({"name": "weekly reset credit", "count": 1, "ends_at": None, "clears": ["five_hour"]})
    return out


def read_statusline_claude(state_dir: Path | None = None, max_age_min: float = CLAUDE_MAX_AGE_MIN) -> dict | None:
    best = None
    for name in glob.glob(str((state_dir or STATE_DIR) / "session-budget-source.claude.*.json")):
        try:
            item = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stamp = item.get("epoch") if isinstance(item, dict) else None
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not 0 <= _now() - stamp <= max_age_min * 60:
            continue
        values = {key: item[key] for key in ("five", "seven") if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool) and 0 <= item[key] <= 100}
        if values and max(values.values()) > 0 and (best is None or stamp > best["epoch"]):
            best = {"epoch": stamp, **values, **{f"{key}_reset": item[f"{key}_reset"] for key in ("five", "seven") if isinstance(item.get(f"{key}_reset"), (int, float))}}
    return best


def read_claude() -> dict:
    live, reason = _fetch_claude_live_detailed()
    out = {"source": CLAUDE_USAGE_URL, "windows": [], "warnings": [], "plan": _claude_plan(), "age_minutes": None}
    if live:
        out["age_minutes"] = 0
        out["model_windows"] = model_windows(live)
        out["banked"] = claude_banked(live)
        _append_windows(out, live)
        if out["windows"]:
            return out
    observed = read_statusline_claude()
    if observed:
        out["source"] = "statusline observation"
        out["age_minutes"] = round((_now() - observed["epoch"]) / 60)
        out["model_windows"] = statusline_model_windows()
        usage = {}
        for key, name in (("five", "five_hour"), ("seven", "seven_day")):
            if key in observed:
                window = {"utilization": observed[key]}
                reset = observed.get(f"{key}_reset")
                if reset:
                    window["resets_at"] = datetime.fromtimestamp(reset, timezone.utc).isoformat()
                usage[name] = window
        out["warnings"].append(f"live source unavailable ({reason or 'unknown'}); using status line reading")
        _append_windows(out, usage)
        if out["windows"]:
            return out
    path = _home() / ".claude.json"
    out["source"] = str(path)
    out["model_windows"] = statusline_model_windows()
    out["warnings"].append(f"live source unavailable ({reason or 'unknown'}); checking local cache")
    try:
        cache = json.loads(path.read_text(encoding="utf-8")).get("cachedUsageUtilization") or {}
    except (OSError, ValueError, AttributeError):
        out["warnings"].append("usage unknown: local Claude cache unavailable")
        return out
    fetched = cache.get("fetchedAtMs")
    if isinstance(fetched, (int, float)):
        out["age_minutes"] = round((_now() - fetched / 1000) / 60)
        if out["age_minutes"] > CLAUDE_MAX_AGE_MIN:
            out["warnings"].append("local cache is stale")
    _append_windows(out, cache.get("utilization") or {})
    return out


def read_codexbar() -> dict | None:
    appdata = Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming"))
    path = appdata / "CodexBar" / "codex-accounts" / "snapshots.json"
    try:
        snapshots = json.loads(path.read_text(encoding="utf-8")).get("snapshots") or {}
    except (OSError, ValueError, AttributeError):
        return None
    rows = [(record, _parse_iso(record.get("updatedAt"))) for record in snapshots.values() if isinstance(record, dict)]
    rows = [(record, stamp) for record, stamp in rows if stamp is not None]
    if not rows:
        return None
    newest, stamp = max(rows, key=lambda row: row[1])
    valid = [(record, ts) for record, ts in rows if not record.get("limitReached")]
    weekly_source = max(valid, key=lambda row: row[1])[0] if valid else newest
    out = {"source": str(path), "windows": [], "warnings": [], "plan": newest.get("plan"), "age_minutes": round((_now() - stamp) / 60)}
    if out["age_minutes"] > CODEX_MAX_AGE_MIN:
        out["warnings"].append("CodexBar reading is stale")
    if newest.get("limitReached"):
        out["warnings"].append("primary limit reached")
    for key, label, source in (("primaryWindow", "primary", newest), ("secondaryWindow", "weekly", weekly_source)):
        window = source.get(key) or {}
        if not window:
            continue
        reset = _parse_iso(window.get("resetAt"))
        seconds = window.get("limitWindowSeconds") or 0
        span = f"{seconds // 86400} day" if seconds and seconds % 86400 == 0 else f"{seconds // 3600} hour" if seconds else ""
        out["windows"].append({"name": f"{label} ({span})".strip(), "percent": window.get("usedPercent"),
            "resets_at": window.get("resetAt"), "remaining": _fmt_delta(reset - _now()) if reset and reset > _now() else None,
            "expired": bool(reset and reset < _now())})
    return out if out["windows"] else None


def _fill_codex(out: dict, limits: dict, age: float) -> None:
    out["plan"] = limits.get("plan_type")
    out["age_minutes"] = round(age / 60)
    if age > 86400:
        out["warnings"].append("latest session reading is over a day old")
    for key, label in (("primary", "primary"), ("secondary", "secondary")):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        reset = window.get("resets_at")
        minutes = window.get("window_minutes") or 0
        span = f"{minutes // 1440} day" if minutes >= 1440 else f"{minutes // 60} hour" if minutes else ""
        out["windows"].append({"name": f"{label} ({span})".strip(), "percent": window.get("used_percent"),
            "resets_at": datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset else None,
            "remaining": _fmt_delta(reset - _now()) if reset and reset > _now() else None, "expired": bool(reset and reset < _now())})


def read_codex(scan_limit: int = 40) -> dict:
    fresh = read_codexbar()
    if fresh is not None:
        return fresh
    root = _home() / ".codex" / "sessions"
    out = {"source": str(root), "windows": [], "warnings": ["CodexBar unavailable; using session files"], "age_minutes": None}
    files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
    if not files:
        out["warnings"].append("usage unknown: no Codex session files found")
        return out
    fallback = None
    for path in files[:scan_limit]:
        candidate = None
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "rate_limits" not in line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                stack = [item]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        if isinstance(node.get("rate_limits"), dict):
                            candidate = node["rate_limits"]
                        stack.extend(node.values())
                    elif isinstance(node, list):
                        stack.extend(node)
        except OSError:
            continue
        if isinstance(candidate, dict):
            age = _now() - path.stat().st_mtime
            fallback = fallback or (candidate, age)
            if candidate.get("primary"):
                _fill_codex(out, candidate, age)
                return out
    if fallback:
        _fill_codex(out, *fallback)
    else:
        out["warnings"].append("usage unknown: no rate_limits record found")
    return out


def read_codex_banked(timeout: float = 10) -> tuple[list[dict], str | None]:
    exe = shutil.which("codex")
    if not exe:
        return [], "codex command unavailable"
    try:
        proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        return [], f"app-server unavailable ({type(exc).__name__})"
    answer = {}
    def receive():
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == 2:
                answer.update(message)
                break
    try:
        for message in ({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"quota-view","version":"1"}}},
                        {"jsonrpc":"2.0","method":"initialized"},
                        {"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read","params":{}}):
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        thread = threading.Thread(target=receive, daemon=True)
        thread.start(); thread.join(timeout)
    except (OSError, AttributeError) as exc:
        return [], f"app-server communication failed ({type(exc).__name__})"
    finally:
        proc.kill()
    result = answer.get("result")
    credits = result.get("rateLimitResetCredits", {}).get("credits", []) if isinstance(result, dict) else []
    out = [{"name": str(c.get("title") or c.get("id") or "reset"), "count": 1,
            "ends_at": c.get("expiresAt"), "clears": ["five_hour", "seven_day"]}
           for c in credits if isinstance(c, dict) and c.get("status") == "available"]
    return out, None if isinstance(result, dict) else "app-server returned no rate limits"


def find_agy() -> str | None:
    """The agy command: on PATH, else where its installers put it."""
    found = shutil.which("agy")
    if found:
        return found
    places = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        places.append(Path(local) / "agy" / "bin" / "agy.exe")
    places += [_home() / ".local" / "bin" / "agy", Path("/snap/bin/agy"), Path("/opt/homebrew/bin/agy"),
               Path("/usr/local/bin/agy")]
    return next((str(place) for place in places if place.is_file()), None)


def _agy_env() -> dict:
    # A session this call might start must not load NeoMyelin's memory (memory_context.py).
    return {**os.environ, "NEOMYELIN_INVOKED_BY": "limit"}


def agy_version(exe: str) -> tuple[int, ...] | None:
    try:
        result = subprocess.run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=15, env=_agy_env(), creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout.decode("utf-8", "replace"))
    return tuple(int(part) for part in match.groups()) if match and result.returncode == 0 else None


def _agy_usage(exe: str) -> tuple[list | None, str | None]:
    """(groups, None) from `agy -p /usage`, or (None, reason). Its answer goes to a file, not a
    pipe, so a child process agy leaves behind cannot hold the call open after a timeout; the
    folder with that file and agy's log is deleted afterwards. A snap agy has its own /tmp, so its
    folder goes under the snap's common folder, which both sides see (not measured)."""
    snap = config.agy_snap(exe)
    parent = _home() / "snap" / snap / "common" if snap else None
    try:
        if parent is not None:
            parent.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="neomyelin-agy-", dir=parent))
    except OSError as exc:
        return None, f"no temporary folder for agy's log ({type(exc).__name__})"
    answer = folder / "usage.json"
    try:
        try:
            with answer.open("wb") as out:
                process = subprocess.run(
                    [exe, "-p", "/usage", "--output-format", "json", "--print-timeout", "30s",
                     "--log-file", str(folder / "agy.log")],
                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL, timeout=AGY_TIMEOUT,
                    cwd=folder, env=_agy_env(), creationflags=NO_WINDOW)
            raw = answer.read_bytes()
        except subprocess.TimeoutExpired:
            return None, f"agy -p /usage gave no answer within {AGY_TIMEOUT}s"
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"agy -p /usage could not run ({type(exc).__name__})"
    finally:
        _remove_folder(folder)
    # Never echo agy's own output: when it is not logged in it may name the account.
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None, (f"agy -p /usage returned no JSON (exit {process.returncode}); "
                      "is agy logged in? Run agy once to sign in")
    if not isinstance(payload, dict):
        return None, "agy -p /usage returned an unexpected answer"
    if payload.get("status") != "SUCCESS":
        return None, f"agy -p /usage status is {str(payload.get('status'))[:40]!r}, not SUCCESS"
    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list) or not groups:
        return None, "agy -p /usage returned no quota groups"
    return groups, None


def _remove_folder(folder: Path) -> None:
    """Delete agy's temporary folder; its log holds the account email, so a folder that stays is
    said on stderr with its path."""
    for attempt in range(3):
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return
        time.sleep(0.5 * (attempt + 1))  # Windows: agy may still hold its log open for a moment
    print(f"limit: could not delete {folder}; delete it (agy's log in it names your account)", file=sys.stderr)


def agy_windows(groups: list) -> list[dict]:
    """One window per bucket; used % = (1 - remaining_fraction) * 100. A bucket with nothing used
    (remaining_fraction 1) is "not started": it shows no reset time."""
    out = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            span = AGY_SPANS.get(bucket.get("window"), str(bucket.get("window") or bucket.get("name") or "window"))
            fraction = bucket.get("remaining_fraction")
            valid = (isinstance(fraction, (int, float)) and not isinstance(fraction, bool)
                     and math.isfinite(fraction) and 0 <= fraction <= 1)
            reset = _parse_iso(bucket.get("reset_time"))
            not_started = valid and fraction == 1
            live = bool(valid and not not_started and reset and reset > _now())
            out.append({"name": f"{group.get('name') or 'agy'} {span}", "group": group.get("name"),
                        "span": span, "percent": round((1 - fraction) * 100, 1) if valid else None,
                        "resets_at": None if not_started else bucket.get("reset_time"),
                        "remaining": _fmt_delta(reset - _now()) if live else None,
                        "expired": bool(valid and not not_started and reset and reset <= _now()),
                        "not_started": not_started})
    return out


def read_agy(cache: Path | None = None) -> dict:
    """agy's quota windows, or unknown with the reason; never 0 for a reading that failed."""
    cache = cache or AGY_CACHE
    out = {"source": "agy -p /usage", "windows": [], "warnings": [], "version": None,
           "age_minutes": None, "status": "unknown", "reason": None}
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        age = _now() - float(cached["epoch"])
        if 0 <= age <= AGY_CACHE_MIN * 60 and isinstance(cached.get("groups"), list):
            out.update(source="agy -p /usage (cached)", version=cached.get("version"),
                       age_minutes=round(age / 60), status="ok", windows=agy_windows(cached["groups"]))
            return out
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        pass
    exe = find_agy()
    if exe is None:
        out["reason"] = "agy is not installed (not on PATH or in its usual folders)"
        return out
    version = agy_version(exe)
    if version is None:
        out["reason"] = "`agy --version` gave no version"
        return out
    out["version"] = ".".join(map(str, version))
    if version < AGY_MIN_VERSION:
        out["reason"] = (f"agy {out['version']} is older than 1.1.11, where /usage would spend a model "
                         "turn; update agy (agy update)")
        return out
    groups, reason = _agy_usage(exe)
    if groups is None:
        out["reason"] = reason
        return out
    out.update(status="ok", age_minutes=0, windows=agy_windows(groups))
    # Only the fields the windows need go to disk.
    slim = [{"name": group.get("name"),
             "buckets": [{key: bucket.get(key) for key in ("id", "name", "window", "remaining_fraction", "reset_time")}
                         for bucket in group.get("buckets") or [] if isinstance(bucket, dict)]}
            for group in groups if isinstance(group, dict)]
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_name(cache.name + ".tmp")
        temporary.write_text(json.dumps({"epoch": _now(), "version": out["version"], "groups": slim}),
                             encoding="utf-8", newline="\n")
        os.replace(temporary, cache)
    except OSError:
        pass  # no cache: the next run asks agy again
    return out


def _bar(percent: float | None, width: int = 20) -> str:
    if not isinstance(percent, (int, float)) or isinstance(percent, bool):
        return "?" * width
    filled = round(min(max(percent, 0), 100) / 100 * width)
    return "#" * filled + "." * (width - filled)


def render(claude: dict, codex: dict, color: bool = False, agy: dict | None = None) -> str:
    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text
    lines = []
    blocks = [("CLAUDE", claude), ("CODEX", codex)] + ([("AGY", agy)] if agy is not None else [])
    for title, block in blocks:
        if title == "AGY":
            what = f"agy {block['version']}" if block.get("version") else "agy"
        else:
            what = block.get('plan') or 'plan unknown'
        age = f", {block['age_minutes']} min old" if title == "AGY" and block.get("age_minutes") else ""
        lines.append(paint(f"{title}  {what}", "1") + f"  source: {block.get('source', 'unknown')}{age}")
        width = max([22] + [len(str(window.get("name", ""))) for window in block.get("windows", [])])
        for window in block.get("windows", []):
            value = window.get("percent")
            shown = f"{value:.0f}%" if isinstance(value, (int, float)) and not isinstance(value, bool) else "unknown"
            reset = ("  not started" if window.get("not_started")
                     else f"  resets in {window['remaining']}" if window.get("remaining") else "")
            tint = "31" if isinstance(value, (int, float)) and value >= 90 else "33" if isinstance(value, (int, float)) and value >= 70 else "32"
            lines.append(f"  {window['name']:<{width}} {paint(shown, tint):<8} [{_bar(value)}]{reset}")
        if not block.get("windows"):
            lines.append("  usage: unknown" + (f" ({block['reason']})" if block.get("reason") else ""))
        for credit in block.get("banked", []):
            lines.append(f"  banked reset: {credit['count']}x  {credit['name']}")
        for warning in block.get("warnings", []):
            lines.append(f"  ! {warning}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    config.force_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args(argv)
    claude, codex = read_claude(), read_codex()
    codex["banked"], reason = read_codex_banked()
    if reason:
        codex["warnings"].append(f"banked reset reading unavailable: {reason}")
    agy = read_agy()
    if args.json:
        print(json.dumps({"claude": claude, "codex": codex, "agy": agy}, ensure_ascii=False, indent=2))
    else:
        enabled = args.color == "always" or (args.color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ)
        print(render(claude, codex, color=enabled, agy=agy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
