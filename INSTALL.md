# Install NeoMyelin

You are an AI coding agent (Claude Code, Codex or Antigravity) running in the folder the user chose
for their vault: usually a new, empty folder, or a notes folder they already keep. Follow these
steps in order. Ask the user only where a step says so; do everything else yourself and say in one
line what you are doing. Stop and report if a check fails; never guess past it.

## 1. Check what is there

1. Say in one line whether the current folder is empty. If it holds files, nothing in it is
   overwritten, moved or deleted: the installer adds its own folders and files next to them and
   names every file it kept.
2. Python 3.11 or newer answers: `py -3 --version` on Windows, `python3 --version` on macOS and
   Linux. Every Python command below names both.
3. Note which of `claude`, `codex`, `agy` are installed (`--version` on each). Those are the
   harnesses NeoMyelin will hook into.
4. Windows only: if the vault path holds a space (for example `C:\Users\Sam\My Notes`) and Codex
   or agy is installed, their hooks cannot run from it (Codex hands the hook command to cmd.exe
   unquoted, so the path breaks at the space), and the installer skips them. Before installing,
   offer a path without spaces (for example `C:\Users\Sam\Notes`): if the user agrees, they make
   that folder (and move their notes there), and you install into it. Claude Code works from
   either path.
5. If the folder is a git repository, make a checkpoint commit of its current state so every
   change below can be undone: `git add -A && git commit -m "Before NeoMyelin"`, and note whether
   it already has a remote (`git remote -v`). Git itself is a question in step 2.
6. Obsidian is where the user reads and edits the vault (no plugin is needed; the brain itself
   works on the plain Markdown files). A `.obsidian` folder in the vault means it is already an
   Obsidian vault. Otherwise check whether Obsidian is installed: `winget list --id
   Obsidian.Obsidian` or `%LOCALAPPDATA%\Programs\Obsidian` on Windows, `/Applications/Obsidian.app`
   on macOS, `flatpak list` or `snap list obsidian` on Linux. If it is missing, recommend it and
   offer to install it: `winget install --id Obsidian.Obsidian -e` (Windows), `brew install --cask
   obsidian` (macOS), `flatpak install flathub md.obsidian.Obsidian` (Linux), or the official page
   `https://obsidian.md/download` when that package manager is missing. Install only after the
   user says yes; if they decline, carry on, because every step below works without it.

## 2. Ask the user

Ask all of these in one message and wait for the answers. Never skip the last two: they are the
ones a user cannot set up later without knowing they exist.

- Their name (how the assistant should call them).
- The assistant's name (NeoMyelin has no built-in name; "Atlas", "Mira", anything).
- The language the assistant should speak and write the vault in.
- Claude Code users only: turn on the two-line status line (model, quota, context)? Recommended,
  because the `limit` quota view falls back on its reading.
- Which of the harness CLIs missing in step 1.3 (Claude Code, Codex, Antigravity) to install now.
  NeoMyelin hooks only into the ones installed before step 5; each needs its own account (Claude
  Code a Claude Pro, Max, Team or Enterprise plan or a Console account; Codex a ChatGPT account;
  Antigravity a Google account). Skip this question if all three are installed.
- Back up the vault to a private GitHub repository? Recommended: the nightly run then commits and
  pushes the vault once a day, so the notes survive a lost computer and every change can be
  undone. If yes, ask for the address of an **empty private** repository (made at
  `https://github.com/new` with no README, `.gitignore` or licence, so the first push is not
  refused). If they would rather not, offer git without a remote (`git init`): daily commits on
  this computer only. If the vault already has a remote (step 1.5), say so and skip the question.

Then act on the answers before going on:

1. Install each CLI the user chose, with its official installer (Windows PowerShell first, then
   macOS and Linux):

   - Claude Code: `irm https://claude.ai/install.ps1 | iex`, or
     `curl -fsSL https://claude.ai/install.sh | bash`
   - Codex: `irm https://chatgpt.com/codex/install.ps1 | iex`, or
     `curl -fsSL https://chatgpt.com/codex/install.sh | sh`
   - Antigravity: `irm https://antigravity.google/cli/install.ps1 | iex`, or
     `curl -fsSL https://antigravity.google/cli/install.sh | bash`

   (From a shell other than PowerShell on Windows, wrap the command:
   `powershell -ExecutionPolicy ByPass -c "<command>"`.) Then run `<cli> --version`. If this shell
   does not find it, its folder is not on this shell's PATH yet: add the folder the installer
   printed to PATH for this shell, because `install.py` skips the hooks of a CLI it cannot find.
   Tell the user that each one signs in through the browser the first time it is opened.
