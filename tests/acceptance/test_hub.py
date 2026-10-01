"""Acceptance tests for the vault as the hub (Task-17), written by the orchestrator, not the builder.

Install links every skill in `<vault>/.brain/skills/` into each harness's user skill folder, can
adopt the user's own skills (`--adopt-skills`: moved into the vault, replaced by a link, backed up
first) and keeps the rendered instructions in `<vault>/.brain/instructions/<harness>.md`. Moving a
user's skill can lose it, so these tests are kept apart from the code's author (Blueprint ->
Constraints). They use only the command line and never touch the real home directory.

A link is a symlink or (Windows) a directory junction. The tests never delete a link with a
recursive delete: tearDownClass unlinks every link before the temporary folder is removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_install import ENV, ROOT, fake_cli

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import install  # noqa: E402  (only for skill_targets: where each harness reads its skills)

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English",
         "harnesses": ["claude", "codex", "agy"]}
HARNESSES = ("claude", "codex", "agy")
START, END = "<!-- neomyelin-instructions:start -->", "<!-- neomyelin-instructions:end -->"
POMODORO = b"---\nname: pomodoro\ndescription: a user's own timer skill\n---\n\nTwenty-five minutes, kiwi-7741.\n"
CLASH_CLAUDE = b"---\nname: clash\ndescription: the Claude one\n---\n\nClaude text, walrus-1182.\n"
CLASH_CODEX = b"---\nname: clash\ndescription: the Codex one\n---\n\nCodex text, heron-5530.\n"
MY_NOTES = b"---\nname: my-notes\ndescription: a skill added to the vault after install\n---\n\nNotes.\n"
KEEP = b"---\nname: keepme\ndescription: a user's skill installed without adoption\n---\n\nquokka-9021.\n"


def is_link(p: Path | str) -> bool:
    return os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p))


def real(p: Path | str) -> str:
    return os.path.normcase(os.path.realpath(p))


def inside(p: Path | str, folder: Path | str) -> bool:
    path, base = real(p), real(folder)
    return path == base or path.startswith(base.rstrip("\\/") + os.sep)


def tree(*dirs: Path) -> dict[str, tuple]:
    """Every file (hash), folder and link (its resolved target) under `dirs`, links not followed,
    `.state` and `__pycache__` left out."""
    out: dict[str, tuple] = {}

    def walk(folder: Path) -> None:
        for entry in sorted(os.scandir(folder), key=lambda e: e.name):
            p = Path(entry.path)
            if entry.name in (".state", "__pycache__"):
                continue
            if is_link(p):
                out[str(p)] = ("link", real(p))
            elif entry.is_dir():
                out[str(p)] = ("dir",)
                walk(p)
            elif entry.is_file():
                out[str(p)] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())

    for base in dirs:
        if base.is_dir():
            walk(base)
    return out


def unlink_links(folder: Path) -> None:
    """Remove every link under `folder` without touching its target (unlink, never rmtree)."""
    for entry in list(os.scandir(folder)):
        p = Path(entry.path)
        if is_link(p):
            try:
                os.unlink(p)
            except OSError:
                os.rmdir(p)  # a junction, removed as the folder entry it is
        elif entry.is_dir(follow_symlinks=False):
            unlink_links(p)


def block(text: str) -> str:
    """The text between the instruction markers."""
    return text[text.index(START) + len(START):text.index(END)]


def meaningful(text: str) -> list[str]:
    """Lines of `text` without line endings, blank lines, the markers and a one-line generated-by
    comment."""
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped or stripped in (START, END):
            continue
        if re.fullmatch(r"<!--.*-->", stripped) and re.search(r"neomyelin|generated|written by",
                                                               stripped, re.IGNORECASE):
            continue
        lines.append(line.rstrip())
    return lines


class HubAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = base = Path(cls.tmp.name).resolve()

        # Stand-in harness commands: install configures a harness only when its command is on PATH.
        stand_in = base / "stand_in_cli.py"
        stand_in.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        cls.bin = base / "bin"
        for name in HARNESSES:
            fake_cli(cls.bin, name, stand_in)
        cls.env = {**ENV, "PATH": str(cls.bin) + os.pathsep + ENV.get("PATH", "")}

        cls.config = base / "config.json"
        cls.config.write_text(json.dumps(NAMES), encoding="utf-8")

        # Vault A + home A: fresh install, a later skill, idempotence.
        cls.vault = cls.make_vault("vault-a")
        cls.home = base / "home-a"
        cls.home.mkdir()
        # Vault B + home B: adoption, then uninstall.
        cls.vault_b = cls.make_vault("vault-b")
        cls.home_b = base / "home-b"
        # Vault C + home C: a user's skill and no --adopt-skills.
        cls.vault_c = cls.make_vault("vault-c")
        cls.home_c = base / "home-c"

        cls._fresh = None
        cls._adopted = None
        cls._adopt_before = None
        cls._uninstalled_b = None

    @classmethod
    def tearDownClass(cls):
        try:
            unlink_links(cls.base)
        finally:
            cls.tmp.cleanup()

    @classmethod
    def py(cls, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *args], cwd=ROOT, env=cls.env, capture_output=True, text=True,
            encoding="utf-8", timeout=600,
        )

    @classmethod
    def make_vault(cls, name: str) -> Path:
        vault = cls.base / name
        made = cls.py("scripts/make_test_vault.py", str(vault))
        if made.returncode != 0:
            raise AssertionError(f"make_test_vault failed:\n{made.stdout}\n{made.stderr}")
        return vault

    @classmethod
    def install(cls, vault: Path, home: Path, *flags: str) -> subprocess.CompletedProcess:
        return cls.py("install.py", "--vault", str(vault), "--config", str(cls.config),
                      "--home", str(home), *flags)

    @classmethod
    def uninstall(cls, vault: Path, home: Path) -> subprocess.CompletedProcess:
        return cls.py("uninstall.py", "--vault", str(vault), "--home", str(home))

    @staticmethod
    def skill_dirs(home: Path) -> dict[str, Path]:
        """Each harness's user skill folder (the `limit` target is <folder>/limit/SKILL.md)."""
        # Task-17 made skill_targets return each harness's skills folder (it returned
        # <folder>/limit/SKILL.md before); accept both shapes.
        return {h: (path.parent.parent if path.name == "SKILL.md" else path)
                for h, path in install.skill_targets(home).items()}

    @staticmethod
    def output(res: subprocess.CompletedProcess) -> str:
        return f"exit {res.returncode}\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"

    # Each scenario runs once and is shared by the tests that need it, in any order.

    def fresh(self) -> subprocess.CompletedProcess:
        cls = type(self)
        if cls._fresh is None:
            cls._fresh = cls.install(cls.vault, cls.home)
        self.assertEqual(cls._fresh.returncode, 0, self.output(cls._fresh))
        return cls._fresh

    def adopted(self) -> subprocess.CompletedProcess:
        cls = type(self)
        if cls._adopted is None:
            dirs = self.skill_dirs(cls.home_b)
            for folder, data in ((dirs["claude"] / "pomodoro", POMODORO),
                                 (dirs["claude"] / "clash", CLASH_CLAUDE),
                                 (dirs["codex"] / "clash", CLASH_CODEX)):
                folder.mkdir(parents=True)
                (folder / "SKILL.md").write_bytes(data)
            cls._adopt_before = tree(cls.home_b)
            cls._adopted = cls.install(cls.vault_b, cls.home_b, "--adopt-skills")
        self.assertEqual(cls._adopted.returncode, 0, self.output(cls._adopted))
        return cls._adopted

    def assert_link(self, path: Path, target: Path) -> None:
        self.assertTrue(is_link(path), f"{path} is not a link (symlink or junction)")
        self.assertEqual(real(path), real(target), f"{path} does not resolve to {target}")

    def assert_real_folder(self, folder: Path, data: bytes) -> None:
        self.assertTrue(folder.is_dir(), f"{folder} is missing")
        self.assertFalse(is_link(folder), f"{folder} is a link, not the user's real folder")
        self.assertEqual((folder / "SKILL.md").read_bytes(), data, f"{folder}/SKILL.md changed")

    # ------------------------------------------------------------------------------------------

    def test_0_fresh_install_links_limit_and_writes_instructions(self):
        self.fresh()
        brain = self.vault / ".brain"
        limit = brain / "skills" / "limit"
        self.assertTrue((limit / "SKILL.md").is_file(), limit)
        for harness, folder in self.skill_dirs(self.home).items():
            self.assert_link(folder / "limit", limit)
            self.assertTrue((folder / "limit" / "SKILL.md").is_file(), f"{harness}: limit unreadable")

        # Claude: the block holds only an import of the vault's claude.md.
        instructions = brain / "instructions"
        claude_md = self.home / ".claude" / "CLAUDE.md"
        text = claude_md.read_text(encoding="utf-8")
        self.assertEqual(text.count(START), 1, text)
        self.assertEqual(text.count(END), 1, text)
        lines = meaningful(block(text))
        self.assertEqual(len(lines), 1, f"the Claude block is not one import line: {lines}")
        line = lines[0].strip()
        self.assertTrue(line.startswith("@"), line)
        self.assertIn(".brain/instructions/claude.md", line.replace("\\", "/"))
        self.assertEqual(real(line[1:].strip()), real(instructions / "claude.md"), line)
        claude_text = (instructions / "claude.md").read_text(encoding="utf-8")
        self.assertIn(NAMES["assistant_name"], claude_text)

        # Codex: no import, so its block is a copy of the vault's codex.md.
        codex_file = instructions / "codex.md"
        self.assertTrue(codex_file.is_file(), codex_file)
        codex_md = (self.home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(codex_md.count(START), 1, codex_md)
        self.assertEqual("\n".join(meaningful(block(codex_md))).strip(),
                         "\n".join(meaningful(codex_file.read_text(encoding="utf-8"))).strip())

    def test_1_skill_added_later_is_linked_by_next_install(self):
        self.fresh()
        skill = self.vault / ".brain" / "skills" / "my-notes"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_bytes(MY_NOTES)
        res = self.install(self.vault, self.home)
        self.assertEqual(res.returncode, 0, self.output(res))
        for harness, folder in self.skill_dirs(self.home).items():
            self.assert_link(folder / "my-notes", skill)
            self.assertEqual((folder / "my-notes" / "SKILL.md").read_bytes(), MY_NOTES, harness)

    def test_2_adopt_moves_backs_up_records_and_leaves_a_conflict(self):
        res = self.adopted()
        brain = self.vault_b / ".brain"
        dirs = self.skill_dirs(self.home_b)

        # pomodoro: moved into the vault, a link left behind, a backup, its origin recorded.
        hub = brain / "skills" / "pomodoro"
        self.assertEqual((hub / "SKILL.md").read_bytes(), POMODORO)
        self.assertFalse(is_link(hub), f"{hub} is a link, not the vault's own folder")
        self.assert_link(dirs["claude"] / "pomodoro", hub)
        backups = sorted((brain / ".backup").glob("skills-*/claude/pomodoro/SKILL.md"))
        self.assertTrue(backups, f"no backup under {brain / '.backup'}")
        self.assertTrue(any(p.read_bytes() == POMODORO for p in backups), backups)
        self.assertTrue(any(re.fullmatch(r"skills-\d{8}-\d{6}", p.parents[2].name) for p in backups),
                        backups)
        record = json.loads((brain / "skills.json").read_text(encoding="utf-8"))
        self.assertIn("pomodoro", record, record)
        self.assertIn("claude", record["pomodoro"].get("harnesses", []), record)
        origin = record["pomodoro"].get("origins", {}).get("claude")
        self.assertIsNotNone(origin, record)
        self.assertEqual(os.path.normcase(os.path.abspath(origin)),
                         os.path.normcase(os.path.abspath(dirs["claude"] / "pomodoro")), record)

        # clash: two different skills of one name, neither adopted, both as the user left them.
        self.assert_real_folder(dirs["claude"] / "clash", CLASH_CLAUDE)
        self.assert_real_folder(dirs["codex"] / "clash", CLASH_CODEX)
        self.assertFalse((brain / "skills" / "clash").exists(), "the conflicting skill was adopted")
        self.assertNotIn("clash", record, record)
        self.assertIn("clash", res.stdout + res.stderr, "the conflict is not reported")

    def test_3_without_adopt_flag_user_skills_untouched(self):
        dirs = self.skill_dirs(self.home_c)
        mine = {dirs["claude"] / "keepme": KEEP, dirs["codex"] / "keepme": KEEP}
        for folder, data in mine.items():
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_bytes(data)
        before = {str(folder): tree(folder) for folder in mine}
        res = self.install(self.vault_c, self.home_c)
        self.assertEqual(res.returncode, 0, self.output(res))
        for folder, data in mine.items():
            self.assert_real_folder(folder, data)
            self.assertEqual(tree(folder), before[str(folder)], f"{folder} changed")
        self.assertFalse((self.vault_c / ".brain" / "skills" / "keepme").exists(),
                         "a user skill was moved into the vault without --adopt-skills")

    def test_4_uninstall_after_adoption_restores_and_keeps_the_vault(self):
        self.adopted()
        cls = type(self)
        if cls._uninstalled_b is None:
            cls._uninstalled_b = self.uninstall(self.vault_b, self.home_b)
        res = cls._uninstalled_b
        self.assertEqual(res.returncode, 0, self.output(res))
        brain = self.vault_b / ".brain"
        dirs = self.skill_dirs(self.home_b)

        self.assert_real_folder(dirs["claude"] / "pomodoro", POMODORO)
        self.assertEqual((brain / "skills" / "pomodoro" / "SKILL.md").read_bytes(), POMODORO,
                         "the vault's own copy is gone")
        self.assertTrue((brain / "skills" / "limit" / "SKILL.md").is_file(),
                        "removing the links emptied the vault's limit skill")
        for harness, folder in dirs.items():
            if not folder.is_dir():
                continue
            for entry in os.scandir(folder):
                self.assertFalse(is_link(entry.path) and inside(entry.path, self.vault_b),
                                 f"{harness}: {entry.path} still links into the vault")
        self.assert_real_folder(dirs["claude"] / "clash", CLASH_CLAUDE)
        self.assert_real_folder(dirs["codex"] / "clash", CLASH_CODEX)

        claude_md = self.home_b / ".claude" / "CLAUDE.md"
        if claude_md.is_file():
            text = claude_md.read_text(encoding="utf-8").replace("\\", "/")
            self.assertNotIn(".brain/instructions/claude.md", text, "the import line is still there")
            self.assertNotIn(START, text)

    def test_5_second_install_and_second_uninstall_write_nothing(self):
        self.fresh()
        res = self.install(self.vault, self.home)
        self.assertEqual(res.returncode, 0, self.output(res))
        before = tree(self.vault, self.home)
        again = self.install(self.vault, self.home)
        self.assertEqual(again.returncode, 0, self.output(again))
        self.assertEqual(before, tree(self.vault, self.home), "a second install wrote something")

        self.assertTrue((self.vault / ".brain" / "skills" / "limit" / "SKILL.md").is_file(),
                        "install left no limit skill in the vault")
        first = self.uninstall(self.vault, self.home)
        self.assertEqual(first.returncode, 0, self.output(first))
        self.assertTrue((self.vault / ".brain" / "skills" / "limit" / "SKILL.md").is_file(),
                        "uninstall emptied the vault's limit skill")
        before = tree(self.vault, self.home)
        second = self.uninstall(self.vault, self.home)
        self.assertEqual(second.returncode, 0, self.output(second))
        self.assertEqual(before, tree(self.vault, self.home), "a second uninstall wrote something")


if __name__ == "__main__":
    unittest.main()
