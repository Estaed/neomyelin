"""Acceptance tests for uninstall.py, written by an independent test author, not the code's author.

Uninstall edits the user's own harness files (hook settings, instruction files, skill folders), a
path that can lose data, so these tests are kept apart from the code (Blueprint -> Constraints).
They use only the command line `python uninstall.py --vault <V> [--home <H>]` and never touch the
real home directory.

Codex and agy are registered by install only when their command is on PATH, so setUpClass puts
stand-in `claude`, `codex` and `agy` commands (a Python script that exits 0) first on PATH for
install and uninstall alike; no real CLI is reached. On Windows, Codex and agy also need a vault
path without spaces; when the temp folder has one, their parts are skipped with that reason.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_install import ENV, ROOT, fake_cli, hook_commands, snapshot
from test_install_harnesses import agy_commands

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English",
         "harnesses": ["claude", "codex", "agy"]}
START, END = "<!-- neomyelin-instructions:start -->", "<!-- neomyelin-instructions:end -->"
OWN_TEXT = "# My own rules\n\nAlways answer briefly.\n"
USER_SKILL = "---\nname: limit\ndescription: my own limit skill\n---\n\nA skill the user wrote.\n"
FOREIGN = "echo user-own-hook"


def state(*dirs: Path) -> tuple[dict[str, str], list[str]]:
    """File hashes plus every folder, so a created or removed empty folder shows too."""
    folders = sorted(str(p) for base in dirs if base.exists() for p in base.rglob("*") if p.is_dir()
                     and "__pycache__" not in p.parts and ".state" not in p.parts)
    return snapshot(*[d for d in dirs if d.exists()]), folders


def backups_of(path: Path) -> list[Path]:
    """Sibling files next to `path` whose name starts with its name (install's `<name>.neomyelin-*.bak`)."""
    if not path.parent.is_dir():
        return []
    return [p for p in path.parent.iterdir()
            if p.is_file() and p.name != path.name and p.name.startswith(path.name)]


class UninstallAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = base = Path(cls.tmp.name).resolve()
        cls.vault = base / "vault"
        cls.home = base / "home"
        cls.other_vault = base / "other-vault"

        # Stand-in harness commands: install registers Codex and agy only when they are on PATH.
        stand_in = base / "stand_in_cli.py"
        stand_in.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        cls.bin = base / "bin"
        for name in ("claude", "codex", "agy"):
            fake_cli(cls.bin, name, stand_in)
        cls.env = {**ENV, "PATH": str(cls.bin) + os.pathsep + ENV.get("PATH", "")}

        made = cls.py("scripts/make_test_vault.py", str(cls.vault))
        if made.returncode != 0:
            raise AssertionError(f"make_test_vault failed:\n{made.stdout}\n{made.stderr}")
        cls.config = base / "config.json"
        cls.config.write_text(json.dumps(NAMES), encoding="utf-8")

        # Windows: Codex and agy hooks need a vault path without whitespace (render_hooks.Unsupported).
        cls.shimmed_ok = not (os.name == "nt" and re.search(r"\s", str(cls.vault)))

        # Paths of every harness file install writes.
        h = cls.home
        cls.claude_settings = h / ".claude" / "settings.json"
        cls.codex_hooks = h / ".codex" / "hooks.json"
        cls.agy_hooks = h / ".gemini" / "config" / "hooks.json"
        cls.claude_md = h / ".claude" / "CLAUDE.md"
        cls.codex_md = h / ".codex" / "AGENTS.md"
        cls.agy_md = h / ".gemini" / "GEMINI.md"
        cls.claude_skill = h / ".claude" / "skills" / "limit"
        cls.codex_skill = h / ".codex" / "skills" / "limit"
        cls.agy_skill = h / ".gemini" / "antigravity" / "skills" / "limit"

        # The user's own content before install.
        other_scripts = (cls.other_vault / ".brain" / "scripts").as_posix()
        cls.other_cmd = f'"python" "{other_scripts}/memory_context.py" --session-start'
        cls.claude_original = {
            "model": "keep-me",
            "hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": FOREIGN}]},
                {"hooks": [{"type": "command", "command": cls.other_cmd}]},
            ]},
        }
        cls.codex_original = {"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "echo codex-own-hook"}]},
            {"hooks": [{"type": "command", "command": cls.other_cmd}]},
        ]}}
        cls.agy_original = {"other-brain": {"PreInvocation": [
            {"type": "command", "command": "echo agy-own-hook"}]}}
        for path, data in ((cls.claude_settings, cls.claude_original),
                           (cls.codex_hooks, cls.codex_original),
                           (cls.agy_hooks, cls.agy_original)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        cls.claude_md.write_text(OWN_TEXT, encoding="utf-8")
        (cls.codex_skill / "SKILL.md").parent.mkdir(parents=True)
        (cls.codex_skill / "SKILL.md").write_text(USER_SKILL, encoding="utf-8")

        cls.install_res = cls.py("install.py", "--vault", str(cls.vault), "--config", str(cls.config),
                                 "--home", str(cls.home), "--statusline")
        if cls.install_res.returncode != 0:
            raise AssertionError(f"install failed:\n{cls.install_res.stdout}\n{cls.install_res.stderr}")

        # Everything as install left it, for the checks that install wrote our pieces, the backups
        # and the untouched vault.
        cls.harness_files = [cls.claude_settings, cls.codex_hooks, cls.agy_hooks,
                             cls.claude_md, cls.codex_md, cls.agy_md]
        cls.installed = {p: (p.read_bytes() if p.is_file() else None) for p in cls.harness_files}
        cls.vault_installed = state(cls.vault)
        cls.skills_installed = {p: ((p / "SKILL.md").read_bytes() if (p / "SKILL.md").is_file() else None)
                                for p in (cls.claude_skill, cls.codex_skill, cls.agy_skill)}

        cls.uninstall_res = cls.uninstall()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def py(cls, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args], cwd=ROOT, env=cls.env, capture_output=True, text=True,
            encoding="utf-8", timeout=600,
        )

    @classmethod
    def uninstall(cls, vault: Path | None = None, home: Path | None = None) -> subprocess.CompletedProcess:
        return cls.py("uninstall.py", "--vault", str(vault or cls.vault), "--home", str(home or cls.home))

    def ours(self, command: str) -> bool:
        """A command that runs something under THIS vault's .brain/ (a script or a shim)."""
        folder = (self.vault.as_posix().rstrip("/") + "/.brain/").casefold()
        return folder in command.replace("\\", "/").casefold()

    def need_shims(self):
        if not self.shimmed_ok:
            self.skipTest("Windows: Codex and agy need a vault path without spaces, and this one has one")

    def output(self, res: subprocess.CompletedProcess) -> str:
        return f"exit {res.returncode}\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"

    # ------------------------------------------------------------------------------------------

    def test_0_install_wrote_our_pieces(self):
        settings = json.loads(self.installed[self.claude_settings].decode("utf-8"))
        self.assertTrue(any(self.ours(c) for c in hook_commands(settings)), settings)
        self.assertIn("statusline.py", settings.get("statusLine", {}).get("command", ""))
        self.assertIn(START, self.installed[self.claude_md].decode("utf-8"))
        if self.shimmed_ok:
            codex = json.loads(self.installed[self.codex_hooks].decode("utf-8"))
            self.assertTrue(any(self.ours(c) for c in hook_commands(codex)), codex)
            agy = json.loads(self.installed[self.agy_hooks].decode("utf-8"))
            self.assertTrue(any(self.ours(c) for c in agy_commands(agy, "neomyelin")), agy)
        for path in (self.codex_md, self.agy_md):
            self.assertIsNotNone(self.installed[path], f"install did not write {path}")
            self.assertIn(START, self.installed[path].decode("utf-8"))
        # The skills: ours for Claude (Task-18: the vault's rendered SKILL.md, naming its own script);
        # agy reads the hub through skills.json, so nothing goes to its old folder; the user's own
        # Codex skill kept as written.
        vault_skill = (self.vault / ".brain" / "skills" / "limit" / "SKILL.md").read_bytes()
        self.assertEqual(self.skills_installed[self.claude_skill], vault_skill)
        self.assertIsNone(self.skills_installed[self.agy_skill])
        # write_text wrote the user's skill with the platform's line ending.
        self.assertEqual(self.skills_installed[self.codex_skill].decode("utf-8").replace("\r\n", "\n"),
                         USER_SKILL)

    def test_1_uninstall_exits_zero_and_prints_a_report(self):
        self.assertEqual(self.uninstall_res.returncode, 0, self.output(self.uninstall_res))
        self.assertTrue(self.uninstall_res.stdout.strip(), "uninstall printed no report")

    def test_2_claude_settings_back_to_the_users_own(self):
        after = json.loads(self.claude_settings.read_text(encoding="utf-8"))
        cmds = hook_commands(after)
        self.assertFalse([c for c in cmds if self.ours(c)], cmds)
        self.assertIn(FOREIGN, cmds)
        self.assertIn(self.other_cmd, cmds, "another vault's hook was removed")
        self.assertNotIn("statusLine", after, "this vault's status line is still set")
        self.assertIn(after.get("env"), (None, {"PYTHONUTF8": "1"}), after.get("env"))
        # Everything else as the user had it, empty events dropped.
        self.assertEqual({k: v for k, v in after.items() if k != "env"}, self.claude_original)

    def test_3_codex_and_agy_hooks_removed_foreign_kept(self):
        self.need_shims()
        codex = json.loads(self.codex_hooks.read_text(encoding="utf-8"))
        self.assertFalse([c for c in hook_commands(codex) if self.ours(c)], codex)
        self.assertEqual(codex, self.codex_original)
        agy = json.loads(self.agy_hooks.read_text(encoding="utf-8"))
        self.assertEqual(agy_commands(agy, "neomyelin"), [], agy)
        self.assertEqual(agy.get("neomyelin", {}), {}, agy)
        self.assertEqual({k: v for k, v in agy.items() if k != "neomyelin"}, self.agy_original)

    def test_4_instruction_blocks_removed_own_text_kept(self):
        text = self.claude_md.read_text(encoding="utf-8")
        self.assertNotIn(START, text)
        self.assertNotIn(END, text)
        self.assertEqual(text.rstrip(), OWN_TEXT.rstrip())
        # Install created these two with nothing but our block, so uninstall removes them.
        self.assertFalse(self.codex_md.exists(), self.codex_md)
        self.assertFalse(self.agy_md.exists(), self.agy_md)

    def test_5_limit_skill_removed_only_where_neomyelin_wrote_it(self):
        self.assertFalse(self.claude_skill.exists(), self.claude_skill)
        self.assertFalse(self.agy_skill.exists(), self.agy_skill)
        self.assertEqual((self.codex_skill / "SKILL.md").read_text(encoding="utf-8"), USER_SKILL)

    def test_6_every_changed_harness_file_backed_up_first(self):
        files = [self.claude_settings, self.claude_md]
        if self.shimmed_ok:
            files += [self.codex_hooks, self.agy_hooks]
        files += [self.codex_md, self.agy_md]
        for path in files:
            before = self.installed[path]
            self.assertIsNotNone(before, path)
            now = path.read_bytes() if path.is_file() else None
            self.assertNotEqual(before, now, f"{path} was not changed by uninstall")
            found = backups_of(path)
            self.assertTrue(any(p.read_bytes() == before for p in found),
                            f"no backup of {path} as it was before uninstall among {found}")

    def test_7_vault_untouched(self):
        self.assertEqual(self.vault_installed, state(self.vault))
        self.assertTrue((self.vault / "brain.py").is_file())
        self.assertTrue((self.vault / ".brain" / "config.json").is_file())

    def test_8_second_run_writes_nothing(self):
        before = state(self.home, self.vault)
        again = self.uninstall()
        self.assertEqual(again.returncode, 0, self.output(again))
        self.assertEqual(before, state(self.home, self.vault))

    def test_9_vault_without_install_refused_nothing_written(self):
        vault = self.base / "vault-bare"
        made = self.py("scripts/make_test_vault.py", str(vault))
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.assertFalse((vault / ".brain" / "config.json").exists())
        home = self.base / "home-bare"
        settings = home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        scripts = (vault / ".brain" / "scripts").as_posix()
        settings.write_text(json.dumps({"model": "keep-me", "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": FOREIGN}]},
            {"hooks": [{"type": "command",
                        "command": f'"python" "{scripts}/memory_context.py" --session-start'}]},
        ]}}, indent=2), encoding="utf-8")
        (home / ".claude" / "CLAUDE.md").write_text(
            f"{OWN_TEXT}\n{START}\nsome block\n{END}\n", encoding="utf-8")
        before = state(home, vault)
        res = self.uninstall(vault=vault, home=home)
        self.assertNotEqual(res.returncode, 0, self.output(res))
        self.assertEqual(before, state(home, vault))