2. Git: if the user chose a backup, run `git init` (when the folder is not a repository yet) and
   `git remote add origin <address>`; if they chose git without a remote, run `git init` only. The
   first push happens in step 8, once the vault holds the install.

## 3. Download the release

1. Open `https://github.com/Estaed/neomyelin/releases/latest`, download
   `neomyelin-vX.Y.Z.zip` and `neomyelin-vX.Y.Z.zip.sha256`.
2. Check the SHA-256 of the ZIP against the `.sha256` file. If they differ, stop.
3. Unpack it into a temporary folder outside the vault.

## 4. Existing notes (only if the folder already holds notes)

NeoMyelin brings a folder structure: `200-Goals`, `300-Education`, `400-Work`, `500-Projects`,
`600-Life`, `700-Private`, `800-Arsenal`, `900-Archive`. The installer only adds these; the user's
own folders stay where they are.

If notes sit in other folders, list them grouped by the NeoMyelin folder each would fit, show the
list to the user and move only what they approve. Never move `receipts/`, `daily/`, `knowledge/`,
`tasks/` or the companion folder (`850-Companion 🔮`, or the one the vault already has: a folder
whose name ends in "Companion", or that holds `Core.md`). Receipts and tasks already in the
vault's format keep working as they are.

## 5. Install

1. Write `neomyelin-config.json` in the temporary folder:

   ```json
   {
     "user_name": "<their name>",
     "assistant_name": "<assistant name>",
     "language": "<language>",
     "harnesses": ["claude", "codex", "agy"],
     "engine": "auto",
     "nightly_at": "21:00"
   }
   ```

   List only the harnesses found in step 1.3 or installed in step 2. `nightly_at` is when the daily evolution run becomes
   due; it runs in the background at the first session after that time.
2. From the unpacked release folder run, on Windows:

   ```
   py -3 install.py --vault "<vault path>" --config "<temp>/neomyelin-config.json" [--statusline]
   ```

   and on macOS or Linux the same with `python3` in place of `py -3`.

   Show the user its output. It is safe to run again: unchanged files are not rewritten and every
   settings file it changes is backed up next to itself first. It puts the engine, `brain.py`
   (receipts, tasks, the daily log), at the vault root and everything else in `.brain/`, the
   vault's hub:
   - the house rules (receipts, recall, friction) in `.brain/instructions/`, reached from a block
     between NeoMyelin markers in the user-level `CLAUDE.md` (an import line), Codex `AGENTS.md`
     and agy's `GEMINI.md` (a copy); `--no-instructions` skips the blocks;
   - the skills in `.brain/skills/` (NeoMyelin ships `limit`), linked into the skill folders of
     Claude Code and Codex; agy reads `.brain/skills/` itself through one entry the installer
     adds to agy's `~/.gemini/config/skills.json`, next to the user's own. `--no-skills` skips
     both. A skill folder of the user's own, or a link to somewhere else, under the same name is
     never replaced.
3. If the output has a line `your own skills, not in the hub: ...`, show the user that list and
   ask whether to move those skills into the vault's hub. Recommend yes: one source for every
   harness, so a skill edited once is the same in Claude Code, Codex and agy (agy's own skills
   in `~/.gemini/config/skills/` stay where they are; agy reads them and the hub). Say that each
   is backed up first into `.brain/.backup/` and that uninstalling puts them back where they were.
   If they agree, run the same command again with `--adopt-skills` and show its output. A line
   `skill conflict, not adopted` names skills that differ between harnesses (or from the hub's
   own); they stay where they are, and the user can merge them by hand and run it again.

## 6. One-time trust steps

Tell the user exactly these, for the harnesses they use:

- **Claude Code:** nothing.
- **Codex:** open Codex once, run `/hooks`, and approve the NeoMyelin hooks. Codex skips an
  unapproved hook silently, so this step is required, and again after a NeoMyelin update.
