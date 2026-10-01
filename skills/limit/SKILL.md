---
name: limit
description: Show Claude, Codex and Antigravity (agy) usage windows and quota sources when asked about quota or limits.
allowed-tools: Bash({script:limit.py} *) PowerShell({script:limit.py} *)
---

# Limit

Run `{script:limit.py} --color always` (it works from any folder) and show its output before
summarizing it. It has three sections:

- **CLAUDE**: the 5-hour and 7-day windows and any model-specific window, from Claude's usage
  endpoint with the local Claude login; banked reset credits.
- **CODEX**: the primary and weekly windows, from CodexBar or the newest Codex session file;
  banked reset credits from `codex app-server`.
- **AGY**: each Antigravity quota group (for example "Gemini Models", "Claude and GPT models")
  with its 5-hour and weekly window, from `agy -p /usage`, which answers without a model turn on
  agy 1.1.11 and newer. Used % is the part of the window spent; "not started" means the window
  has not been touched, so it has no reset time yet. The reading is cached for five minutes.
  When agy is not installed, older than 1.1.11, not logged in or fails, AGY says unknown and why.

Treat a stale, expired, unknown, or unavailable reading as unknown; do not use it as a current
quota value. When asked whether work can be delegated, use the fullest valid pool window.

Model-specific windows apply only to that model. They do not reduce the shared pool reading.
If a model-specific window cannot be read, say that it is unknown rather than assuming it does
not exist.

Banked reset credits are displayed for information. They are used manually and this script never
spends one. Do not recommend spending a credit unless the user asks for a quota recommendation;
when discussing it, explain that the reset action itself requires the user's choice in the relevant
service interface.

If a source cannot be read, report that limitation as part of the answer. HTTP 429 means the
usage endpoint could not provide a reading; it does not mean the quota is exhausted. Do not retry
repeatedly. A fresh status line observation, the local Claude usage cache, CodexBar, and Codex
session files are fallback sources and must be identified as such.

<!-- neomyelin:skill -->