class UninstallTwoVaults(unittest.TestCase):
    """Two vaults installed on one home: vault A, then vault B. B's install replaces the instruction
    block, which names the vault it was written for, so the block (and each harness's `limit` skill)
    belongs to B. Uninstalling A keeps them and removes only A's hooks; uninstalling B then removes
    B's hooks, the block and the skill."""

    py = UninstallAcceptance.__dict__["py"]
    output = UninstallAcceptance.output
    need_shims = UninstallAcceptance.need_shims

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = base = Path(cls.tmp.name).resolve()
        cls.vault_a = base / "vault-a"
        cls.vault_b = base / "vault-b"
        cls.home = h = base / "home"

        stand_in = base / "stand_in_cli.py"
        stand_in.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        cls.bin = base / "bin"
        for name in ("claude", "codex", "agy"):
            fake_cli(cls.bin, name, stand_in)
        cls.env = {**ENV, "PATH": str(cls.bin) + os.pathsep + ENV.get("PATH", "")}
        cls.shimmed_ok = not (os.name == "nt" and re.search(r"\s", str(base)))

        cls.config = base / "config.json"
        cls.config.write_text(json.dumps(NAMES), encoding="utf-8")

        cls.claude_settings = h / ".claude" / "settings.json"
        cls.codex_hooks = h / ".codex" / "hooks.json"
        cls.agy_hooks = h / ".gemini" / "config" / "hooks.json"
        cls.md_files = [h / ".claude" / "CLAUDE.md", h / ".codex" / "AGENTS.md", h / ".gemini" / "GEMINI.md"]
        # agy has no skill folder of ours since Task-18 (it reads the hub through skills.json).
        cls.skills = [h / ".claude" / "skills" / "limit", h / ".codex" / "skills" / "limit"]

        for vault in (cls.vault_a, cls.vault_b):
            made = cls.py("scripts/make_test_vault.py", str(vault))
            if made.returncode != 0:
                raise AssertionError(f"make_test_vault failed:\n{made.stdout}\n{made.stderr}")
            res = cls.py("install.py", "--vault", str(vault), "--config", str(cls.config),
                         "--home", str(h), "--statusline")
            if res.returncode != 0:
                raise AssertionError(f"install of {vault} failed:\n{res.stdout}\n{res.stderr}")

        cls.after_installs = cls.read_all()
        cls.uninstall_a = cls.py("uninstall.py", "--vault", str(cls.vault_a), "--home", str(h))
        cls.after_a = cls.read_all()
        cls.uninstall_b = cls.py("uninstall.py", "--vault", str(cls.vault_b), "--home", str(h))
        cls.after_b = cls.read_all()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def read_all(cls) -> dict:
        """Hook files parsed, instruction files as text, skill files as bytes (None when absent)."""
        def load(path: Path):
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return {
            "claude": load(cls.claude_settings),
            "codex": load(cls.codex_hooks),
            "agy": load(cls.agy_hooks),
            "md": {p: (p.read_text(encoding="utf-8") if p.is_file() else None) for p in cls.md_files},
            "skills": {p: ((p / "SKILL.md").read_bytes() if (p / "SKILL.md").is_file() else None)
                       for p in cls.skills},
        }

    @staticmethod
    def of(vault: Path, commands: list[str]) -> list[str]:
        """The commands that run something under `vault`'s .brain/ (a script or a shim)."""
        folder = (vault.as_posix().rstrip("/") + "/.brain/").casefold()
        return [c for c in commands if folder in c.replace("\\", "/").casefold()]

    def hooks(self, snap: dict, vault: Path, harness: str) -> list[str]:
        cmds = (agy_commands(snap["agy"], "neomyelin") if harness == "agy"
                else hook_commands(snap[harness]))
        return self.of(vault, cmds)

    def block_names(self, text: str, vault: Path) -> bool:
        block = text[text.index(START):text.index(END)]
        return (vault.as_posix() + "/").casefold() in block.replace("\\", "/").casefold()

    # ------------------------------------------------------------------------------------------

    def test_0_both_installs_left_their_hooks_and_the_block_names_b(self):
        snap = self.after_installs
        harnesses = ["claude"] + (["codex"] if self.shimmed_ok else [])
        for harness in harnesses:
            self.assertTrue(self.hooks(snap, self.vault_a, harness), f"{harness}: no hook of vault A")
            self.assertTrue(self.hooks(snap, self.vault_b, harness), f"{harness}: no hook of vault B")
        if self.shimmed_ok:
            self.assertTrue(self.hooks(snap, self.vault_b, "agy"), snap["agy"])
        for path, text in snap["md"].items():
            self.assertIsNotNone(text, f"install did not write {path}")
            self.assertEqual(text.count(START), 1, f"{path} holds more than one block")
            self.assertTrue(self.block_names(text, self.vault_b), f"the block in {path} does not name vault B")
            self.assertFalse(self.block_names(text, self.vault_a), f"the block in {path} still names vault A")
        for skill, data in snap["skills"].items():
            self.assertIsNotNone(data, f"install did not copy {skill}")

    def test_1_uninstall_a_keeps_the_block_and_skills_of_b(self):
        self.assertEqual(self.uninstall_a.returncode, 0, self.output(self.uninstall_a))
        for path in self.md_files:
            self.assertEqual(self.after_a["md"][path], self.after_installs["md"][path],
                             f"uninstall of A changed {path}, whose block belongs to B")
        for skill in self.skills:
            self.assertEqual(self.after_a["skills"][skill], self.after_installs["skills"][skill],
                             f"uninstall of A changed or removed {skill}, which B's install uses")

    def test_2_uninstall_a_reports_the_block_kept_for_another_vault(self):
        self.assertIn("another vault", self.uninstall_a.stdout.casefold(), self.output(self.uninstall_a))

    def test_3_uninstall_a_removes_only_a_hooks(self):
        harnesses = ["claude"] + (["codex", "agy"] if self.shimmed_ok else [])
        for harness in harnesses:
            self.assertEqual(self.hooks(self.after_a, self.vault_a, harness), [],
                             f"{harness}: vault A's hooks are still there")
            self.assertEqual(self.hooks(self.after_a, self.vault_b, harness),
                             self.hooks(self.after_installs, self.vault_b, harness),
                             f"{harness}: vault B's hooks changed")

    def test_4_uninstall_b_removes_hooks_block_and_skills(self):
        self.assertEqual(self.uninstall_b.returncode, 0, self.output(self.uninstall_b))
        for harness in ["claude"] + (["codex", "agy"] if self.shimmed_ok else []):
            self.assertEqual(self.hooks(self.after_b, self.vault_b, harness), [],
                             f"{harness}: vault B's hooks are still there")
        self.assertNotIn("statusLine", self.after_b["claude"], self.after_b["claude"])
        for path, text in self.after_b["md"].items():
            # Install created each file with nothing but the block, so it goes with the block.
            self.assertIsNone(text, f"{path} is still there")
        for skill, data in self.after_b["skills"].items():
            self.assertIsNone(data, f"{skill} is still there")
            self.assertFalse(skill.exists(), skill)


