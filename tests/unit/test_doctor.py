from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("neomyelin_doctor", ROOT / "brain/scripts/doctor.py")
doctor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = doctor
SPEC.loader.exec_module(doctor)
import render_hooks  # noqa: E402  (doctor.py put brain/scripts on sys.path)

REAL_WHICH = shutil.which
NAMES = {"user_name": "Alex", "assistant_name": "Nova", "language": "English"}


class HookRegistrationTests(unittest.TestCase):
    """The Hooks row checks each configured harness's user-level hook file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        if os.name == "nt" and re.search(r"\s", str(base)):
            self.skipTest("Windows: Codex hooks need a vault path without spaces")
        self.root, self.home = base / "vault", base / "home"
        targets = {hook.target for hooks in render_hooks.HOOKS.values() for hook in hooks}
        for target in targets | {"scripts/render_hooks.py"}:
            path = self.root / ".brain" / target
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        (self.root / ".brain" / "config.json").write_text(json.dumps(
            {**NAMES, "vault": str(self.root), "harnesses": ["claude", "codex"]}), encoding="utf-8")
        which = patch.object(doctor.shutil, "which", lambda name: f"/bin/{name}")
        which.start()
        self.addCleanup(which.stop)

    def register(self, harness: str, scope: str = "user") -> None:
        path = render_hooks.settings_path(harness, scope, self.home, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = render_hooks.merge(harness, {}, self.root, sys.executable)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_every_configured_harness_registered_at_user_level_is_ok(self):
        self.register("claude")
        self.register("codex")
        status, detail = doctor.check_hooks(self.root, self.home)
        self.assertEqual(status, doctor.OK, detail)
        self.assertIn("claude (user), codex (user)", detail)

    def test_a_harness_without_our_hooks_is_a_warning_naming_what_is_missing(self):
        self.register("claude")
        status, detail = doctor.check_hooks(self.root, self.home)
        self.assertEqual(status, doctor.WARNING)
        self.assertIn("codex:", detail)
        self.assertIn("SessionStart memory_context.py", detail)
        # One hook removed by hand is named too.
        path = render_hooks.settings_path("claude", "user", self.home, self.root)
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["hooks"]["Stop"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.register("codex")
        status, detail = doctor.check_hooks(self.root, self.home)
        self.assertEqual(status, doctor.WARNING)
        self.assertIn("Stop receipt_gate.py", detail)

    def test_project_scope_registration_counts(self):
        self.register("claude", "project")
        self.register("codex")
        status, detail = doctor.check_hooks(self.root, self.home)
        self.assertEqual(status, doctor.OK, detail)
        self.assertIn("claude (project)", detail)

    def test_a_harness_whose_command_is_missing_is_skipped(self):
        self.register("claude")
        with patch.object(doctor.shutil, "which", lambda name: None if name == "codex" else f"/bin/{name}"):
            status, detail = doctor.check_hooks(self.root, self.home)
        self.assertEqual(status, doctor.OK, detail)
        self.assertIn("codex skipped (codex not installed)", detail)


class DoctorTests(unittest.TestCase):
    def test_missing_recall_index_is_a_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            status, detail = doctor.check_recall_index(Path(directory))
        self.assertEqual(status, doctor.WARNING)
        self.assertIn("missing", detail)

    def test_broken_hook_entry_is_a_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / ".brain/scripts"
            scripts.mkdir(parents=True)
            for name in ("render_hooks.py", "memory_context.py", "prompt_recall.py", "receipt_gate.py"):
                (scripts / name).write_text("", encoding="utf-8")
            settings = root / ".claude/settings.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({"hooks": {"SessionStart": "broken"}}), encoding="utf-8")
            status, detail = doctor.check_hooks(root)
        self.assertEqual(status, doctor.WARNING)
        self.assertIn("not a list", detail)

    def test_git_not_installed_is_a_warning(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(doctor.shutil, "which", lambda name: None if name == "git" else REAL_WHICH(name)):
            root = Path(directory)
            status, detail = doctor.check_git(root)
            self.assertEqual(status, doctor.WARNING)
            self.assertIn("git is not installed", detail)
            self.assertEqual(doctor._run(["neomyelin-no-such-command"], root).returncode, 127)
            check = doctor._safe("Git", lambda: doctor.check_git(root))
        self.assertEqual(check.status, doctor.WARNING)

    def test_save_writes_current_health_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checks = [doctor.Check("Daily", doctor.WARNING, "empty")]
            doctor.write_health(root, checks)
            state = json.loads((root / ".brain/.state/doctor.json").read_text(encoding="utf-8"))
        self.assertEqual(state["component"], "doctor")
        self.assertEqual(state["warnings"], ["Daily: empty"])


if __name__ == "__main__":
    unittest.main()