- **Antigravity (agy):** nothing for this install (its hooks are user-level). If agy asks to
  trust the vault folder, accept: hooks in a folder's own `.agents/hooks.json` load only after
  that trust. On Linux, an agy installed as a snap cannot run NeoMyelin's hooks (the snap cannot
  see the system Python); the installer says so and still installs the house rules for it. Tell
  the user that installing agy outside snap gives agy the session memory too.

## 7. Check it works

1. Run `py -3 .brain/scripts/doctor.py` (Windows) or `python3 .brain/scripts/doctor.py` (macOS,
   Linux) in the vault. Every row must be OK or an explained warning (no recall index before the
   first session, or a folder that is not a git repository, is normal).
2. Ask the user to open a new session in each harness they use and ask: "Who are you? Quote your
   `[Memory: Session]` line." The answer must use the assistant name and language from step 2
   and quote that line with its 24-character value. Only the session hook prints it (the
   instruction block alone can still give the name), so no such line means the hooks did not
   run: run step 6 again and re-check.
3. Windows and Codex users: recommend CodexBar (a tray app) so `limit` sees fresh Codex quota numbers.

## 8. Tell the user what they got

In their language, in a few lines: session memory in every folder, recall of their notes on each
prompt, a receipt at the end of meaningful work with a daily log built from the receipts
(`daily/`), a personality that changes only when their own behaviour shows the same thing on three
different days (`850-Companion 🔮/Personality.md`, any line can be vetoed), friction turned into
proposals (`Evolution.md`), a nightly run after `nightly_at`, one home for their skills and house
rules (`.brain/`, which every harness reads), `limit` for their Claude, Codex and agy quota, and
`doctor.py` for health. Then delete the temporary folder (the release and the config file in it)
and record this install as the vault's first receipt: write
`{"event_id": "<YYYY-MM-DD>-neomyelin-install", "summary": "[NeoMyelin] Installed (Model: <your model>)", "refs": ["AGENTS.md"]}`
to a file and run `py -3 brain.py receipt --file <that file> --harness <claude, codex or agy>`
(Windows; `python3` on macOS and Linux) in the vault; `daily/<today>.md` then shows it.

If the vault is a git repository, commit this first state with the nightly run's own commit step:
`py -3 .brain/scripts/daily_commit.py` (Windows; `python3` on macOS and Linux) in the vault. It
leaves out runtime state and backups and, when there is a remote, pushes and links the branch to
it, so the nightly push works from then on. Its last line must be `daily push complete` (or
`daily push skipped: no remote` for git without a remote). If the push fails because git is not
signed in to GitHub, help the user sign in (on Windows, Git Credential Manager opens a browser
during the push; elsewhere `gh auth login` from GitHub CLI) and run it again.

Last, if Obsidian is installed, tell them to open the vault in it: Open folder as vault, then pick
this exact folder (not a folder above it, or Obsidian sees the vault as one of its subfolders).

## Updating

Run step 3 with the new release, then step 5.2 without `--config`: the installer reuses the
vault's own `.brain/config.json` (the config file from the first install was deleted with the
temporary folder):

```
py -3 install.py --vault "<vault path>" [--statusline]
```

(`python3` in place of `py -3` on macOS and Linux). Then step 5.3 if the output names skills of
the user's own, and step 6. Notes, receipts, the personality and every user line are kept; only
NeoMyelin's own files and entries change.

## Uninstalling

If the user asks to remove NeoMyelin: the release folder from the install was deleted, so
download and unpack the release again (step 3), then from that folder run
`py -3 uninstall.py --vault "<their vault>"` (Windows) or
`python3 uninstall.py --vault "<their vault>"` (macOS, Linux) and show its report. It removes only
what install wrote outside the vault (hook entries, status line, instruction blocks, the skill
links, the entry in agy's `skills.json`), puts every skill it adopted back where it came from as a
real folder, backs up every file it changes and leaves the vault untouched; say that the vault
folder, `.brain/skills/` included, is theirs to keep or delete.

If the user moved or deleted the vault folder without uninstalling (Claude Code then refuses
every prompt with a hook error naming a missing `.brain/scripts/...` file), run the same command
with the old path: it works without the folder and removes every hook, block and link that points
at it. To move a vault: uninstall, move the folder, install again from the new place.
