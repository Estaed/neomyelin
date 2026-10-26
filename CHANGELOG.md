# Changelog

Every NeoMyelin release, newest first: what was added, changed or fixed, and when. The format
follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and the numbers follow
[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html), so the number tells you how
heavy an update is:

- **PATCH** (1.2.0 → 1.2.1): fixes only; nothing you use works differently.
- **MINOR** (1.2.0 → 1.3.0): something new, or something that now works differently; your vault
  keeps working as it is.
- **MAJOR** (1.2.0 → 2.0.0): your vault's format changes; the line that does it is marked
  **BREAKING**, and that release's notes say how to move your vault.
- **1.0.1 and 1.0.2** were numbered before these rules were checked: both also added features,
  so today they would be MINOR releases.

A date is the day GitHub published the release (UTC). Each release's page on
https://github.com/Estaed/neomyelin/releases also says how to update, how to go back and what was
tested.

## [Unreleased]

## [1.3.0] - 2026-10-26

### Added

- **Open projects at session start:** every session, in any folder, lists your open project
  cards (`status: active` or `waiting`, or no status) with a one-line summary, so the assistant
  checks what you already have before proposing a new project. Cards take an optional `summary:`
  line; `done` and `archived` cards stay out. The list gives way first when the session context
  is full, before your rules or the brain's alarms.
- **Line endings guard:** a `git commit` that would store a file with other line endings than the
  last commit (an LF file rewritten as CRLF on Windows, for example) is refused with the file's
  name and how to restore it; add `# eol-ok` to the command when the change is intended.
- **Changelog:** this file, from 1.0.0 on.

### Changed

- **Symptoms lines are searched one by one:** each line of a note's `symptoms:` is now a piece of
  its own besides the joined one, so a question asked with any one line finds the note; the index
  updates the notes that have symptoms by itself on the next build, and recall keeps answering meanwhile.

### Fixed

- **Personality ledger with CRLF:** an editor that saved `.brain/patterns.md` with Windows line
  endings made the nightly run add a second `Last scan` line and read every observation again; the
  ledger now keeps its own line endings and a duplicate line is folded into one.

## [1.2.0] - 2026-10-12

### Added

- **Symptoms lines:** a knowledge note can carry `symptoms: ["...", "..."]` in its frontmatter,
  2-3 lines in your own words for the moment you need it, and recall searches them as one more
  piece of the note.
- **Who writes them:** the house rules and the vault's route table ask for symptoms lines in every
  new knowledge note, and the nightly knowledge audit writes them (at most 3 lines of 120
  characters, in the vault's language). Notes you already have are not changed.
- **Skills:** each skill's `SKILL.md` in `.brain/skills/` is searched, and a prompt that names one
  of your skills adds a `[Skill]` line with that file's path, so the assistant reads the skill
  before describing or changing it. Names that are everyday words (`everyday_skills` in
  `.brain/config.json`) never trigger it.
- **Receipts are committed when a session ends:** in a vault that is a git repository, every Stop
  commits only `receipts/` and `daily/` with your own git identity and pushes in the background
  when there is a remote; anything else you staged or edited stays as it was.
- **When it cannot commit:** a busy repository is skipped and the nightly commit catches it; a
  failed commit or push is logged in `.brain/.state/receipt-commit.log` and shows as a warning at
  the next session start.
- **Parallel sessions ask first:** a new gate denies the first write into a folder where another
  session prompted in the last 45 minutes and names that session's topic; the second try passes.
- **Task files:** `task_update.py` refuses a task file with uncommitted changes (exit 3), since
  another session may own it, until you add `--mine`.
- **You hear about the next release:** when a newer NeoMyelin release is out, the assistant says
  so once in its first reply of a session, with the link to its release notes; the installer
  records the installed version in `.brain/VERSION`. Turn it off with `"update_notice": false` in
  `.brain/config.json` or `NEOMYELIN_UPDATE_NOTICE=off`.
- **Stale proposals close:** a proposal in `Evolution.md` still pending 30 days after its last
  activity is closed as reject, with a note saying why and since when.
- **Empty pattern runs are flagged:** `doctor.py` warns when the last 7 nightly pattern runs
  produced nothing while observations wait, and points at the model's raw reply.
- **Missing symptoms lines are flagged:** `doctor.py` warns about a knowledge note written without
  symptoms lines, naming up to three; notes that were there before the update never count.

### Changed

- **Knowledge notes come first:** the prompt hook shows up to 3 knowledge notes, then up to 2
  receipts, tasks or other notes; in one mixed top 3, receipts had pushed the answering note out.
- **A lower bar after the first prompt (Ollama):** later prompts show hits from 0.52 instead of
  0.58. BM25 mode keeps its own rule.
- **Personality evolution reads a month:** the nightly pattern run reads the new observations plus
  the 30 days before them, and writes a new pattern only when one of its quotes is new, so old
  lines alone never propose the same pattern night after night. The model's raw reply is kept in
  `.brain/.state/patterns-last-reply.md`.

### Fixed

- **No console window:** the background recall index refresh no longer opens a window on Windows.

## [1.1.0] - 2026-10-10

### Added

- **Reaction reminders:** a new nightly step finds praise or objections of yours that no commit,
  receipt or note took in, and the next session start lists them as `[reaction debt]` until the
  lesson is written down and closed with `reactions.py close`.

### Changed

- **Alarms first:** the session start lists the brain's own alarms (health check, failed nightly
  steps, reaction debt, evolution, knowledge debt) before due tasks, and never cuts them to fit
  its five lines.
