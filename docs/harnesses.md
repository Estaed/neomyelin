# How NeoMyelin's hooks run in Claude Code, Codex and agy

The reference for what `install.py` (through `brain/scripts/render_hooks.py`) registers in each
harness, how each hook runs, what a user does once per harness, how the skills and house rules in
the vault's `.brain/` reach each harness (§8), the file formats of the engine (`brain.py`) the
hooks read and write, and how `limit` reads agy's quota (§9). Measured on Windows 11 unless a line
says WSL.

## 1. Versions measured (2026-10-01)

| Tool | Version |
|---|---|
| Python | 3.13.5 (3.11+ required; nothing was tested on an older Python) |
| Claude Code | 2.1.286 |
| Codex CLI | 0.159.2 |
| Antigravity `agy` | 1.2.11 (1.2.14 read for its documented rule folders; its `skills.json` and `-p /usage` measured on 1.2.14, 2026-10-02) |
| Ollama (optional) | 0.35.0 with `bge-m3:latest` |

Real-turn evidence comes from `scripts/smoke_harness.py claude|codex|agy <vault>`: one headless
turn whose reply holds a random token only if the session block reached the model. It passed in
all three harnesses at `--scope project`; per-prompt recall passed a real Claude turn.

## 2. Hook files and events

| Harness | User scope (default) | `--scope project` |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | `<vault>/.claude/settings.json` (`settings.local.json` stays the user's) |
| Codex | `~/.codex/hooks.json` | `<vault>/.codex/hooks.json` |
| agy | `~/.gemini/config/hooks.json` | `<vault>/.agents/hooks.json` |

User scope makes memory load in every folder; project scope is for a machine whose user-level
hooks belong to another brain. In every file our entries sit next to whatever else it holds.

| Event | Claude Code (matcher) | Codex | agy |
|---|---|---|---|
| SessionStart | `memory_context.py --session-start` | same | — (PreInvocation below) |
| UserPromptSubmit | `prompt_recall.py --prompt-submit`; `receipt_gate.py --event UserPromptSubmit` | same two | no event used |
| PostToolUse | `receipt_gate.py --event PostToolUse` (`Edit\|Write\|MultiEdit\|NotebookEdit\|apply_patch`) | same | no event used |
| PreToolUse | `git_gate.py --pre-tool-use` (`Bash\|PowerShell`); `heredoc_gate.py --pre-tool-use` (`Bash`) | same two, no matcher | no event used |
| Stop | `receipt_gate.py --event Stop` | same | `receipt_gate.py --harness agy --event Stop` |
| PreInvocation | — | — | `.brain/hooks/agy_hook.py` |

- **Claude Code** also gets `env.PYTHONUTF8=1` in its settings (on Windows Python would otherwise
  use the ANSI code page) and, with `install.py --statusline`, a `statusLine` running
  `statusline.py`. Nothing else in the file changes.
- **Codex** output is the Claude shape (`hookSpecificOutput.additionalContext`), so it runs the
  same scripts. Its shell tool name is not measured, so the gates get every tool call without a
  matcher and read the `command` (a string or an argv list) themselves. PreToolUse sending
  `tool_name`/`tool_input.command`, PostToolUse with the `apply_patch` matcher and Stop's
  `{"decision": "block", "reason": ...}` are not measured in a real Codex turn here.
- **agy** reads named hooks: `{"<name>": {"<Event>": [handlers]}}`; ours live under
  `"neomyelin"`. The file location comes from the agy 1.2.11 binary, which names
  `~/.gemini/config/hooks.json` and `.agents/hooks.json` and logs "loaded %d named hooks from %d
  hooks.json file(s)"; `agy changelog` calls `~/.gemini/config/hooks.json` the shared file.
  PreInvocation runs before every model call: `agy_hook.py` treats `invocationNum == 0` as the
  session start and answers `{"injectSteps": [{"ephemeralMessage": text}]}`, else `{}`. It passes
  `conversationId` on as `session_id`.

An entry is ours only when its command ends with one of our forms from this vault's `.brain/`
(the script with exactly our arguments, or our shim), so a hook running the same script without
our arguments (another brain's, or the user's own) is never removed. An unchanged file is never
rewritten; a changed one is first copied next to itself as `<name>.neomyelin-<stamp>.bak` and
keeps its line endings and BOM.

### Where a harness lacks the event, nothing is faked

- **agy, per-prompt recall: not registered.** PreInvocation and Stop are not known to carry the
  user's prompt, and a later PreInvocation cannot be told apart from a tool-loop step. agy users
  get recall by asking (`recall.py "<question>"`).
- **agy, PreToolUse gates: not registered.** The binary names `PreToolUse`, but its payload and
  deny shape are neither documented nor measured.
- **agy, receipt reminder: idle turns only.** Stop fires after every agent loop; only a Stop with
  `fullyIdle: true` counts as a turn, and agy has no edit event this layer uses, so the rule is 2
  idle turns without a receipt. agy's Stop output has no verified way to show text, so the
  reminder is queued and `agy_hook.py` shows it once through PreInvocation's `injectSteps` at the
  start of the next turn.

## 3. Command form and the Windows `.cmd` shims

- **Claude Code:** `"<python>" "<vault>/.brain/scripts/<script>.py" <args>` (absolute, forward
  slashes, double quotes). Claude Code 2.1.286 runs hooks through Git Bash on Windows; the form
  also runs under cmd. Under PowerShell a quoted first token would be printed, not run.
- **Codex and agy on Windows:** Codex starts a hook command without a shell and hands it to
  cmd.exe unquoted, so a path with a space breaks at the space (measured on Codex: a project path
  with spaces failed with `'<drive>:/Charles' is not recognized`; 8.3 short names were off on that
  drive, so a short path is no escape). Both harnesses therefore run a generated shim,
  `<vault>/.brain/hooks/<harness>-<event>[-<name>].cmd`, one per hook. The shim holds the quoted
  Python path, sets `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`, and is written with CRLF in ASCII
  (or the console's OEM code page for a non-ASCII Python path, which cmd.exe reads that way).
  A vault path with whitespace makes `install.py` skip both harnesses with one line. When the
  Python it was written with is gone, the session shim prints a visible warning to run install.py
  again and every other shim exits quietly.
- **Codex and agy elsewhere:** a plain `python script args` command (`shlex`-quoted).
- **Exit codes:** every hook exits 0; a bad call exits 1, never 2, because exit code 2 from a
  UserPromptSubmit, PreToolUse or Stop hook makes Claude Code block the prompt, the tool call or
  the stop. A gate that fails lets the call through.

## 4. One-time trust and approval steps

| Harness | What the user does once |
|---|---|
| Claude Code | User scope: nothing. Project scope: accept Claude's folder-trust prompt in the vault. |
| Codex | Approve the NeoMyelin hooks once in `/hooks` in an interactive session, and again after an install that changed `hooks.json`. Until then Codex skips them silently (it still shows "Completed"). `codex exec` has no `/hooks`, so `smoke_harness.py codex` passes `--dangerously-bypass-hook-trust` for its one turn. |
| agy | User scope (the default install): nothing; if agy asks to trust the vault folder, accept. Project scope: trust the vault folder when agy asks (workspace hooks load only after trust, agy 1.2.11 changelog). README and INSTALL.md say the same. |

Open:
- Codex `[features] hooks = true`: the Codex smoke run passed with the vault trusted in
  `~/.codex/config.toml` and that line set in the vault's `.codex/config.toml`. `install.py`
  writes no `config.toml`. If a Codex session shows no `[Memory: ...]` block after the hooks
  were approved, check that line first.
- agy headless permissions: one probe (`agy -p "/hooks"` with a sandboxed home) printed `a tool
  required the "command" permission that headless mode cannot prompt for, so it was auto-denied`;
  whether the hook or a model tool asked is not known. If `smoke_harness.py agy` fails with that
  line, the fix is an allow rule (`permissions.allow`, `command(<shim path>)`) in agy's settings.

## 5. agy as a Linux snap

Measured in WSL, 2026-10-01: an agy installed as a snap (`/snap/bin/agy` links to the snap) runs
with HOME moved to `~/snap/<snap>/common`, so it reads `~/snap/<snap>/common/.gemini` and never
`~/.gemini`. It loads that folder's hook file, but the snap's confinement hides the system Python
(`/usr/bin/python3: not found`), so every hook would fail. `install.py` therefore skips agy hooks
for a snap with one line saying so, and still writes the instruction block into the snap's
`GEMINI.md`, which agy read there. Installing agy outside snap gives it the session memory too.

## 6. What each per-turn hook decides

- **Recall** (`prompt_recall.py`) searches with recall's engine without waiting for indexing (a
  detached `recall.py --update` at most every 5 minutes), within an 8-second budget. With Ollama:
  a 0.48 floor on the session's first prompt and 0.58 later (not measured). With BM25: a strict
  rule (2+ shared words, relative-idf weight >= 0.30; the threshold is Avenox V3's calibration,
  not re-measured here), with words found in more than half of the notes dropped instead of a
  stopword list, so it holds in any language. Only the first prompt says "nothing found" or
  "could not run". Ollama is reached at `http://127.0.0.1:11434/api/embed`; `127.0.0.1`, not
  `localhost`, which resolves to IPv6 first on Windows.
- **Receipt reminder** (`receipt_gate.py`): 3+ edits or 2+ human prompts and no receipt in
  `receipts/` whose frontmatter carries this session's value (`sha256(session_id)[:24]`, the
  value `[Memory: Session]` prints). Once per session, in every harness and folder; `[no-record]`
  in a prompt opts the session out. It names `brain.py receipt --file <json> --harness
  claude|codex|agy`.
