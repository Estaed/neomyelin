#!/usr/bin/env python3
"""Render Claude Code's two-line status display and cache its quota observation."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
ESC = "\033"
RESET, DIM, BOLD = ESC + "[0m", ESC + "[2m", ESC + "[1m"
GREEN, YELLOW, RED, MAGENTA = ESC + "[32m", ESC + "[33m", ESC + "[31m", ESC + "[35m"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(argv: list[str], timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.decode("utf-8", "replace").splitlines()[0].strip() if result.returncode == 0 and result.stdout.strip() else ""


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def clean(value, default: str = "") -> str:
    if value is None or value is False:
        return default
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return re.sub(r"[\r\n\t]", " ", value) or default


def floor_pct(node, *path, default: int = -1) -> int:
    value = node
    for key in path:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return math.floor(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


def reset_epoch(payload: dict, window: str):
    try:
        value = payload.get("rate_limits", {}).get(window, {}).get("resets_at")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return value / 1000 if value > 1e12 else value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        pass
    return None


def write_lines(path: Path, values) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(str(v) for v in values) + "\n", encoding="utf-8", newline="\n")
    except OSError:
        pass


def cache_claude(payload: dict, sid: str, now: float) -> None:
    if not sid or sid == "nosess":
        return
    path = SCRIPT_DIR / ".state" / ("session-budget-source.claude." + hashlib.sha256(sid.encode()).hexdigest() + ".json")
    five = floor_pct(payload, "rate_limits", "five_hour", "used_percentage")
    seven = floor_pct(payload, "rate_limits", "seven_day", "used_percentage")
    data = {"epoch": int(now), "five": five if five >= 0 else None, "seven": seven if seven >= 0 else None,
            "five_reset": reset_epoch(payload, "five_hour"), "seven_reset": reset_epoch(payload, "seven_day")}
    old = read_json(path) or {}
    if now - mtime(path) < 15 and all(old.get(key) == data[key] for key in ("five", "seven", "five_reset", "seven_reset")):
        return
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        temp.write_text(json.dumps(data) + "\n", encoding="utf-8", newline="\n")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


def short_name(model: str) -> str:
    word = re.sub("[^a-z]", "", model.lower()) or "m"
    consonants = [char for char in word[1:] if char not in "aeiou"]
    return word[0] + (consonants[0] if consonants else word[1:2])


def usage_color(value: int) -> str:
    return RED if value >= 90 else YELLOW if value >= 70 else GREEN


def build(payload: dict) -> str:
    now = time.time()
    section = lambda key: payload.get(key) if isinstance(payload.get(key), dict) else {}
    model = clean(section("model").get("display_name"), "?")
    ctx = floor_pct(payload, "context_window", "used_percentage", default=0)
    effort = clean(section("effort").get("level"))
    think = section("thinking").get("enabled") is True
    five = floor_pct(payload, "rate_limits", "five_hour", "used_percentage")
    seven = floor_pct(payload, "rate_limits", "seven_day", "used_percentage")
    sid = clean(payload.get("session_id"), "nosess")
    cache_claude(payload, sid, now)
    tmp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    cache = tmp / ("cc-sl-" + re.sub(r"[^A-Za-z0-9]", "_", sid) + ".cache")
    cached = []
    if now - mtime(cache) < 5:
        try:
            cached = cache.read_text(encoding="utf-8").splitlines()
        except OSError:
            pass
    if len(cached) >= 5:
        branch, dirty, badge, sync, approvals = cached[:5]
    else:
        cwd = clean(section("workspace").get("current_dir")) or clean(payload.get("cwd")) or "."
        branch = run(["git", "-C", cwd, "symbolic-ref", "--short", "HEAD"]) or run(["git", "-C", cwd, "rev-parse", "--short", "HEAD"])
        dirty = "*" if run(["git", "-C", cwd, "status", "--porcelain", "--untracked-files=no"], 5) else ""
        root = run(["git", "-C", cwd, "rev-parse", "--show-toplevel"]) or cwd
        badge, sync, approvals = "", "", "0"
        badge_cmd = os.environ.get("SL_BADGE_CMD")
        if badge_cmd:
            try:
                result = subprocess.run(badge_cmd, shell=True, cwd=root, capture_output=True, timeout=3, creationflags=NO_WINDOW)
                badge = re.sub(r"[\t\r\n]", " ", result.stdout.decode("utf-8", "replace").splitlines()[0]) if result.stdout else ""
            except (OSError, subprocess.SubprocessError, IndexError):
                badge = ""
        write_lines(cache, [branch, dirty, badge, sync, approvals])
    branch = branch if len(branch) <= 24 else branch[:23] + "…"
    ctx = max(0, min(ctx, 100))
    ctx_color = RED if ctx >= 80 else YELLOW if ctx >= 50 else GREEN
    filled = round(ctx * 8 / 100)
    bar = "▰" * filled + "▱" * (8 - filled)

    # The harness payload is the freshest Claude reading; it is also limit.py's fallback source.
    codex_cache = tmp / "cc-sl-codex.cache"
    try:
        cx, cxw, cxage = map(int, codex_cache.read_text(encoding="utf-8").splitlines()[:3]) if now - mtime(codex_cache) < 30 else (-1, -1, -1)
    except (OSError, ValueError):
        cx, cxw, cxage = -1, -1, -1
    if cx < 0:
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            from limit import read_codex
            block = read_codex()
            windows = block.get("windows", [])
            if windows and isinstance(windows[0].get("percent"), (int, float)):
                cx = math.floor(windows[0]["percent"])
            if len(windows) > 1 and isinstance(windows[1].get("percent"), (int, float)):
                cxw = math.floor(windows[1]["percent"])
            cxage = block.get("age_minutes") if isinstance(block.get("age_minutes"), int) else -1
            write_lines(codex_cache, [cx, cxw, cxage])
        except Exception:
            pass

    model_cache = tmp / "cc-sl-claude-models.cache"
    models, stamp = [], 0
    try:
        rows = model_cache.read_text(encoding="utf-8").splitlines()
        stamp = int(rows[0])
        for row in rows[1:]:
            bits = row.split("\t")
            if len(bits) >= 3:
                models.append((bits[0], int(bits[1]), bits[2]))
    except (OSError, ValueError, IndexError):
        pass
    if now - mtime(model_cache) >= 300:
        try:
            from limit import _fetch_claude_live_detailed, model_windows
            live, _ = _fetch_claude_live_detailed(timeout=2)
            if live:
                stamp = int(now)
                models = [(short_name(item["model"]) + item["span"], min(max(math.floor(item["percent"]), 0), 999), item["name"])
                          for item in model_windows(live) if not item["expired"]]
        except Exception:
            pass
        write_lines(model_cache, [stamp] + [f"{label}\t{pct}\t{name}" for label, pct, name in models])

    line1 = BOLD + model + RESET
    if effort:
        line1 += f" {DIM}·{RESET} {effort}"
    if think:
        line1 += f" {DIM}·{RESET} {MAGENTA}think{RESET}"
    if five >= 0:
        line1 += f"   {DIM}cl5h{RESET} {usage_color(five)}{five}%{RESET}"
    if seven >= 0:
        line1 += f" {DIM}· cl7d{RESET} {usage_color(seven)}{seven}%{RESET}"
    stale_model = "?" if now - stamp > 1800 else ""
    for label, pct, _ in models:
        line1 += f" {DIM}· {label}{RESET} {usage_color(pct)}{pct}%{stale_model}{RESET}"
    if cx >= 0:
        stale = "?" if cxage > 15 else ""
        line1 += f" {DIM}· cx5h{RESET} {usage_color(cx)}{cx}%{stale}{RESET}"
        if cxw >= 0:
            line1 += f" {DIM}· cx7d{RESET} {usage_color(cxw)}{cxw}%{stale}{RESET}"

    line2 = f"{ctx_color}{bar}{RESET} {ctx_color}{ctx}%{RESET}"
    if branch:
        line2 += f"  {DIM}⎇{RESET} {branch}{YELLOW + '*' + RESET if dirty else ''}"
    worktree = clean(section("workspace").get("git_worktree"))
    if worktree:
        line2 += f" {DIM}⑂{worktree}{RESET}"
    if badge:
        line2 += f"  {GREEN}●{RESET} {badge}"
    try:
        if int(approvals) > 0:
            line2 += f"  {RED}⚑{approvals}{RESET}"
    except ValueError:
        pass
    if sync == "live":
        line2 += f"  {GREEN}●sync{RESET}"
    elif sync == "stale":
        line2 += f"  {DIM}○sync{RESET}"
    return line1 + "\n" + line2


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    sys.stdout.buffer.write((build(payload) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
