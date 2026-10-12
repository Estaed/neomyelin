"""prompt_recall.py: knowledge notes get their own slots, later prompts keep hits from 0.52, the
background refresh opens no window."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, receipt, write, write_config

import prompt_recall
import recall


def row(path: str, score: float) -> dict:
    return {'path': path, 'score': score, 'preview': f'preview of {path}'}


# recall's order (best first): receipts and a task outscore every knowledge note.
WITH_KNOWLEDGE = [
    row('receipts/a.md', 0.70),
    row('receipts/b.md', 0.69),
    row('tasks/c.md', 0.66),
    row('knowledge/concepts/k1.md', 0.60),
    row('receipts/d.md', 0.59),
    row('knowledge/concepts/k2.md', 0.55),
    row('knowledge/concepts/k3.md', 0.52),
    row('knowledge/concepts/k4.md', 0.519),
    row('.brain/skills/chef/SKILL.md', 0.50),
]
WITHOUT_KNOWLEDGE = [
    row('receipts/a.md', 0.70),
    row('tasks/c.md', 0.60),
    row('.brain/skills/chef/SKILL.md', 0.53),
    row('500-Projects/card.md', 0.50),
]


def paths(rows: list[dict]) -> list[str]:
    return [item['path'] for item in rows]


class SlotTests(unittest.TestCase):
    """The split over a fake result list, with knowledge hits present and absent."""

    def find(self, rows: list[dict], first: bool, engine: str = 'ollama') -> tuple[list[str], mock.Mock]:
        search = mock.Mock(return_value=(engine, [dict(item) for item in rows]))
        with mock.patch.object(recall, 'search', search):
            return paths(prompt_recall.find('where did we write about the heron nest', first)), search

    def test_knowledge_first_then_two_others(self):
        self.assertEqual(paths(prompt_recall.slots(WITH_KNOWLEDGE)), [
            'knowledge/concepts/k1.md', 'knowledge/concepts/k2.md', 'knowledge/concepts/k3.md',
            'receipts/a.md', 'receipts/b.md'])

    def test_without_knowledge_the_others_keep_their_slots(self):
        self.assertEqual(paths(prompt_recall.slots(WITHOUT_KNOWLEDGE)), ['receipts/a.md', 'tasks/c.md'])
        self.assertEqual(prompt_recall.slots([]), [])

    def test_fewer_knowledge_notes_than_slots(self):
        rows = [row('receipts/a.md', 0.7), row('knowledge/k.md', 0.6), row('tasks/c.md', 0.55),
                row('receipts/d.md', 0.5)]
        self.assertEqual(paths(prompt_recall.slots(rows)), ['knowledge/k.md', 'receipts/a.md', 'tasks/c.md'])

    def test_the_search_goes_wide(self):
        _, search = self.find(WITH_KNOWLEDGE, first=True)
        self.assertEqual(search.call_args.kwargs.get('k'), 40)
        self.assertIs(search.call_args.kwargs.get('refresh'), False)

    def test_first_prompt_slots_with_and_without_knowledge(self):
        self.assertEqual(self.find(WITH_KNOWLEDGE, first=True)[0], [
            'knowledge/concepts/k1.md', 'knowledge/concepts/k2.md', 'knowledge/concepts/k3.md',
            'receipts/a.md', 'receipts/b.md'])
        self.assertEqual(self.find(WITHOUT_KNOWLEDGE, first=True)[0], ['receipts/a.md', 'tasks/c.md'])

    def test_later_prompts_keep_hits_from_0_52(self):
        # 0.55 and 0.52 stay (0.58, the earlier bar, hid them); 0.519 does not.
        self.assertEqual(self.find(WITH_KNOWLEDGE, first=False)[0], [
            'knowledge/concepts/k1.md', 'knowledge/concepts/k2.md', 'knowledge/concepts/k3.md',
            'receipts/a.md', 'receipts/b.md'])
        later = [row('knowledge/k.md', 0.519), row('receipts/a.md', 0.53), row('tasks/c.md', 0.52),
                 row('receipts/b.md', 0.51)]
        self.assertEqual(self.find(later, first=False)[0], ['receipts/a.md', 'tasks/c.md'])
        self.assertEqual(self.find(WITHOUT_KNOWLEDGE, first=False)[0], ['receipts/a.md', 'tasks/c.md'])


FILLER = {
    'bread': 'Sourdough needs a starter fed daily.',
    'tide': 'The moon pulls the sea twice a day.',
    'garden': 'Tomatoes need water in the morning.',
    'bike': 'The chain needs oil after rain.',
    'books': 'The library closes at six on Sundays.',
    'music': 'The piano needs tuning in spring.',
    'tea': 'Green tea steeps for two minutes.',
    'car': 'Tyres lose pressure in cold weather.',
    'desk': 'A standing desk helps the back.',
    'cat': 'The cat sleeps on the warm laptop.',
}
LONG = ('Heron nest reeds marsh: {n} field notes kept over several seasons by the volunteers, with '
        'counts of eggs, fledglings, water levels, weather, predators, ringing codes and the observers '
        'who walked the {n} transect at dawn.')
# Few distinct words, so each receipt outscores every knowledge note under the strict rule.
RECEIPTS = ('Heron nest reeds marsh.', 'Heron nest reeds marsh and the boardwalk repairs.',
            'Heron nest reeds marsh and the boardwalk repairs before the winter storms.')


class HookSlotTests(unittest.TestCase):
    """The hook end to end on bm25: receipts outscore the knowledge notes and still come second."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.vault, cfg = make_vault(base)
        patches = [
            mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(base, cfg)),
                                         'NEOMYELIN_RECALL': 'bm25'}),
            mock.patch.object(recall, 'STATE_DIR', base / 'recall-state'),
            mock.patch.object(recall, 'OLLAMA_URL', 'http://127.0.0.1:9'),  # nothing listens there
            mock.patch.object(prompt_recall, 'STATE_DIR', base / 'recall-state'),
            mock.patch.object(prompt_recall, 'background_refresh', mock.Mock()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for name in (prompt_recall.SMOKE_ENV, prompt_recall.INVOKED_ENV):
            os.environ.pop(name, None)
        for name, text in FILLER.items():
            write(self.vault / 'notes' / f'{name}.md', f'# {name}\n\n{text}\n')
        for number, words in enumerate(('first', 'second', 'third', 'fourth and the longest of all'), 1):
            write(self.vault / 'knowledge' / 'concepts' / f'heron-{number}.md',
                  f'# Heron {number}\n\n{LONG.format(n=words)}\n')
        for number, sentence in enumerate(RECEIPTS, 1):
            receipt(self.vault, f'heron-{number}', ' '.join([sentence] * 3),
                    created_at=f'2026-10-0{number}T10:00:00+00:00')
        recall.build('bm25')

    def test_three_knowledge_notes_then_two_receipts_uncut(self):
        text = prompt_recall.context_for(json.dumps({
            'session_id': 's1', 'prompt': 'Where is the heron nest in the marsh reeds?', 'cwd': str(self.vault)}))
        lines = text.splitlines()
        self.assertEqual(lines[0], prompt_recall.HEADER)
        self.assertIn('knowledge notes first, then receipts, tasks and other files', lines[0])
        root = self.vault.as_posix()
        shown = [line.split(' (', 1)[0].split('. ', 1)[1] for line in lines[1:]]
        self.assertEqual(shown, [f'{root}/knowledge/concepts/heron-1.md', f'{root}/knowledge/concepts/heron-2.md',
                                 f'{root}/knowledge/concepts/heron-3.md', f'{root}/receipts/heron-1.md',
                                 f'{root}/receipts/heron-2.md'], text)
        # Five hits with full previews fit under the cap (the old 1200 cut the last ones).
        self.assertGreater(len(text), 1300)
        self.assertNotIn('[…]', text)
        self.assertTrue(lines[-1].endswith(f"— {' '.join([RECEIPTS[1]] * 3)}"), lines[-1])

    def test_the_receipts_do_outscore_the_knowledge_notes(self):
        # The fixture's premise: in one mixed top 3 the knowledge notes would not appear.
        ranked = prompt_recall.strict_filter('Where is the heron nest in the marsh reeds?',
                                             recall.search('heron nest marsh reeds', k=40, refresh=False)[1])
        self.assertEqual([item['path'] for item in ranked[:3]],
                         ['receipts/heron-1.md', 'receipts/heron-2.md', 'receipts/heron-3.md'])
        self.assertEqual(len(ranked), 7)


class BackgroundRefreshTests(unittest.TestCase):
    FLAGS = {'DETACHED_PROCESS': 0x00000008, 'CREATE_NEW_PROCESS_GROUP': 0x00000200,
             'CREATE_NO_WINDOW': 0x08000000}

    def refresh(self, os_name: str) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        popen = mock.Mock()
        patches = [mock.patch.object(prompt_recall, 'STATE_DIR', Path(tmp.name) / 'state'),
                   mock.patch.object(prompt_recall.subprocess, 'Popen', popen)]
        # The flags exist only on Windows; the same values let this run on any platform.
        patches += [mock.patch.object(subprocess, name, value, create=True) for name, value in self.FLAGS.items()]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(prompt_recall.os, 'name', os_name):
            prompt_recall.background_refresh()
        popen.assert_called_once()
        return popen.call_args.kwargs

    def test_windows_gets_no_console_window(self):
        options = self.refresh('nt')
        self.assertEqual(options['creationflags'], self.FLAGS['CREATE_NO_WINDOW'] | self.FLAGS['CREATE_NEW_PROCESS_GROUP'])
        self.assertFalse(options['creationflags'] & self.FLAGS['DETACHED_PROCESS'])
        self.assertEqual((options['stdin'], options['stdout'], options['stderr']), (subprocess.DEVNULL,) * 3)

    def test_elsewhere_a_new_session(self):
        options = self.refresh('posix')
        self.assertTrue(options['start_new_session'])
        self.assertNotIn('creationflags', options)


if __name__ == '__main__':
    unittest.main()