- **Nightly run:** `memory_context.py --session-start` checks the local `nightly_at` (default
  `21:00`) against `.brain/.state/nightly.last-run`. Once due, it detaches `nightly.py` and returns
  the context without waiting; a launch marker and `nightly.lock` stop overlapping starts. Each
  step logs one result to `.brain/.state/nightly.log`. A finished run writes the stamp even if a
  step failed, so the next session does not repeat a partly failed night.
- **Child sessions:** `NEOMYELIN_INVOKED_BY` in the environment of a model CLI the layer starts
  (`engine.ask`: `claude -p`, `codex exec` or `agy -p`) keeps that session from loading memory or
  starting another nightly run.

## 7. The engine's file formats

`engine/brain.py`, installed as `<vault>/brain.py`, writes plain Markdown files with JSON
frontmatter between `---` lines, UTF-8 with LF line endings. The hooks read these files directly.

| Command | Writes | Format |
|---|---|---|
| `receipt --file <json\|-> [--harness claude\|codex\|agy\|manual]` | `receipts/<sha256(event_id)>.md`, then `daily/` | frontmatter `kind, event_id, harness, refs, visibility, created_at[, session]`, the summary as body; refs must be existing vault files; secrets masked as `[REDACTED]`; the same event again writes nothing, another summary under it is refused |
| `task-create`, `task-update` (with `expected_revision`) | `tasks/<id>.md` | frontmatter `id, title, status, owner, project, next_action, due_at, kind, revision`; a stale revision is refused (`RevisionConflict`) |
| `history <tasks/<id>.md or id>` | nothing | `[{sequence, event_type, record_id, revision, source, at, record}]` from `.brain/.state/engine/history/<id>.jsonl`, plus the file as it is now when it changed after the last entry |
| `sync` | `daily/<local day>.md` | `{"generated": true, "kind": "receipt-index"}`, `# Daily: <day>`, `### HH:MM — [Project] title · Local PC` (or `· Manual`), the body, `[[receipts/<file>\|receipt]]`; a daily note without `"generated": true` is never overwritten |
| `doctor` | nothing | `{status, receipts, tasks, problems}`; `.brain/scripts/doctor.py` shows it as its Engine row |

