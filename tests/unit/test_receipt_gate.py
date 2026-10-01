"""receipt_gate.py: the one-time receipt reminder (Claude, Codex) and agy's queued reminder."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, receipt, write_config

import agy_hook
import receipt_gate


def session_value(session_id: str) -> str:
    return hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]


class ReceiptReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.vault, cfg = make_vault(base)
        self.state = base / 'receipt-state'
        patches = [
            mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(base, cfg))}),
            mock.patch.object(receipt_gate, 'STATE_DIR', self.state),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop(receipt_gate.INVOKED_ENV, None)

    def event(self, event: str, harness: str = 'claude', **payload):
        return receipt_gate.respond(harness, event, json.dumps(payload))

    def prompt(self, text: str, session: str = 's1', harness: str = 'claude'):
        return self.event('UserPromptSubmit', harness, session_id=session, prompt=text)

    def edit(self, session: str = 's1', times: int = 1, harness: str = 'claude'):
        for _ in range(times):
            self.event('PostToolUse', harness, session_id=session, tool_name='Edit')

    def stop(self, session: str = 's1', harness: str = 'claude', **extra):
        return self.event('Stop', harness, session_id=session, **extra)

    def test_an_edited_receiptless_session_is_blocked_once_then_let_through(self):
        self.prompt('Rename the config loader and update its callers.')
        self.edit(times=3)
        answer = self.stop()
        self.assertEqual(answer['decision'], 'block')
        reason = answer['reason']
        self.assertTrue(reason.startswith('[Memory: Receipt] Files changed in this session'), reason)
        self.assertIn(f'"{self.vault.as_posix()}/brain.py" receipt --file <receipt.json> --harness claude', reason)
        self.assertIn(f'"session": "{session_value("s1")}"', reason)
        self.assertIsNone(self.stop(stop_hook_active=True))
        self.assertIsNone(self.stop())  # the second Stop of the session passes
        self.edit(times=3)
        self.assertIsNone(self.stop())  # once per session, not once per batch of edits

    def test_a_session_with_several_prompts_is_reminded_too(self):
        self.prompt('What was the plan for the garden?')
        self.assertIsNone(self.stop())
        self.prompt('And when do we water the tomatoes?')
        answer = self.stop()
        self.assertTrue(answer['reason'].startswith('[Memory: Receipt] This session ran several turns without '
                                                    'changing files'), answer)

    def test_short_sessions_pass(self):
        self.prompt('Fix the typo in the README.')
        self.edit(times=2)
        self.assertIsNone(self.stop())

    def test_a_receipt_with_this_session_lets_the_stop_through(self):
        self.edit(times=3)
        receipt(self.vault, 'a' * 64, '[Garden] other session', kind='receipt', event_id='other',
                harness='claude', refs=['AGENTS.md'], created_at='2026-10-01T10:00:00+00:00',
                session=session_value('another session'))
        self.assertEqual(self.stop(session='s1', stop_hook_active=False)['decision'], 'block')
        self.edit(session='s2', times=3)
        receipt(self.vault, 'b' * 64, '[Garden] watering plan', kind='receipt', event_id='mine',
                harness='claude', refs=['AGENTS.md'], created_at='2026-10-01T10:00:00+00:00',
                session=session_value('s2'))
        self.assertIsNone(self.stop(session='s2'))

    def test_opt_out_and_turns_no_human_typed(self):
        self.prompt('[no-record] just testing something', session='out')
        self.edit(session='out', times=5)
        self.assertIsNone(self.stop(session='out'))
        for text in ('<task-notification> agent finished', '<local-command-stdout>ok'):
            self.prompt(text, session='bots')
        self.assertIsNone(self.stop(session='bots'))

    def test_codex_names_its_own_harness(self):
        self.edit(session='c1', times=3, harness='codex')
        answer = self.stop(session='c1', harness='codex')
        self.assertIn('--harness codex', answer['reason'])

    def test_codex_reminds_inside_and_outside_the_vault(self):
        self.edit(session='inside', times=3, harness='codex')
        self.assertEqual(self.stop(session='inside', harness='codex', cwd=str(self.vault))['decision'], 'block')
        self.edit(session='outside', times=3, harness='codex')
        outside = self.vault.parent / 'project'
        outside.mkdir()
        self.assertEqual(self.stop(session='outside', harness='codex', cwd=str(outside))['decision'], 'block')

    def test_bad_input_never_blocks(self):
        for raw in ('', 'not json', '[1]', json.dumps({'session_id': 3})):
            with self.subTest(raw=raw):
                self.assertIsNone(receipt_gate.respond('claude', 'Stop', raw))

    def test_agy_counts_idle_turns_and_reminds_once_at_the_next_invocation(self):
        def agy_stop(idle: bool):
            return receipt_gate.respond('agy', 'Stop', json.dumps({'conversationId': 'conv', 'fullyIdle': idle}))

        for _ in range(3):
            self.assertIsNone(agy_stop(False))  # agent loops inside one turn
        self.assertIsNone(agy_stop(True))
        self.assertEqual(agy_hook.respond(json.dumps({'invocationNum': 4, 'conversationId': 'conv'})), '{}')
        self.assertIsNone(agy_stop(True))
        steps = json.loads(agy_hook.respond(json.dumps({'invocationNum': 9, 'conversationId': 'conv'})))['injectSteps']
        text = steps[0]['ephemeralMessage']
        self.assertTrue(text.startswith('[Memory: Receipt] This conversation ran several turns'), text)
        self.assertIn('--harness agy', text)
        self.assertIn(f'"session": "{session_value("conv")}"', text)
        self.assertEqual(agy_hook.respond(json.dumps({'invocationNum': 10, 'conversationId': 'conv'})), '{}')
        self.assertIsNone(agy_stop(True))
        self.assertEqual(agy_hook.respond(json.dumps({'invocationNum': 11, 'conversationId': 'conv'})), '{}')

    def test_agy_reminder_is_dropped_when_a_receipt_arrived_meanwhile(self):
        for _ in range(2):
            receipt_gate.respond('agy', 'Stop', json.dumps({'conversationId': 'conv2', 'fullyIdle': True}))
        receipt(self.vault, 'c' * 64, '[Garden] done', kind='receipt', event_id='agy', harness='antigravity',
                refs=['AGENTS.md'], created_at='2026-10-01T10:00:00+00:00', session=session_value('conv2'))
        self.assertEqual(receipt_gate.agy_reminder('conv2'), '')

    def test_main_prints_the_block_and_a_bad_call_exits_1_never_2(self):
        self.edit(session='m', times=3)
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps({'session_id': 'm'}).encode('utf-8')), encoding='utf-8')
        with mock.patch.object(receipt_gate.sys, 'stdin', stdin), \
                mock.patch.object(receipt_gate.sys, 'stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(receipt_gate.main(['--harness', 'claude', '--event', 'Stop']), 0)
        self.assertEqual(json.loads(stdout.getvalue())['decision'], 'block')
        # Exit code 2 from a Stop or UserPromptSubmit hook would block the stop or the prompt.
        with mock.patch.object(receipt_gate.sys, 'stderr', new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as raised:
            receipt_gate.main(['--harness', 'claude'])
        self.assertEqual(raised.exception.code, 1)


if __name__ == '__main__':
    unittest.main()
