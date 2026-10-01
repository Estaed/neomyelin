"""prompt_recall.py: per-prompt recall over a fixture vault (bm25, the engine every user has)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, receipt, task, write, write_config

import prompt_recall
import recall

NOTES = {
    'knowledge/otter.md': '# Kelp forest\n\nThe sea otter wraps itself in kelp to sleep.\n',
    'knowledge/bread.md': '# Bread\n\nSourdough needs a starter fed daily.\n',
    'knowledge/tide.md': '# Tides\n\nThe moon pulls the sea twice a day.\n',
    'knowledge/garden.md': '# Garden\n\nThe tomatoes need water in the morning.\n',
    'knowledge/bike.md': '# Bike\n\nThe chain needs oil after the rain.\n',
    'knowledge/books.md': '# Books\n\nThe library closes at six on Sundays.\n',
    'knowledge/music.md': '# Music\n\nThe piano needs tuning in the spring.\n',
}
OTTER_PROMPT = 'Where did we write that the sea otter wraps itself in kelp?'


class RecallHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.vault, cfg = make_vault(base)
        self.refresh = mock.Mock()
        patches = [
            mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(base, cfg)),
                                         'NEOMYELIN_RECALL': 'bm25'}),
            mock.patch.object(recall, 'STATE_DIR', base / 'recall-state'),
            mock.patch.object(recall, 'OLLAMA_URL', 'http://127.0.0.1:9'),  # nothing listens there
            mock.patch.object(prompt_recall, 'STATE_DIR', base / 'recall-state'),
            mock.patch.object(prompt_recall, 'background_refresh', self.refresh),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for name in (prompt_recall.SMOKE_ENV, prompt_recall.INVOKED_ENV):
            os.environ.pop(name, None)
        for relative, text in NOTES.items():
            write(self.vault / relative, text)
        recall.build('bm25')

    def hook(self, prompt, session='s1', **extra) -> str:
        return prompt_recall.context_for(json.dumps({'session_id': session, 'prompt': prompt,
                                                     'cwd': str(self.vault), **extra}))

    def note(self, relative: str) -> str:
        return f'{self.vault.as_posix()}/{relative}'

    def test_a_phrase_from_a_note_returns_that_notes_path(self):
        text = self.hook(OTTER_PROMPT)
        lines = text.splitlines()
        self.assertEqual(lines[0], prompt_recall.HEADER)
        self.assertTrue(lines[1].startswith(f'1. {self.note("knowledge/otter.md")} ('), text)
        self.assertIn('— The sea otter wraps itself in kelp to sleep.', lines[1])
        self.assertNotIn('bread.md', text)
        self.refresh.assert_called_once_with()

    def test_an_unknown_prompt_abstains_and_says_so_only_the_first_time(self):
        self.assertEqual(self.hook('quantum chromodynamics and the gluon plasma'), prompt_recall.EMPTY)
        self.assertEqual(self.hook('Tell me about the weather tomorrow please'), '')

    def test_a_shared_common_word_is_not_a_hit(self):
        # "the" is in most notes; one shared common word must not pull a note in.
        self.assertEqual(self.hook('Tell me about the weather tomorrow please'), prompt_recall.EMPTY)

    def test_a_later_prompt_still_gets_a_real_hit(self):
        self.hook('quantum chromodynamics and the gluon plasma')
        self.assertIn(self.note('knowledge/otter.md'), self.hook(OTTER_PROMPT))

    def test_tasks_and_receipts_carry_their_state(self):
        task(self.vault, 'otter-survey', title='Otter survey in the kelp', status='active',
             next_action='count the sea otter rafts', due_at='2026-10-02')
        recall.build('bm25')
        text = self.hook('When is the sea otter survey in the kelp due?')
        self.assertIn(f'{self.note("tasks/otter-survey.md")} (', text)
        self.assertIn('[task active, due 2026-10-02]', text)

    def test_a_receipt_hit_is_a_dated_snapshot(self):
        receipt(self.vault, 'census', '[Coast] The sea otter census moved to the kelp bay',
                event_id='e1', created_at='2026-09-30T10:00:00+00:00', refs=['AGENTS.md'])
        recall.build('bm25')
        text = self.hook('When did the sea otter census move to the kelp bay?')
        self.assertIn(f'{self.note("receipts/census.md")} (', text)
        self.assertIn('[receipt 2026-09-30, historical]', text)

    def test_a_stamp_survives_an_unreadable_file(self):
        self.assertEqual(prompt_recall._stamp(self.vault, 'receipts/gone.md'), ' [receipt, historical]')
        self.assertEqual(prompt_recall._stamp(self.vault, 'tasks/gone.md'), ' [task]')
        self.assertEqual(prompt_recall._stamp(self.vault, 'knowledge/gone.md'), '')

    def test_silent_turns(self):
        cases = {
            'short': ('kelp otter', {}),
            'opt-out': (f'[no-record] {OTTER_PROMPT}', {}),
            'notification': ('<task-notification> ' + OTTER_PROMPT, {}),
            'not human': (OTTER_PROMPT, {'origin': {'kind': 'peer'}}),
        }
        for name, (prompt, extra) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.hook(prompt, session=name, **extra), '')
        self.assertEqual(prompt_recall.context_for(json.dumps({'prompt': OTTER_PROMPT})), '')  # no session
        self.assertEqual(prompt_recall.context_for('not json'), '')

    def test_the_same_prompt_twice_in_a_row_is_answered_once(self):
        self.assertIn('otter.md', self.hook(OTTER_PROMPT))
        self.assertEqual(self.hook(OTTER_PROMPT), '')
        self.assertIn('otter.md', self.hook(OTTER_PROMPT, session='other'))

    def test_a_missing_index_is_said_on_the_first_prompt(self):
        with mock.patch.object(recall, 'STATE_DIR', Path(self.tmp.name) / 'empty-state'):
            text = self.hook(OTTER_PROMPT)
        self.assertTrue(text.startswith('[Memory: Recall] Pre-check could not run (RecallError: index empty'), text)
        self.assertIn('/.brain/scripts/recall.py" "<question>"', text)
        self.assertTrue((Path(self.tmp.name) / 'recall-state' / 'errors.log').is_file())

    def test_the_smoke_nonce_rides_along_and_only_as_hex(self):
        with mock.patch.dict(os.environ, {prompt_recall.SMOKE_ENV: 'ab12cd34ef56'}):
            self.assertTrue(self.hook(OTTER_PROMPT).endswith('\n[Memory: Recall Check] ab12cd34ef56'))
        with mock.patch.dict(os.environ, {prompt_recall.SMOKE_ENV: 'not hex; rm -rf'}):
            self.assertNotIn('Recall Check', self.hook('quantum chromodynamics and the gluon plasma', session='x'))

    def test_main_prints_the_userpromptsubmit_shape(self):
        raw = json.dumps({'session_id': 'main', 'prompt': OTTER_PROMPT}).encode('utf-8')
        stdin = io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8')
        with mock.patch.object(prompt_recall.sys, 'stdin', stdin), \
                mock.patch.object(prompt_recall.sys, 'stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(prompt_recall.main(['--prompt-submit']), 0)
        output = json.loads(stdout.getvalue())['hookSpecificOutput']
        self.assertEqual(output['hookEventName'], 'UserPromptSubmit')
        self.assertIn(self.note('knowledge/otter.md'), output['additionalContext'])

    def test_a_bad_call_exits_1_never_2(self):
        # Exit code 2 from a UserPromptSubmit hook would block the user's prompt.
        with mock.patch.object(prompt_recall.sys, 'stderr', new_callable=io.StringIO), \
                self.assertRaises(SystemExit) as raised:
            prompt_recall.main([])
        self.assertEqual(raised.exception.code, 1)

    def test_the_layer_own_processes_stay_silent(self):
        with mock.patch.dict(os.environ, {prompt_recall.INVOKED_ENV: 'x'}), \
                mock.patch.object(prompt_recall.sys, 'stdout', new_callable=io.StringIO) as stdout:
            self.assertEqual(prompt_recall.main(['--prompt-submit']), 0)
        self.assertEqual(stdout.getvalue(), '')


class ParallelSessionTests(unittest.TestCase):
    """Another session prompting in the same folder is announced once; errors cost only the note."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault, cfg = make_vault(self.base)
        self.state = self.base / 'recall-state'
        patches = [
            mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(self.base, cfg)),
                                         'NEOMYELIN_RECALL': 'bm25'}),
            mock.patch.object(recall, 'STATE_DIR', self.state),
            mock.patch.object(recall, 'OLLAMA_URL', 'http://127.0.0.1:9'),
            mock.patch.object(prompt_recall, 'STATE_DIR', self.state),
            mock.patch.object(prompt_recall, 'background_refresh', mock.Mock()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for name in (prompt_recall.SMOKE_ENV, prompt_recall.INVOKED_ENV):
            os.environ.pop(name, None)
        for relative, text in NOTES.items():
            write(self.vault / relative, text)
        recall.build('bm25')
        self.folder = self.base / 'greenhouse'
        (self.folder / 'src').mkdir(parents=True)

    def hook(self, prompt: str, session: str, cwd: Path) -> str:
        return prompt_recall.context_for(json.dumps({'session_id': session, 'prompt': prompt, 'cwd': str(cwd)}))

    def notes(self, text: str) -> list[str]:
        return [line for line in text.splitlines() if line.startswith('[Parallel session]')]

    def test_each_other_session_is_announced_once(self):
        self.assertEqual(self.notes(self.hook('Fix the humidity sensor script', 'A', self.folder)), [])
        first_b = self.notes(self.hook('Write the watering schedule summary', 'B', self.folder / 'src'))
        self.assertEqual(first_b, [
            "[Parallel session] 1 more session(s) open in this folder: 'Fix the humidity sensor script' "
            '(0 min ago). Re-read a file from disk before you change it; git status before a commit.'])
        self.assertEqual(self.notes(self.hook('And now the fertiliser table please', 'B', self.folder)), [])
        second_a = self.notes(self.hook('Next, the sensor calibration constants', 'A', self.folder))
        self.assertEqual(len(second_a), 1)
        self.assertIn("'Write the watering schedule summary'", second_a[0])
        self.assertEqual(self.notes(self.hook('One more thing about the sensor wiring', 'A', self.folder)), [])
        self.assertEqual(self.notes(self.hook('Something unrelated in another folder', 'C', self.base / 'other')), [])

    def test_an_idle_session_is_not_announced(self):
        self.hook('Fix the humidity sensor script', 'A', self.folder)
        marker = prompt_recall._marker('A')
        data = json.loads(marker.read_text(encoding='utf-8'))
        data['updated_at'] -= prompt_recall.PARALLEL_STALE_SECONDS + 60
        marker.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(self.notes(self.hook('Write the watering schedule summary', 'B', self.folder)), [])

    def test_the_note_rides_along_with_recall_hits(self):
        self.hook('Fix the humidity sensor script', 'A', self.folder)
        text = self.hook(OTTER_PROMPT, 'B', self.folder)
        self.assertTrue(text.startswith(prompt_recall.HEADER), text)
        self.assertEqual(len(self.notes(text)), 1)

    def test_a_broken_check_costs_only_the_note(self):
        self.hook('Fix the humidity sensor script', 'A', self.folder)
        with mock.patch.object(prompt_recall, '_same_workspace', side_effect=RuntimeError('boom')):
            text = self.hook(OTTER_PROMPT, 'B', self.folder)
        self.assertIn('otter.md', text)
        self.assertEqual(self.notes(text), [])
        self.assertIn('parallel note: RuntimeError: boom', (self.state / 'errors.log').read_text(encoding='utf-8'))

    def test_without_a_cwd_there_is_no_note(self):
        prompt_recall.context_for(json.dumps({'session_id': 'A', 'prompt': 'Fix the humidity sensor script'}))
        text = prompt_recall.context_for(json.dumps({'session_id': 'B', 'prompt': 'Write the watering schedule'}))
        self.assertEqual(self.notes(text), [])


if __name__ == '__main__':
    unittest.main()