class UninstallVaultGone(unittest.TestCase):
    """The vault folder is deleted (or moved) without uninstalling first. Its hooks then point at
    missing scripts; Python exits 2 on a missing file, which Claude Code reads as "block", so every
    prompt is refused. `uninstall.py --vault <old path>` must still take back everything install
    wrote that points at that path, say the folder is gone and exit 0; a second run writes nothing.

    The harness skill links point INTO the vault, so the vault is deleted with a recursive delete
    (which never follows a link outside it); a link itself is only ever unlinked."""

    py = UninstallAcceptance.__dict__["py"]
    output = UninstallAcceptance.output
    need_shims = UninstallAcceptance.need_shims

    @classmethod
    def setUpClass(cls):
        from test_hub import tree
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.base = base = Path(cls.tmp.name).resolve()
        cls.vault = base / "vault"
        cls.home = h = base / "home"

        stand_in = base / "stand_in_cli.py"
        stand_in.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        cls.bin = base / "bin"
        for name in ("claude", "codex", "agy"):
            fake_cli(cls.bin, name, stand_in)
        cls.env = {**ENV, "PATH": str(cls.bin) + os.pathsep + ENV.get("PATH", "")}
        cls.shimmed_ok = not (os.name == "nt" and re.search(r"\s", str(cls.vault)))

        made = cls.py("scripts/make_test_vault.py", str(cls.vault))
        if made.returncode != 0:
            raise AssertionError(f"make_test_vault failed:\n{made.stdout}\n{made.stderr}")
        cls.config = base / "config.json"
        cls.config.write_text(json.dumps(NAMES), encoding="utf-8")

        cls.claude_settings = h / ".claude" / "settings.json"
        cls.codex_hooks = h / ".codex" / "hooks.json"
        cls.agy_hooks = h / ".gemini" / "config" / "hooks.json"
        cls.agy_skills_json = h / ".gemini" / "config" / "skills.json"
        cls.claude_md = h / ".claude" / "CLAUDE.md"
        cls.md_files = [cls.claude_md, h / ".codex" / "AGENTS.md", h / ".gemini" / "GEMINI.md"]
        cls.skill_folders = [h / ".claude" / "skills", h / ".codex" / "skills",
                             h / ".gemini" / "antigravity" / "skills"]

        # The user's own content before install: a foreign Claude hook, own CLAUDE.md text and an
        # own entry in agy's skills.json.
        cls.claude_settings.parent.mkdir(parents=True)
        cls.claude_settings.write_text(json.dumps({"model": "keep-me", "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": FOREIGN}]}]}}, indent=2), encoding="utf-8")
        cls.claude_md.write_text(OWN_TEXT, encoding="utf-8")
        cls.own_entry = {"path": "~/my-own-skills"}
        cls.agy_skills_json.parent.mkdir(parents=True)
        cls.agy_skills_json.write_text(json.dumps({"entries": [cls.own_entry]}, indent=2), encoding="utf-8")

        cls.install_res = cls.py("install.py", "--vault", str(cls.vault), "--config", str(cls.config),
                                 "--home", str(h), "--statusline")
        if cls.install_res.returncode != 0:
            raise AssertionError(f"install failed:\n{cls.install_res.stdout}\n{cls.install_res.stderr}")
        cls.installed = cls.read_all()
        cls.links_installed = cls.links_into_vault()

        # The vault goes without an uninstall. No link lives inside it; the links into it from the
        # home are left dangling, never followed by the recursive delete of the vault folder.
        shutil.rmtree(cls.vault)
        if os.path.lexists(cls.vault):
            raise AssertionError(f"could not delete {cls.vault}")

        cls.uninstall_res = cls.py("uninstall.py", "--vault", str(cls.vault), "--home", str(h))
        cls.after = cls.read_all()
        cls.home_after = tree(h)
        cls.again_res = cls.py("uninstall.py", "--vault", str(cls.vault), "--home", str(h))
        cls.home_again = tree(h)

    @classmethod
    def tearDownClass(cls):
        from test_hub import unlink_links
        if cls.home.is_dir():
            unlink_links(cls.home)  # a link left behind by a failure is unlinked, never recursed into
        cls.tmp.cleanup()

    @classmethod
    def read_all(cls) -> dict:
        def load(path: Path):
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return {
            "claude": load(cls.claude_settings),
            "codex": load(cls.codex_hooks),
            "agy": load(cls.agy_hooks),
            "skills.json": load(cls.agy_skills_json),
            "md": {p: (p.read_text(encoding="utf-8") if p.is_file() else None) for p in cls.md_files},
        }

    @classmethod
    def names_vault(cls, text: str) -> bool:
        """`text` names the old vault folder (backslashes, and the escaped spaces of Claude's import
        line, read as plain path characters)."""
        folder = cls.vault.as_posix().rstrip("/") + "/"
        return folder.casefold() in text.replace("\\ ", " ").replace("\\", "/").casefold()

    @classmethod
    def link_target(cls, path: Path) -> str | None:
        """Where a symlink or junction points, read without resolving it (its target may be gone)."""
        try:
            raw = os.readlink(path)
        except (OSError, ValueError):
            return None
        for prefix in ("\\\\?\\UNC\\", "\\\\?\\"):
            if raw.startswith(prefix):
                raw = ("\\\\" if "UNC" in prefix else "") + raw[len(prefix):]
                break
        return raw if os.path.isabs(raw) else str(path.parent / raw)

    @classmethod
    def links_into_vault(cls) -> list[str]:
        """Every entry in the harness skill folders that is a link whose target is inside the vault."""
        folder = os.path.normcase(os.path.normpath(str(cls.vault)))
        found = []
        for root in cls.skill_folders:
            if not root.is_dir():
                continue
            for entry in os.scandir(root):
                target = cls.link_target(Path(entry.path))
                if target is None:
                    continue
                target = os.path.normcase(os.path.normpath(target))
                if target == folder or target.startswith(folder.rstrip("\\/") + os.sep):
                    found.append(entry.path)
        return found

    def hooks_naming_vault(self, snap: dict) -> dict[str, list[str]]:
        found = {
            "claude": [c for c in hook_commands(snap["claude"]) if self.names_vault(c)],
            "codex": [c for c in hook_commands(snap["codex"]) if self.names_vault(c)],
            "agy": [c for c in agy_commands(snap["agy"], "neomyelin") if self.names_vault(c)],
        }
        status = snap["claude"].get("statusLine", {}).get("command", "")
        found["claude statusLine"] = [status] if self.names_vault(status) else []
        return found

    def hub_entries(self, snap: dict) -> list:
        hub = self.vault.as_posix().rstrip("/") + "/.brain/skills"
        return [e for e in snap["skills.json"].get("entries", [])
                if isinstance(e, dict) and isinstance(e.get("path"), str)
                and e["path"].replace("\\", "/").rstrip("/").casefold() == hub.casefold()]

    # ------------------------------------------------------------------------------------------

    def test_0_install_pointed_the_home_at_the_vault(self):
        found = self.hooks_naming_vault(self.installed)
        self.assertTrue(found["claude"], self.installed["claude"])
        self.assertTrue(found["claude statusLine"], self.installed["claude"].get("statusLine"))
        if self.shimmed_ok:
            self.assertTrue(found["codex"], self.installed["codex"])
            self.assertTrue(found["agy"], self.installed["agy"])
        for path, text in self.installed["md"].items():
            self.assertIsNotNone(text, f"install did not write {path}")
            self.assertTrue(self.names_vault(text), f"the block in {path} does not name the vault")
        self.assertTrue(self.links_installed, "install linked no skill into the vault")
        self.assertEqual(len(self.hub_entries(self.installed)), 1, self.installed["skills.json"])

    def test_1_uninstall_exits_zero_and_says_the_folder_is_gone(self):
        self.assertEqual(self.uninstall_res.returncode, 0, self.output(self.uninstall_res))
        self.assertIn("gone", self.uninstall_res.stdout.casefold(), self.output(self.uninstall_res))
        self.assertFalse(os.path.lexists(self.vault), "uninstall created the vault folder again")

    def test_2_no_hook_or_status_line_names_the_old_vault(self):
        for where, cmds in self.hooks_naming_vault(self.after).items():
            self.assertEqual(cmds, [], f"{where} still runs something in the deleted vault")

    def test_3_no_instruction_block_names_the_old_vault(self):
        for path, text in self.after["md"].items():
            if text is None:
                continue
            self.assertFalse(self.names_vault(text), f"{path} still names the deleted vault:\n{text}")
            self.assertNotIn(START, text, path)
        self.assertEqual(self.after["md"][self.claude_md].rstrip(), OWN_TEXT.rstrip())

    def test_4_no_skill_link_into_the_old_vault(self):
        self.assertEqual(self.links_into_vault(), [])

    def test_5_agy_skills_json_drops_the_vault_keeps_the_users_entry(self):
        self.assertEqual(self.hub_entries(self.after), [], self.after["skills.json"])
        self.assertIn(self.own_entry, self.after["skills.json"].get("entries", []), self.after["skills.json"])

    def test_6_foreign_hook_and_other_keys_kept(self):
        self.assertIn(FOREIGN, hook_commands(self.after["claude"]))
        self.assertEqual(self.after["claude"].get("model"), "keep-me")

    def test_7_second_run_writes_nothing(self):
        self.assertEqual(self.again_res.returncode, 0, self.output(self.again_res))
        self.assertEqual(self.home_after, self.home_again)


if __name__ == "__main__":
    unittest.main()
