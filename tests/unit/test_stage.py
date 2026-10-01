"""The model's disposable stage enforces its write boundary and reports failures."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault
import stage


class StageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault, self.cfg = make_vault(Path(temporary.name))

    def test_stage_is_disposed_and_input_is_copied(self):
        original = self.vault / 'source.md'
        original.write_text('A quoted observation.\n', encoding='utf-8')
        visited = []

        def answer(_prompt, *, cfg, **_kwargs):
            work = Path(cfg['vault'])
            visited.append(work)
            self.assertEqual((work / 'sources' / 'source.md').read_text(encoding='utf-8'),
                             original.read_text(encoding='utf-8'))
            self.assertEqual((work / 'instruction_prompt.txt').read_text(encoding='utf-8'),
                             'Read the observation.')
            return '[]'

        with mock.patch.object(stage.engine, 'ask', side_effect=answer):
            self.assertEqual(stage.ask_json('Read the observation.', self.cfg,
                                            source_files=[(original, 'sources/source.md')]), [])
        self.assertFalse(visited[0].exists())

    def test_unapproved_stage_write_fails_and_warns(self):
        def answer(_prompt, *, cfg, **_kwargs):
            (Path(cfg['vault']) / 'instruction_prompt.txt').write_text('Changed', encoding='utf-8')
            return '[]'

        with mock.patch.object(stage.engine, 'ask', side_effect=answer):
            with self.assertRaisesRegex(RuntimeError, 'outside the allowed output'):
                stage.ask_json('Original', self.cfg)
        health = self.vault / '.brain' / '.state' / 'health.json'
        self.assertIn('model changed instruction_prompt.txt', health.read_text(encoding='utf-8'))

    def test_output_file_is_allowed(self):
        def answer(_prompt, *, cfg, **_kwargs):
            (Path(cfg['vault']) / 'candidate.json').write_text('[]', encoding='utf-8')
            return ''

        with mock.patch.object(stage.engine, 'ask', side_effect=answer):
            self.assertEqual(stage.ask_json('Request', self.cfg), [])

    def test_lock_rejects_concurrent_run(self):
        path = self.vault / '.brain' / '.state' / 'evolution.lock'
        with stage.exclusive_lock(path) as first:
            with stage.exclusive_lock(path) as second:
                self.assertTrue(first)
                self.assertFalse(second)


if __name__ == '__main__':
    unittest.main()
