"""Transcript adapters read only human messages and check today's exact quote."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, write

import habits


class HabitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / 'home'
        self.vault, self.cfg = make_vault(Path(temporary.name))
        self.cfg['harnesses'] = ['claude', 'codex', 'agy']
        self.day = dt.date(2026, 9, 30)
        self.stamp = '2026-09-30T12:00:00+09:30'

    def test_three_harnesses_and_hidden_machine_messages(self):
        claude = self.home / '.claude' / 'projects' / 'sample' / 's.jsonl'
        write(claude, '\n'.join(json.dumps(item) for item in [
            {'type': 'user', 'entrypoint': 'cli', 'timestamp': self.stamp,
             'message': {'content': 'Please show the result first'}},
            {'type': 'user', 'entrypoint': 'sdk-cli', 'timestamp': self.stamp,
             'message': {'content': 'Hidden model prompt'}},
        ]))
        codex = self.home / '.codex' / 'sessions' / '2026' / '09' / '30' / 's.jsonl'
        write(codex, '\n'.join(json.dumps(item) for item in [
            {'type': 'session_meta', 'payload': {'originator': 'codex', 'source': 'cli'}},
            {'type': 'response_item', 'timestamp': self.stamp,
             'payload': {'role': 'user', 'content': [{'text': 'Can you measure that case?'}]}},
        ]))
        agy = self.home / '.gemini' / 'tmp' / 'sample' / 'chats' / 's.json'
        write(agy, json.dumps({'messages': [
            {'type': 'user', 'timestamp': self.stamp,
             'content': [{'text': 'I want one concrete example'}]},
            {'type': 'gemini', 'timestamp': self.stamp,
             'content': [{'text': 'Hidden assistant reply'}]},
        ]}))
        items = habits.messages({self.day}, self.cfg, self.home)
        self.assertEqual({item[1] for item in items}, {'claude', 'codex', 'agy'})
        self.assertEqual(len(items), 3)
        self.assertEqual(habits.validated(
            [{'behavior': 'Asks for a result', 'quote': 'Please show the result first'}], items),
            [('Asks for a result', 'Please show the result first')])
        self.assertEqual(habits.validated(
            [{'behavior': 'Asks for a result', 'quote': 'show the result first please'}], items), [])

    def test_agy_cli_reads_typed_history_not_headless_transcripts(self):
        noon = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=9, minutes=30)))
        ms = int(noon.timestamp() * 1000)
        cli = self.home / '.gemini' / 'antigravity-cli'
        write(cli / 'history.jsonl', '\n'.join(json.dumps(item) for item in [
            {'display': 'Keep the answer short', 'timestamp': ms, 'workspace': 'C:\\work'},
            {'display': '/model', 'timestamp': ms, 'workspace': 'C:\\work', 'type': 'slash_command'},
            {'display': 'A typed message from another day', 'timestamp': ms - 3 * 86400000},
        ]))
        # `agy -p` calls land only in the per-conversation transcript; they are machine prompts.
        write(cli / 'brain' / 'c1' / '.system_generated' / 'logs' / 'transcript.jsonl', json.dumps(
            {'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'created_at': '2026-09-30T02:30:00Z',
             'content': '<USER_REQUEST>\nAutomated nightly prompt\n</USER_REQUEST>'}))
        self.cfg['harnesses'] = ['agy']
        items = habits.messages({self.day}, self.cfg, self.home)
        self.assertEqual([(item[1], item[2]) for item in items], [('agy', 'Keep the answer short')])

    def test_dry_run_prints_prompt_without_model_or_receipt(self):
        source = self.home / '.claude' / 'projects' / 'sample' / 's.jsonl'
        write(source, json.dumps({'type': 'user', 'entrypoint': 'cli', 'timestamp': self.stamp,
                                  'message': {'content': 'Please show the result first'}}))
        with mock.patch.object(habits.stage, 'ask_json') as ask:
            self.assertEqual(habits.run(self.cfg, day=self.day, dry_run=True,
                                            home=self.home), 0)
        ask.assert_not_called()
        self.assertFalse((self.vault / 'receipts').exists())

    def test_prompt_limits_earlier_context(self):
        today = [(self.stamp, 'claude', 'Please show the result first')]
        earlier = [(self.stamp, 'claude', 'Earlier message ' * 10_000)]
        text = habits.prompt(self.day, today, earlier, self.cfg)
        self.assertLess(len(text), habits.MAX_CONTEXT + 3000)

    def test_identical_messages_on_different_days_are_retained(self):
        source = self.home / '.claude' / 'projects' / 'sample' / 's.jsonl'
        write(source, '\n'.join(json.dumps({'type': 'user', 'entrypoint': 'cli',
                                              'timestamp': f'2026-09-{day:02d}T12:00:00+09:30',
                                              'message': {'content': 'Please show one example'}})
                                 for day in (29, 30)))
        items = habits.messages({self.day, self.day - dt.timedelta(days=1)},
                                self.cfg, self.home)
        self.assertEqual(len(items), 2)


if __name__ == '__main__':
    unittest.main()
