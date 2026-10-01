"""agy_hook.py: the Antigravity PreInvocation adapter's output (not its registration)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, write_config

import agy_hook
import config
import memory_context


class AgyHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.vault, cfg = make_vault(base)
        self.env = mock.patch.dict(os.environ, {config.CONFIG_ENV: str(write_config(base, cfg))})
        self.env.start()
        self.warm = mock.patch.object(memory_context, '_start_recall_warmup')
        self.warm.start()
        self.nightly = mock.patch.object(memory_context, '_start_nightly')
        self.nightly.start()

    def tearDown(self):
        self.nightly.stop()
        self.warm.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_first_invocation_gets_the_context_as_an_ephemeral_message(self):
        raw = json.dumps({'invocationNum': 0, 'conversationId': 'conv-1', 'cwd': str(self.vault)})
        steps = json.loads(agy_hook.respond(raw))['injectSteps']
        self.assertEqual(len(steps), 1)
        text = steps[0]['ephemeralMessage']
        self.assertIn("[Memory: Identity] You are Nova, Alex's assistant.", text)
        self.assertIn('[Memory: Rules]\n# Rules\n\nAlways cite the source.', text)  # inside the vault too
        self.assertIn(hashlib.sha256(b'conv-1').hexdigest()[:24], text)

    def test_later_invocations_and_bad_input_print_an_empty_object(self):
        for raw in (json.dumps({'invocationNum': 1, 'conversationId': 'c'}), json.dumps({'conversationId': 'c'}),
                    '', 'not json', '[0]'):
            with self.subTest(raw=raw):
                self.assertEqual(agy_hook.respond(raw), '{}')

    def test_a_broken_config_is_a_visible_warning(self):
        with mock.patch.dict(os.environ, {config.CONFIG_ENV: str(Path(self.tmp.name) / 'missing.json')}):
            text = json.loads(agy_hook.respond(json.dumps({'invocationNum': 0})))['injectSteps'][0]['ephemeralMessage']
        self.assertTrue(text.startswith('[Memory warning]'), text)

    def test_the_layer_own_processes_stay_silent(self):
        with mock.patch.dict(os.environ, {agy_hook.INVOKED_ENV: 'x'}), \
                mock.patch.object(agy_hook.sys, 'stdout') as stdout:
            self.assertEqual(agy_hook.main(), 0)
        stdout.write.assert_called_once_with('{}\n')
        self.assertEqual(agy_hook.INVOKED_ENV, memory_context.INVOKED_ENV)


if __name__ == '__main__':
    unittest.main()