- **Health line names the checks:** the session start's health line names every failing check
  instead of the first one's details.
- **Handled corrections:** `doctor.py` stops flagging a correction once a receipt carries
  `CORRECTION HANDLED: <event_id> -> <where>`, for a correction a hook, a skill, a task or an
  existing rule took care of instead of a new rule.

### Fixed

- **Missed nights:** a nightly run that came due while no session was open is caught up by the
  next session, a morning one included; before, someone who only worked in the morning never got
  a nightly run.

## [1.0.2] - 2026-10-07

### Added

- **Language check:** when you write in your own language and the reply comes back in English,
  the end of the turn asks the assistant to answer again in your language. It works for any
  language, stays silent when the vault's language is English, and runs in Claude Code and Codex
  (Antigravity's hook events carry no text).
- **Learning reminder:** when a session's receipt has a `**Learning**` item and no knowledge note
  was written in that session, the end of the turn reminds the assistant once to write it.

### Changed

- **Receipt labels stay English:** the house rules tell the assistant to keep the receipt labels
  (`**Done**`, `**Learning**` and the rest) and line prefixes in English whatever language it
  writes in, because the brain's scripts find them by those words.

## [1.0.1] - 2026-10-07

### Added

- **Missing CLIs:** the install offers to install whichever of Claude Code, Codex and Antigravity
  is missing, with its official installer, before hooking into them.
- **Vault backup on GitHub:** the install asks whether to back up the vault to an empty private
  GitHub repository (or to keep git on this computer only), links it and makes the first push, so
  the nightly run pushes from then on.
- **Obsidian:** the install recommends Obsidian for reading and editing the vault and offers to
  install it; nothing depends on it.

### Fixed

- **Nightly push:** `daily_commit.py` sets the branch's upstream on its first push; before, a
  remote added by hand after `git init` made every nightly push fail.

## [1.0.0] - 2026-10-01

### Added

- **Install in one message:** paste one line that points Claude Code, Codex or Antigravity at
  `INSTALL.md`; the agent asks your name, the assistant's name, the language and the status line,
  downloads the release, checks its checksum and installs. Running it again from a newer release
  updates, and every settings file it changes is backed up first.
- **Memory in every session:** each new session starts with who you are, your rules, the
  assistant's personality, the last receipt of the project you are in and what is due, in any
  folder.
- **Receipts, tasks and a daily log:** `brain.py`, a small engine at the vault root, records each
  finished piece of work as a receipt (secrets in it masked), keeps tasks with their history and
  rebuilds a daily log, all in plain Markdown.
- **Recall on every prompt:** the notes that touch your question are attached before the assistant
  answers; pure Python search out of the box, better matches when Ollama with `bge-m3` is running.
- **A personality that is earned:** a line enters `Personality.md` only when the same thing shows
  on three different days, with the quotes as evidence; any line can be vetoed and never comes
  back.
- **Friction becomes proposals:** the nightly run turns friction the assistant hit twice into a
  proposed fix in `Evolution.md`, and lessons a session forgot into knowledge notes. It needs no
  scheduler: it starts at your first session after the time you chose.
- **A structure for your whole life:** folders from `200-Goals` to `900-Archive`, and a map in the
  vault's `AGENTS.md` that tells the assistant where each kind of note goes.
- **House rules in every session:** a short rule set in `.brain/instructions/` (receipts, recall,
  friction, four working principles), imported by your user-level `CLAUDE.md` and copied into
  Codex's `AGENTS.md` and Antigravity's `GEMINI.md`.
- **One home for your skills:** skills live in `.brain/skills/` and are linked into all three; the
  install can move your own skills in, each backed up first.
- **Guard rails:** a receipt reminder at the end of real work, and gates that refuse a git command
  that wipes the working tree and a shell heredoc that mangles backslashes.
- **Health and quota:** `doctor.py` checks the whole layer, `limit` shows Claude, Codex and
  Antigravity quota from any folder, and Claude Code can get a two-line status line.
- **Uninstall:** `uninstall.py` takes back what the install wrote outside the vault and leaves the
  vault as it is.
