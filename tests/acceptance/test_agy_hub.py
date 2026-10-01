"""Acceptance test for agy's view of the skills hub (Task-18), written by the orchestrator.

agy reads skills from the folders declared in `~/.gemini/config/skills.json` (measured with agy
1.2.14; it does not read `~/.gemini/antigravity/skills/`). Install adds one entry for the vault's
`.brain/skills`; the user's own entries and keys stay; uninstall gives the user's file back as it
was. A stand-in `agy` on PATH makes install treat agy as installed; the real home is never used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_install import ENV, ROOT, fake_cli

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English",
         "harnesses": ["claude", "codex", "agy"]}
USER_SKILLS_JSON = {"inherits": [{"path": "/somewhere/team/skills.json"}],
                    "entries": [{"path": "~/my-own-skills", "exclude": ["draft-.*"]}],
                    "note": "the user's own key"}


def same_path(a: str, b: Path) -> bool:
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.expanduser(str(p)).replace("/", os.sep)))
    return norm(a) == norm(b)


class AgyHubAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(cls.tmp.name).resolve()
        cls.vault, cls.home = base / "vault", base / "home"
        stand_in = base / "stand_in_cli.py"
        stand_in.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        for name in ("claude", "codex", "agy"):
            fake_cli(base / "bin", name, stand_in)
        cls.env = {**ENV, "PATH": str(base / "bin") + os.pathsep + ENV.get("PATH", "")}
        made = cls.py("scripts/make_test_vault.py", str(cls.vault))
        assert made.returncode == 0, made.stdout + made.stderr
        cls.config = base / "config.json"
        cls.config.write_text(json.dumps(NAMES), encoding="utf-8")
        cls.skills_json = cls.home / ".gemini" / "config" / "skills.json"
        cls.skills_json.parent.mkdir(parents=True)
        cls.original = json.dumps(USER_SKILLS_JSON, indent=2).encode("utf-8")
        cls.skills_json.write_bytes(cls.original)
        cls.installed = cls.py("install.py", "--vault", str(cls.vault), "--config", str(cls.config),
                               "--home", str(cls.home))
        cls.after_install = cls.skills_json.read_bytes() if cls.skills_json.exists() else b""
        cls.reinstalled = cls.py("install.py", "--vault", str(cls.vault), "--home", str(cls.home))
        cls.after_reinstall = cls.skills_json.read_bytes() if cls.skills_json.exists() else b""
        cls.uninstalled = cls.py("uninstall.py", "--vault", str(cls.vault), "--home", str(cls.home))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def py(cls, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *args], cwd=ROOT, env=cls.env, capture_output=True,
                              text=True, encoding="utf-8", timeout=600)

    def test_0_install_declares_the_hub_once_and_keeps_the_users_entries(self):
        self.assertEqual(self.installed.returncode, 0, self.installed.stdout + self.installed.stderr)
        data = json.loads(self.after_install)
        hub = self.vault / ".brain" / "skills"
        ours = [e for e in data.get("entries", []) if same_path(e.get("path", ""), hub)]
        self.assertEqual(len(ours), 1, data)
        self.assertIn({"path": "~/my-own-skills", "exclude": ["draft-.*"]}, data["entries"])
        self.assertEqual(data.get("inherits"), USER_SKILLS_JSON["inherits"])
        self.assertEqual(data.get("note"), "the user's own key")
        self.assertTrue((hub / "limit" / "SKILL.md").is_file())
        old = self.home / ".gemini" / "antigravity" / "skills" / "limit"
        self.assertFalse(os.path.lexists(old), "agy does not read antigravity/skills; nothing goes there")

    def test_1_second_install_writes_nothing(self):
        self.assertEqual(self.reinstalled.returncode, 0, self.reinstalled.stdout + self.reinstalled.stderr)
        self.assertEqual(self.after_install, self.after_reinstall)

    def test_2_uninstall_gives_the_users_file_back(self):
        self.assertEqual(self.uninstalled.returncode, 0, self.uninstalled.stdout + self.uninstalled.stderr)
        # Same content; the byte layout may differ after a JSON round trip.
        self.assertEqual(json.loads(self.skills_json.read_bytes()), USER_SKILLS_JSON)
        self.assertTrue((self.vault / ".brain" / "skills" / "limit" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
