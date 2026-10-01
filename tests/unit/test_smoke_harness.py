"""smoke_harness.py is checked for argument handling only; the orchestrator runs it for real."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import write

import smoke_harness


class SmokeHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        write(self.vault / '.brain' / 'scripts' / 'memory_context.py', '# stub\n')

    def tearDown(self):
        self.tmp.cleanup()

    def rejects(self, argv: list[str]) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            smoke_harness.parse_args(argv)
        self.assertEqual(raised.exception.code, 2)
        return err.getvalue()

    def test_valid_call(self):
        args = smoke_harness.parse_args(['claude', str(self.vault), '--model', 'sonnet'])
        self.assertEqual((args.harness, args.vault, args.model, args.timeout),
                         ('claude', self.vault.resolve(), 'sonnet', 300))

    def test_bad_calls_exit_2(self):
        self.assertIn('invalid choice', self.rejects(['vim', str(self.vault)]))
        self.assertIn('is not a folder', self.rejects(['claude', str(self.vault / 'missing')]))
        bare = Path(self.tmp.name) / 'bare'
        bare.mkdir()
        self.assertIn('no NeoMyelin install', self.rejects(['claude', str(bare)]))
        self.assertIn('positive', self.rejects(['claude', str(self.vault), '--timeout', '0']))
        self.rejects(['claude'])

    def test_command_disables_every_tool(self):
        command = smoke_harness.build_command('claude', 'opus')
        self.assertEqual(command[:3], ['claude', '-p', smoke_harness.PROMPT])
        index = command.index('--tools')
        self.assertEqual(command[index + 1], '')
        self.assertIn('--output-format', command)
        self.assertEqual(command[-2:], ['--model', 'opus'])
        self.assertNotIn('--model', smoke_harness.build_command('claude'))

    def test_claude_reply_is_the_json_result(self):
        self.assertEqual(smoke_harness.reply_for('claude', json.dumps({'result': '0123abcd'})), '0123abcd')
        self.assertEqual(smoke_harness.reply_for('claude', json.dumps({'result': 'NONE'})), 'NONE')
        self.assertEqual(smoke_harness.reply_for('claude', 'plain 0123abcd\n'), 'plain 0123abcd')

    def test_every_harness_is_accepted(self):
        for harness in ('claude', 'codex', 'agy'):
            self.assertEqual(smoke_harness.parse_args([harness, str(self.vault)]).harness, harness)

    def test_codex_command_keeps_the_token_from_the_shell_and_writes_the_reply_to_a_file(self):
        reply = Path(self.tmp.name) / 'reply.txt'
        command = smoke_harness.build_command('codex', 'gpt-x', harness='codex', reply_file=reply)
        self.assertEqual(command[:2], ['codex', 'exec'])
        self.assertEqual(command[-1], smoke_harness.PROMPT)
        self.assertEqual(command[command.index('--output-last-message') + 1], str(reply))
        self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
        self.assertIn(smoke_harness.NONCE_ENV, command[command.index('-c') + 1])
        self.assertIn('--dangerously-bypass-hook-trust', command)
        self.assertEqual(command[command.index('-m') + 1], 'gpt-x')
        with self.assertRaises(ValueError):
            smoke_harness.build_command('codex', harness='codex')

    def test_codex_reply_comes_from_the_file_never_from_the_event_stream(self):
        stream = json.dumps({'type': 'hook', 'output': '[Memory: Check] 0123abcd'})
        self.assertEqual(smoke_harness.reply_for('codex', stream, Path(self.tmp.name) / 'missing.txt'), '')
        reply = write(Path(self.tmp.name) / 'reply.txt', '0123abcd\n')
        self.assertEqual(smoke_harness.reply_for('codex', stream, reply), '0123abcd')

    def test_a_codex_turn_that_ran_a_tool_does_not_count(self):
        stream = json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution'}})
        self.assertTrue(smoke_harness.used_tool('codex', stream))
        self.assertFalse(smoke_harness.used_tool('codex', json.dumps({'item': {'type': 'agent_message'}})))
        self.assertFalse(smoke_harness.used_tool('claude', stream))

    def test_agy_command_and_reply(self):
        self.assertEqual(smoke_harness.build_command('agy', 'm', harness='agy'),
                         ['agy', '-p', smoke_harness.PROMPT, '--model', 'm'])
        self.assertEqual(smoke_harness.reply_for('agy', ' 0123abcd \n'), '0123abcd')

    def test_turn_env_carries_the_nonce_and_never_the_silencer(self):
        with mock.patch.dict('os.environ', {'NEOMYELIN_INVOKED_BY': 'x'}):
            env = smoke_harness.turn_env('0123abcd')
        self.assertEqual(env[smoke_harness.NONCE_ENV], '0123abcd')
        self.assertNotIn('NEOMYELIN_INVOKED_BY', env)

    def test_missing_cli_is_a_failure_not_a_pass(self):
        err = io.StringIO()
        with mock.patch.object(smoke_harness.shutil, 'which', return_value=None), contextlib.redirect_stderr(err):
            self.assertEqual(smoke_harness.main(['claude', str(self.vault)]), 1)
        self.assertIn('not found', err.getvalue())


if __name__ == '__main__':
    unittest.main()