Recall indexes `receipts/` directly and never `daily/` (generated views of the same receipts);
any file whose frontmatter has `"generated": true` is skipped.

## 8. Skills and house rules live in the vault

The vault's `.brain/` is the one source of what NeoMyelin gives a harness; the harness folders
hold a link to it or a copy generated from it (`brain/scripts/skills_hub.py`,
`brain/scripts/render_instructions.py`).

### Skills

| Harness | How it reaches `.brain/skills/` |
|---|---|
| Claude Code | a link per skill in `~/.claude/skills/` (`install.skill_targets`) |
| Codex | a link per skill in `~/.codex/skills/` (`install.skill_targets`) |
| agy | no link: one entry `{"path": "<vault>/.brain/skills"}` in `~/.gemini/config/skills.json` (the snap's `.gemini` for an agy snap, §5) |

agy (measured 2026-10-02, agy 1.2.14 on Windows): it does not read `~/.gemini/antigravity/skills/`,
where a pre-release build linked its skills. With a probe skill in that folder and another in
`~/.gemini/config/skills/`, `agy -p /skills --output-format json` (a listing, no model turn; run
without Git Bash, which turns `/skills` into a path) named only the one in
`~/.gemini/config/skills/`. Besides that folder, agy reads every folder named in
`~/.gemini/config/skills.json` (`{"entries": [{"path": "<dir>"}], ...}`, each folder scanned one
level deep; agy's bundled guide `~/.gemini/antigravity-cli/builtin/skills/agy-customizations/docs/json_configs.md`).
With a temporary `skills.json` naming three probe hubs, the listing named all three skills: an
absolute path written with forward slashes, one with backslashes, and one with a space in it, each
a `.brain/skills` folder. Then a vault installed by `install.py` had its entry written into the
real `~/.gemini/config/skills.json` by `skills_hub.register_agy` (and taken out again by
`unregister_agy`, the folder compared before and after): the listing named the hub's `limit` at
`<vault>\.brain\skills\limit\SKILL.md`. So install names the hub there instead of linking:
- Install adds `{"path": "<vault>/.brain/skills"}` (forward slashes) to `entries`, keeping every
  other key and entry, after a backup next to the file (`skills.json.neomyelin-<stamp>.bak`); an
  entry already naming the hub, in any form, means nothing is written. Another NeoMyelin vault's
  entry (only a `path`, naming a `.brain/skills` whose `.brain/config.json` is valid, or that is
  gone) is replaced, as its links are, so agy does not list `limit` twice. A file that is not a
  JSON object, or whose `entries` is not a list, stops install before its first write.
- Uninstall removes the entries that name the hub and hold nothing else (one with `exclude` or
  `include_only` is the user's). When install's backup holds exactly what is left, the file gets
  those bytes back, so a file the user had is byte-identical to before install; when nothing is
  left and no backup holds that, install created the file and it is removed.
- NeoMyelin's links and copies in `~/.gemini/antigravity/skills/` (from a pre-release build) are removed by the
  next install and by uninstall; anything else in that folder stays.
- agy's own skills in `~/.gemini/config/skills/` are not adopted: agy reads them where they are,
  and linking them back would show them twice. A skill there with a hub skill's name shows twice in
  agy's list.
- Not measured: whether an agy installed as a snap can read a hub outside the snap's folder.

- `.brain/skills/<name>/` (a folder holding `SKILL.md`) is linked as `<harness folder>/<name>`
  for Claude Code and Codex when configured: a directory junction on Windows (no admin rights or
  developer mode, unlike a symlink), a symlink elsewhere. Where neither can be made, a copy with a
  `.neomyelin-copy` file naming the vault, refreshed when the hub changes; the report says so.
- Windows facts the code relies on (Python 3.13.5, measured 2026-10-02): `os.path.islink` is False
  for a junction while `os.readlink` returns its target with a `\\?\` prefix; a junction whose
  target is gone still `lexists` but does not `exists`; `os.unlink` removes the junction and
  leaves the target; a junction can be renamed; `os.scandir` reports a junction as a directory
  even with `follow_symlinks=False`, so nothing here walks a skill tree without checking each
  entry for a link first.
- A name already taken is never replaced when it is a link somewhere else or a folder of the
  user's own. The exceptions are NeoMyelin's own: a link into another NeoMyelin vault's
  `.brain/skills/` (that `.brain/config.json` is valid) or into a `.brain/skills/` folder that is
  gone, another vault's marked copy, and the plain `limit` copy installs made before the hub.
  These move to this vault, as the instruction block does when another vault installs.
- `install.py --adopt-skills` moves each real skill folder (holds `SKILL.md`, not a link, name not
  starting with a dot) of a configured harness into the hub: a copy to
  `.brain/.backup/skills-<YYYYMMDD-HHMMSS>/<harness>/<name>/` and to the hub, both compared byte
  for byte with the original, then the original is renamed aside, the link made in its place, and
  the renamed folder deleted; if the link fails, the folder is renamed back. `.brain/skills.json`
  records `{"<name>": {"harnesses": [...], "origins": {"<harness>": "<original path>"}}}`. One name
  in two harnesses with identical content is adopted once and linked to both; different content,
  or content that differs from the hub's skill of that name (a user's own `limit`), is not adopted.
- `uninstall.py` removes each link into this vault's hub with `os.unlink` (never a recursive
  delete, which through a junction would empty the hub folder) and checks the hub folder is still
  there, removes this vault's copies, and copies each adopted skill back to its recorded origin as
  a real folder. The vault, `.brain/skills/` and the backups included, stays as it is.
- `doctor.py` (row Skills) warns when a hub skill is not linked in a configured harness, a copy is
  out of date, a link is broken or points elsewhere, or (agy) `skills.json` does not name the hub.
  Its Hooks row checks that each configured harness whose command is installed has every hook of
  this vault in its user-level hook file (or, after `--scope project`, in the vault's own).
- A shipped skill's `SKILL.md` names a script as `{script:<name>.py}`; install writes it into the
  hub as the launcher and the script's absolute path (`py -3 "<vault>/.brain/scripts/limit.py"`
  on Windows, `python3 <vault>/.brain/scripts/limit.py` elsewhere), so `limit` runs from any
  working folder in all three harnesses, and Claude's `allowed-tools` line matches that command.

### House rules (the instruction block)

The text lives in `.brain/instructions/<harness>.md`. What each harness's user-level file holds
between NeoMyelin's markers depends on whether the harness loads `@` imports there:

| Harness | Loads `@<path>` from its user-level file | Block |
|---|---|---|
| Claude Code 2.1.286 | yes, absolute path; a space must be escaped as `\ ` | the import line `@<vault>/.brain/instructions/claude.md` |
| Codex | no import mechanism | a copy of `codex.md` |
| agy 1.2.14 | no (measured below) | a copy of `agy.md` |

Measured 2026-10-02 on this Windows machine:
- **agy:** `~/.gemini/GEMINI.md` was replaced for one headless turn each (`agy -p ... --output-format
  stream-json`, the original restored byte for byte afterwards and its sha256 checked) by a control
  token line plus one `@` line pointing at a file holding a second token, in three forms: absolute
  with forward slashes, absolute with backslashes, and relative to `~/.gemini`. Asked to print
  every token from its rules without using tools, agy printed the control token every time and the
  imported token never, with no tool call in the stream. So agy 1.2.14 loads GEMINI.md and does
  not expand `@` imports; its block is a copy.
- **Claude Code:** a `CLAUDE.md` with a control token and three imports (a path with a plain
  space, the same path with the space escaped as `\ `, a path without spaces) gave the control
  token and the two imported tokens of the escaped and the space-free paths, not the unescaped
  one. Measured with a project `CLAUDE.md` in a temporary folder; the user-level file uses the same
  import syntax (an absolute `@` import in `~/.claude/CLAUDE.md` loads in every session on this
  machine). A vault path with any whitespace other than a space gets a copy instead.

## 9. Quota (`limit`)

`.brain/scripts/limit.py` shows three sections, CLAUDE, CODEX and AGY (`--json`: keys `claude`,
`codex`, `agy`). AGY (measured 2026-10-02, agy 1.2.14 on Windows):

- agy is found on PATH, else in `%LOCALAPPDATA%/agy/bin/agy.exe`, `~/.local/bin/agy`,
  `/snap/bin/agy`, `/opt/homebrew/bin/agy`, `/usr/local/bin/agy`. It is asked only when
  `agy --version` is 1.1.11 or newer: its changelog says `-p "/usage"` answers without an agent
  turn since 1.1.11, and an older agy would send `/usage` to the model and spend quota.
- `agy -p /usage --output-format json --print-timeout 30s --log-file <temp>/agy.log`, with stdin
  closed, a 45-second limit and the temporary folder as working folder, deleted afterwards (the
  log holds the account email; for an agy snap, whose `/tmp` is its own, that folder is made under
  `~/snap/<snap>/common/`, not measured). Measured: `status` `SUCCESS`, `num_turns` 0, in about
  7 seconds; `command.data.groups[]` ("Gemini Models", "Claude and GPT models") each with
  `buckets[]` of `{id, name, window ("weekly"|"5h"), remaining_fraction, reset_time}`.
- Used % is `(1 - remaining_fraction) * 100`. A window with `remaining_fraction` 1 shows "not
  started" instead of a reset time. The groups (only those fields) are cached for five minutes in
  `.brain/.state/limit-agy.json`.
- agy missing, too old, no version, a timeout, output that is not JSON (not logged in), a status
  other than `SUCCESS` or no groups: the section says `usage: unknown (<reason>)`, never 0%.
- `statusline.py` never calls agy: a logged-out agy may open a sign-in.
