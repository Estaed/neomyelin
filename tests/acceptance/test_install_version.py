"""Acceptance: install records the release version the update notice compares (Task-25).

Written by the orchestrator, not the builder (Blueprint -> Constraints). A release copy is the
repo's product folders plus a VERSION file, as release.py builds the ZIP; a dev checkout has none.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from test_install import ROOT, run

NAMES = {"user_name": "Sam", "assistant_name": "Echo", "language": "English", "harnesses": ["claude"]}


class InstallVersion(unittest.TestCase):
    def test_release_copy_records_its_version_and_a_dev_copy_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            release = base / "neomyelin-v9.8.7"
            release.mkdir()
            for folder in ("brain", "engine", "templates", "skills"):
                shutil.copytree(ROOT / folder, release / folder,
                                ignore=shutil.ignore_patterns("__pycache__", ".state"))
            for name in ("install.py", "uninstall.py"):
                shutil.copy2(ROOT / name, release / name)
            (release / "VERSION").write_text("9.8.7\n", encoding="utf-8")

            vault, home, cfg = base / "vault", base / "home", base / "config.json"
            self.assertEqual(run("scripts/make_test_vault.py", str(vault)).returncode, 0)
            home.mkdir()
            cfg.write_text(json.dumps(NAMES), encoding="utf-8")

            res = run(str(release / "install.py"), "--vault", str(vault), "--config", str(cfg),
                      "--home", str(home))
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            record = vault / ".brain" / "VERSION"
            self.assertEqual(record.read_text(encoding="utf-8").strip(), "9.8.7")

            # Re-running from this tree: a dev checkout (no VERSION) must not leave a stale version
            # behind; inside a release ZIP (VERSION present) the record becomes that version.
            res = run("install.py", "--vault", str(vault), "--home", str(home))
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            own = ROOT / "VERSION"
            if own.is_file():
                self.assertEqual(record.read_text(encoding="utf-8").strip(),
                                 own.read_text(encoding="utf-8").strip())
            else:
                self.assertFalse(record.exists(), res.stdout)


if __name__ == "__main__":
    unittest.main()
