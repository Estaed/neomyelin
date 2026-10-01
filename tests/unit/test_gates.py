"""git_gate.py and heredoc_gate.py, run as the harness runs them: hook JSON on stdin."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT

SCRIPTS = ROOT / 'brain' / 'scripts'
ENV = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}


class GateTestCase(unittest.TestCase):
    script = ''

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # not a git repository: no own-worktree exemption
        self.addCleanup(self.tmp.cleanup)

    def run_hook(self, payload: object, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPTS / self.script), *args],
                              input=json.dumps(payload).encode('utf-8'), cwd=self.tmp.name, env=ENV,
                              capture_output=True, timeout=60)

    def decision(self, command: object, tool: str = 'Bash') -> str | None:
        result = self.run_hook({'tool_name': tool, 'tool_input': {'command': command}, 'cwd': self.tmp.name},
                               '--pre-tool-use')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.decode('utf-8').strip()
        if not out:
            return None
        answer = json.loads(out)['hookSpecificOutput']
        self.assertEqual((answer['hookEventName'], answer['permissionDecision']), ('PreToolUse', 'deny'))
        return answer['permissionDecisionReason']

    def test_a_bad_call_exits_1_never_2(self):
        # Exit code 2 from a PreToolUse hook would block every tool call.
        result = self.run_hook({'tool_name': 'Bash', 'tool_input': {'command': 'ls'}})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, b'')

    def test_broken_input_lets_the_call_through(self):
        for raw in (b'', b'not json', b'[1]'):
            with self.subTest(raw=raw):
                result = subprocess.run([sys.executable, str(SCRIPTS / self.script), '--pre-tool-use'], input=raw,
                                        cwd=self.tmp.name, env=ENV, capture_output=True, timeout=60)
                self.assertEqual((result.returncode, result.stdout), (0, b''))


class GitGateTests(GateTestCase):
    script = 'git_gate.py'

    def test_whole_tree_forms_are_refused(self):
        refused = {
            'git checkout -- .': 'git checkout -- .',
            'git reset --hard': 'git reset --hard',
            'cd repo && git reset --hard HEAD~1': 'git reset --hard',
            'git -C repo clean -fd': 'git clean -f',
            'git restore .': 'git restore .',
            'git stash': 'git stash (no paths)',
            'git switch --discard-changes main': 'git switch --discard-changes',
        }
        for command, form in refused.items():
            with self.subTest(command=command):
                reason = self.decision(command)
                self.assertIsNotNone(reason)
                self.assertTrue(reason.startswith(f'git_gate: `{form}`'), reason)

    def test_every_shell_tool_is_checked(self):
        self.assertIsNotNone(self.decision('git checkout .', tool='PowerShell'))
        self.assertIsNotNone(self.decision(['bash', '-lc', 'git reset --hard'], tool='shell'))  # an argv list

    def test_path_limited_and_harmless_forms_pass(self):
        for command in ('git checkout -- src/app.py', 'git status', 'git reset --soft HEAD~1',
                        'git stash push -- notes.md', 'git clean -n', 'git restore --staged .',
                        'echo "git reset --hard" is refused'):
            with self.subTest(command=command):
                self.assertIsNone(self.decision(command))


class HeredocGateTests(GateTestCase):
    script = 'heredoc_gate.py'

    def test_a_backslash_in_a_heredoc_body_is_refused(self):
        for command in ("cat <<'EOF' > fix.py\nprint('a\\nb')\nEOF",
                        'python - <<PY\nimport re; re.compile(r"\\d+")\nPY',
                        'cat <<-EOF > x.json\n\t{"path": "C:\\\\Users"}\n\tEOF',
                        ['bash', '-lc', "cat <<'EOF' > fix.py\nprint('a\\nb')\nEOF"]):
            with self.subTest(command=command):
                reason = self.decision(command)
                self.assertIsNotNone(reason)
                self.assertTrue(reason.startswith('heredoc_gate: the `'), reason)

    def test_clean_heredocs_here_strings_and_powershell_pass(self):
        for command, tool in (("cat <<'EOF' > notes.md\nplain text\nEOF", 'Bash'),
                              ("grep -c x <<< 'a\\b'", 'Bash'),
                              ('echo "a\\b"', 'Bash'),
                              ("cat <<EOF\nC:\\Users\nEOF", 'PowerShell')):
            with self.subTest(command=command):
                self.assertIsNone(self.decision(command, tool=tool))


del GateTestCase  # the shared base has no script of its own

if __name__ == '__main__':
    unittest.main()
