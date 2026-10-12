"""session_gate.py: one stop before touching a folder another live session is working in.

The markers are written by prompt_recall.py's own code (repeat_gate), so a change to their format
shows here.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helpers import ROOT, make_vault, write, write_config

import prompt_recall
import session_gate

SCRIPT = ROOT / 'brain' / 'scripts' / 'session_gate.py'
ENV = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'}


class SessionGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.vault, cfg = make_vault(self.base)
        self.home = self.base / 'home'
        self.fish = self.base / 'fish'          # the other live session's project
        self.mine = self.base / 'garden'        # this session's project
        for folder in (self.home, self.fish / 'docs', self.mine):
            folder.mkdir(parents=True, exist_ok=True)
        self.state = self.base / 'recall-state'
        patches = [
            mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(self.base, cfg)),
                                         'HOME': str(self.home), 'USERPROFILE': str(self.home)}),
            mock.patch.object(prompt_recall, 'STATE_DIR', self.state),
            mock.patch.object(session_gate, 'ACK_DIR', self.base / 'gate-state'),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def marker(self, session: str, cwd: Path, age_min: float = 1, topic: str = 'Reading frame 0089') -> Path:
        prompt_recall.repeat_gate(session, topic, prompt_recall.normal_cwd(str(cwd)))
        path = prompt_recall._marker(session)
        data = json.loads(path.read_text(encoding='utf-8'))
        data['updated_at'] -= age_min * 60
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def hook(self, tool: str, tool_input: dict, session: str = 'me', cwd: Path | None = None) -> str | None:
        """The deny reason, or None when the call passes."""
        answer = session_gate.decide({'tool_name': tool, 'tool_input': tool_input, 'session_id': session,
                                      'cwd': str(self.mine if cwd is None else cwd)})
        if answer is None:
            return None
        output = answer['hookSpecificOutput']
        self.assertEqual((output['hookEventName'], output['permissionDecision']), ('PreToolUse', 'deny'))
        return output['permissionDecisionReason']

    # The original's cases.

    def test_edit_in_live_peer_folder_is_denied_once(self):
        self.marker('me', self.mine)
        self.marker('fish', self.fish)
        target = {'file_path': str(self.fish / 'docs' / 'blueprint.md')}
        reason = self.hook('Edit', target)
        self.assertIsNotNone(reason)
        self.assertIn("topic: 'Reading frame 0089'", reason)
        self.assertIn('Ask that session first', reason)
        self.assertIn('ListAgents', reason)
        self.assertIsNone(self.hook('Edit', target))

    def test_stale_peer_passes(self):
        self.marker('fish', self.fish, age_min=46)
        self.assertIsNone(self.hook('Write', {'file_path': str(self.fish / 'x.md')}))

    def test_own_workspace_passes(self):
        self.marker('fish', self.fish)
        self.assertIsNone(self.hook('Edit', {'file_path': str(self.fish / 'x.md')}, cwd=self.fish))
        self.assertIsNone(self.hook('Edit', {'file_path': str(self.fish / 'docs' / 'x.md')}, cwd=self.fish / 'docs'))

    def test_vault_is_shared(self):
        self.marker('vault', self.vault)
        self.assertIsNone(self.hook('Edit', {'file_path': str(self.vault / 'knowledge' / 'a.md')}, cwd=self.fish))

    def test_writing_bash_into_peer_folder_is_denied(self):
        self.marker('fish', self.fish)
        self.assertIsNotNone(self.hook('Bash', {'command': f'cd {self.fish} && git commit -m x'}))

    def test_git_dash_c_write_is_denied(self):
        self.marker('fish', self.fish)
        self.assertIsNotNone(self.hook('Bash', {'command': f'git -C {self.fish} add --dry-run x'}))

    def test_reading_bash_passes(self):
        self.marker('fish', self.fish)
        self.assertIsNone(self.hook('Bash', {'command': f'cd {self.fish} && git log -3 --oneline'}))

    def test_reading_with_swallowed_errors_passes(self):
        self.marker('fish', self.fish)
        command = f'grep -rn x {self.fish}/docs 2>/dev/null | head; git -C {self.fish} log -1 2>&1'
        self.assertIsNone(self.hook('Bash', {'command': command}))

    def test_redirect_into_peer_file_is_denied(self):
        self.marker('fish', self.fish)
        self.assertIsNotNone(self.hook('Bash', {'command': f'echo x > {self.fish}/notes.md'}))

    def test_broken_payload_never_blocks(self):
        for raw in (b'not json', b'', b'[1]', b'{"tool_name": "Edit", "tool_input": "x", "session_id": 3}'):
            with self.subTest(raw=raw):
                result = subprocess.run([sys.executable, str(SCRIPT), '--pre-tool-use'], input=raw,
                                        capture_output=True, env=ENV, timeout=60)
                self.assertEqual((result.returncode, result.stdout), (0, b''), result.stderr)

    # What the port adds.

    def test_home_and_filesystem_roots_are_shared_ground(self):
        self.marker('home', self.home)
        self.marker('root', Path(self.base.anchor))
        self.assertIsNone(self.hook('Write', {'file_path': str(self.home / 'notes.txt')}, cwd=self.fish))
        self.assertIsNone(self.hook('Bash', {'command': f'echo x > {self.home / "notes.txt"}'}, cwd=self.fish))

    def test_a_session_opened_in_shared_ground_does_not_own_the_folders_below(self):
        project = self.home / 'proj'
        project.mkdir()
        self.marker('proj', project)
        self.assertIsNotNone(self.hook('Edit', {'file_path': str(project / 'a.py')}, cwd=self.home))
        self.marker('fish-in-vault', self.vault / 'fish')
        self.assertIsNotNone(self.hook('Edit', {'file_path': str(self.vault / 'fish' / 'a.md')}, cwd=self.vault))

    def test_apply_patch_targets_resolve_against_cwd(self):
        self.marker('fish', self.fish)
        patch = '*** Begin Patch\n*** Update File: ../fish/notes.md\n@@\n-old\n+new\n*** End Patch\n'
        self.assertIsNotNone(self.hook('apply_patch', {'input': patch}, cwd=self.mine))
        self.assertIsNone(self.hook('apply_patch', {'input': patch}, cwd=self.mine))  # the retry
        own = '*** Begin Patch\r\n*** Add File: notes.md\r\n+hi\r\n*** End Patch\r\n'
        self.assertIsNone(self.hook('apply_patch', {'input': own}, session='other', cwd=self.mine))

    def test_every_patch_line_counts(self):
        self.marker('fish', self.fish)
        for line in (f'*** Add File: {self.fish / "new.md"}', f'*** Delete File: {self.fish / "old.md"}',
                     f'*** Update File: notes.md\n*** Move to: {self.fish / "moved.md"}'):
            with self.subTest(line=line):
                patch = f'*** Begin Patch\n{line}\n+x\n*** End Patch'
                self.assertIsNotNone(self.hook('apply_patch', {'command': patch}, session=line, cwd=self.mine))

    def test_a_patch_inside_a_shell_command_counts(self):
        self.marker('fish', self.fish)
        argv = ['apply_patch', '*** Begin Patch\n*** Update File: ../fish/a.py\n@@\n-a\n+b\n*** End Patch']
        self.assertIsNotNone(self.hook('shell', {'command': argv}, cwd=self.mine))

    def test_a_text_that_only_mentions_patch_lines_is_not_a_patch(self):
        self.marker('fish', self.fish)
        self.assertIsNone(self.hook('Agent', {'prompt': f'*** Update File: {self.fish / "a.py"}'}))
        self.assertIsNone(self.hook('Write', {'file_path': str(self.mine / 'doc.md'),
                                              'content': f'*** Begin Patch\n*** Add File: {self.fish / "a"}\n'}))

    def test_any_tool_carrying_a_command_counts(self):
        self.marker('fish', self.fish)
        calls = [('shell', {'command': ['bash', '-lc', f'echo x > {self.fish}/a.txt']}),
                 ('exec_command', {'command': f'rm {self.fish}/b.txt'}),
                 ('PowerShell', {'command': f'Set-Content -Path "{self.fish}\\c.txt" -Value x'})]
        for number, (tool, tool_input) in enumerate(calls):
            with self.subTest(tool=tool):
                self.assertIsNotNone(self.hook(tool, tool_input, session=f's{number}'))

    def test_a_shell_call_running_in_a_peer_folder_counts_by_its_workdir(self):
        self.marker('fish', self.fish)
        for number, workdir in enumerate((str(self.fish), '../fish', str(self.fish / 'docs'))):
            with self.subTest(workdir=workdir):
                self.assertIsNotNone(self.hook('shell', {'command': ['bash', '-lc', 'rm notes.md'],
                                                         'workdir': workdir}, session=f'w{number}'))
        patch = ['apply_patch', '*** Begin Patch\n*** Update File: notes.md\n@@\n-a\n+b\n*** End Patch']
        self.assertIsNotNone(self.hook('shell', {'command': patch, 'workdir': str(self.fish)}, session='p'))
        self.assertIsNone(self.hook('shell', {'command': 'git status', 'workdir': str(self.fish)}, session='r'))
        self.assertIsNone(self.hook('shell', {'command': 'rm notes.md', 'workdir': str(self.mine)}, session='o'))

    def test_reading_commands_pass(self):
        self.marker('fish', self.fish)
        for command in (f'cat {self.fish}/notes.md', f'ls {self.fish}', f'git -C {self.fish} status',
                        f'Get-Content "{self.fish}\\notes.md"', ['git', '-C', str(self.fish), 'diff']):
            with self.subTest(command=command):
                self.assertIsNone(self.hook('Bash', {'command': command}))

    def test_the_original_write_forms_all_count(self):
        self.marker('fish', self.fish)
        forms = ('sed -i s/a/b/ {f}/a', 'cat x >> {f}/a', 'mv a {f}/a', 'cp a {f}/a', 'echo a | tee {f}/a',
                 'git -C {f} reset HEAD~1', 'git -C "{f}" stash', 'git -C {f} push', 'git -C {f} clean -n',
                 'git -C {f} restore a', 'git -C {f} checkout a', 'git -C {f} rm a', 'git -C {f} mv a b',
                 'Add-Content {f}/a x', 'Out-File {f}/a', 'Remove-Item {f}/a', 'Move-Item a {f}/a',
                 'Copy-Item a {f}/a', 'New-Item {f}/a')
        for number, form in enumerate(forms):
            with self.subTest(form=form):
                self.assertIsNotNone(self.hook('Bash', {'command': form.format(f=self.fish)}, session=f's{number}'))

    def test_a_drive_path_matches_its_git_bash_form_and_back(self):
        home = self.home.as_posix()
        self.assertTrue(session_gate._named_in('echo x > /x/work/fish/a.md', 'x:\\work\\fish', home))
        self.assertTrue(session_gate._named_in('echo x > Q:\\Work\\Fish\\a.md', 'q:\\work\\fish', home))
        self.assertTrue(session_gate._named_in('echo x > x:/work/fish/a.md', '/x/work/fish', home))
        self.assertFalse(session_gate._named_in('echo x > /y/work/fish/a.md', 'x:\\work\\fish', home))
        if os.name == 'nt':  # a real marker folder, named in the Git Bash form
            self.marker('fish', self.fish)
            drive, rest = self.fish.as_posix().split(':', 1)
            self.assertIsNotNone(self.hook('Bash', {'command': f'echo x > /{drive.lower()}{rest}/a.md'}))

    def test_a_folder_under_home_matches_its_tilde_form(self):
        project = self.home / 'proj'
        project.mkdir()
        self.marker('proj', project)
        self.assertIsNotNone(self.hook('Bash', {'command': 'echo x > ~/proj/a.md'}, session='a'))
        self.assertIsNotNone(self.hook('PowerShell', {'command': 'Set-Content $HOME\\proj\\a.md x'}, session='b'))
        self.assertIsNone(self.hook('Bash', {'command': 'echo x > ~/project-two/a.md'}, session='c'))

    def test_a_sibling_folder_sharing_a_prefix_is_not_named(self):
        api = self.base / 'fish-api'
        api.mkdir()
        self.marker('fish', self.fish)
        self.assertIsNone(self.hook('Bash', {'command': f'cd {api} && git commit -m x'}, cwd=api))
        self.assertIsNone(self.hook('Edit', {'file_path': str(api / 'a.py')}, cwd=self.mine))

    def test_each_folder_stops_once(self):
        other = self.base / 'birds'
        other.mkdir()
        self.marker('fish', self.fish, topic='Fish lengths')
        self.marker('birds', other, topic='Bird counts')
        self.assertIn('Fish lengths', self.hook('Write', {'file_path': str(self.fish / 'a')}))
        self.assertIn('Bird counts', self.hook('Write', {'file_path': str(other / 'a')}))
        self.assertIsNone(self.hook('Write', {'file_path': str(other / 'b')}))
        self.assertIsNone(self.hook('Bash', {'command': f'rm {self.fish}/a {other}/a'}))

    def test_one_call_into_two_live_folders_names_both_and_stops_once(self):
        other = self.base / 'birds'
        other.mkdir()
        self.marker('fish', self.fish, topic='Fish lengths')
        self.marker('birds', other, topic='Bird counts')
        reason = self.hook('Bash', {'command': f'rm {self.fish}/a {other}/a'})
        self.assertIn('other live sessions are working in', reason)
        self.assertIn('Ask those sessions first', reason)
        self.assertIn('Fish lengths', reason)
        self.assertIn('Bird counts', reason)
        self.assertIsNone(self.hook('Bash', {'command': f'rm {self.fish}/a {other}/a'}))

    def test_the_window_is_prompt_recalls(self):
        self.assertEqual(prompt_recall.PARALLEL_STALE_SECONDS, 45 * 60)
        self.marker('fish', self.fish, age_min=44)
        self.assertIsNotNone(self.hook('Write', {'file_path': str(self.fish / 'a')}))

    def test_without_a_session_or_a_target_nothing_is_decided(self):
        self.marker('fish', self.fish)
        target = {'file_path': str(self.fish / 'a')}
        self.assertIsNone(session_gate.decide({'tool_name': 'Edit', 'tool_input': target, 'cwd': str(self.mine)}))
        self.assertIsNone(self.hook('Read', target))
        self.assertIsNone(self.hook('Edit', {}))

    def test_broken_markers_are_skipped(self):
        self.state.mkdir(parents=True, exist_ok=True)
        for number, text in enumerate(('not json', '[1, 2]', '{"cwd": "x"}', '{"updated_at": "soon"}')):
            write(self.state / (f'{number}' * 64 + '.json'), text)
        self.assertIsNone(self.hook('Write', {'file_path': str(self.fish / 'a')}))
        self.marker('fish', self.fish)
        self.assertIsNotNone(self.hook('Write', {'file_path': str(self.fish / 'a')}))

    def test_a_missing_state_folder_decides_nothing(self):
        self.assertFalse(self.state.exists())
        self.assertIsNone(self.hook('Write', {'file_path': str(self.fish / 'a')}))

    def run_main(self, payload: object) -> tuple[int, str]:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
        stdin = io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8')
        with mock.patch.object(sys, 'stdin', stdin), mock.patch.object(sys, 'stdout', new_callable=io.StringIO) as out:
            code = session_gate.main(['--pre-tool-use'])
        return code, out.getvalue()

    def test_the_hook_prints_the_deny_answer(self):
        self.marker('fish', self.fish)
        code, out = self.run_main({'tool_name': 'Write', 'tool_input': {'file_path': str(self.fish / 'a')},
                                   'session_id': 'me', 'cwd': str(self.mine)})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_its_own_errors_never_block(self):
        self.marker('fish', self.fish)
        payload = {'tool_name': 'Write', 'tool_input': {'file_path': str(self.fish / 'a')},
                   'session_id': 'me', 'cwd': str(self.mine)}
        write(self.base / 'gate-state', 'a file where the ack folder should be')
        self.assertEqual(self.run_main(payload), (0, ''))  # the stop could not be recorded
        with mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(self.base / 'missing.json')}):
            self.assertEqual(self.run_main(payload), (0, ''))
        self.assertEqual(self.run_main(b'{broken'), (0, ''))

    def test_old_stops_are_forgotten(self):
        self.marker('fish', self.fish)
        old = write(self.base / 'gate-state' / ('0' * 64 + '.json'), '[]')
        os.utime(old, (0, 0))
        self.assertIsNotNone(self.hook('Write', {'file_path': str(self.fish / 'a')}))
        self.assertFalse(old.exists())

    def test_a_bad_call_exits_1_never_2(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], input=b'{}', capture_output=True, env=ENV, timeout=60)
        self.assertEqual((result.returncode, result.stdout), (1, b''))


if __name__ == '__main__':
    unittest.main()
