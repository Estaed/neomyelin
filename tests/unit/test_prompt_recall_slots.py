"""prompt_recall.py: knowledge notes get their own slots, the other slots never hold a receipt, later
prompts keep hits from 0.52, seven full lines fit under the cap, the background refresh opens no
window."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, receipt, task, write, write_config

import prompt_recall
import recall


def row(path: str, score: float) -> dict:
    return {'path': path, 'score': score, 'preview': f'preview of {path}'}


# recall's order (best first): receipts and a task outscore every knowledge note, and five files
# that are neither compete for the four other slots.
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
    row('receipts/e.md', 0.495),
    row('500-Projects/card.md', 0.49),
    row('200-Goals/focus.md', 0.48),
    row('tasks/f.md', 0.47),
]
WITHOUT_KNOWLEDGE = [
    row('receipts/a.md', 0.70),
    row('tasks/c.md', 0.60),
    row('.brain/skills/chef/SKILL.md', 0.53),
    row('500-Projects/card.md', 0.50),
]
ONLY_RECEIPTS = [row('receipts/a.md', 0.70), row('receipts/b.md', 0.60)]


def paths(rows: list[dict]) -> list[str]:
    return [item['path'] for item in rows]


class SlotTests(unittest.TestCase):
    """The split over a fake result list, with knowledge hits present and absent."""

    def find(self, rows: list[dict], first: bool, engine: str = 'ollama') -> tuple[list[str], mock.Mock]:
        search = mock.Mock(return_value=(engine, [dict(item) for item in rows]))
        with mock.patch.object(recall, 'search', search):
            return paths(prompt_recall.find('where did we write about the heron nest', first)), search

    def test_knowledge_first_then_four_others_and_no_receipt(self):
        self.assertEqual(paths(prompt_recall.slots(WITH_KNOWLEDGE)), [
            'knowledge/concepts/k1.md', 'knowledge/concepts/k2.md', 'knowledge/concepts/k3.md',
            'tasks/c.md', '.brain/skills/chef/SKILL.md', '500-Projects/card.md', '200-Goals/focus.md'])

    def test_without_knowledge_the_others_keep_their_slots(self):
        self.assertEqual(paths(prompt_recall.slots(WITHOUT_KNOWLEDGE)),
                         ['tasks/c.md', '.brain/skills/chef/SKILL.md', '500-Projects/card.md'])
        self.assertEqual(prompt_recall.slots([]), [])

    def test_receipts_alone_show_nothing(self):
        self.assertEqual(prompt_recall.slots(ONLY_RECEIPTS), [])
        self.assertEqual(self.find(ONLY_RECEIPTS, first=True)[0], [])

    def test_fewer_knowledge_notes_than_slots(self):
        rows = [row('receipts/a.md', 0.7), row('knowledge/k.md', 0.6), row('tasks/c.md', 0.55),
                row('receipts/d.md', 0.5)]
        self.assertEqual(paths(prompt_recall.slots(rows)), ['knowledge/k.md', 'tasks/c.md'])

    def test_the_search_goes_wide(self):
        _, search = self.find(WITH_KNOWLEDGE, first=True)
        self.assertEqual(search.call_args.kwargs.get('k'), 40)
        self.assertIs(search.call_args.kwargs.get('refresh'), False)

    def test_first_prompt_slots_with_and_without_knowledge(self):
        self.assertEqual(self.find(WITH_KNOWLEDGE, first=True)[0], [
            'knowledge/concepts/k1.md', 'knowledge/concepts/k2.md', 'knowledge/concepts/k3.md',
            'tasks/c.md', '.brain/skills/chef/SKILL.md', '500-Projects/card.md', '200-Goals/focus.md'])
        self.assertEqual(self.find(WITHOUT_KNOWLEDGE, first=True)[0],
                         ['tasks/c.md', '.brain/skills/chef/SKILL.md', '500-Projects/card.md'])

    def test_later_prompts_keep_hits_from_0_52(self):
        # 0.55 and 0.52 stay (0.58, the earlier bar, hid them); 0.519 does not.
        self.assertEqual(self.find(WITH_KNOWLEDGE, first=False)[0], [
            'knowledge/concepts/k1.md', 'knowledge/concepts/k2.md', 'knowledge/concepts/k3.md', 'tasks/c.md'])
        later = [row('knowledge/k.md', 0.519), row('receipts/a.md', 0.53), row('tasks/c.md', 0.52),
                 row('receipts/b.md', 0.51)]
        self.assertEqual(self.find(later, first=False)[0], ['tasks/c.md'])
        self.assertEqual(self.find(WITHOUT_KNOWLEDGE, first=False)[0], ['tasks/c.md', '.brain/skills/chef/SKILL.md'])


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
    'kite': 'A kite needs a steady wind on the hill.',
    'soup': 'Lentil soup thickens as it cools.',
}
LONG = ('Heron nest reeds marsh: {n} field notes kept over several seasons by the volunteers, with '
        'counts of eggs, fledglings, water levels, weather, predators, ringing codes and the observers '
        'who walked the {n} transect at dawn.')
# Few distinct words, so each receipt outscores every knowledge note under the strict rule.
RECEIPTS = ('Heron nest reeds marsh.', 'Heron nest reeds marsh and the boardwalk repairs.',
            'Heron nest reeds marsh and the boardwalk repairs before the winter storms.')
PROMPT = 'Where is the heron nest in the marsh reeds?'
SKILL = ('---\nname: marsh-walk\ndescription: The marsh-walk skill.\n---\n\n# Marsh walk\n\n'
         'Plan a dawn walk to the heron nest through the marsh reeds and log what was seen.\n')


class HookSlotTests(unittest.TestCase):
    """The hook end to end on bm25: receipts outscore everything else and still take no line."""

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
        # Five files that are neither knowledge notes nor receipts, for four slots.
        task(self.vault, 'heron-survey', title='Heron nest survey in the marsh reeds', status='active',
             next_action='count the nests from the hide at first light, note the chicks, the ringed adults, '
             'the wind, the rain gauge and any fox tracks on the path; then upload the sheet, email the '
             'warden, order new pencils, clean the scope, charge the radio and book the minibus for '
             'Saturday', due_at='2026-11-20')
        task(self.vault, 'heron-boardwalk', title='Boardwalk to the heron nest across the marsh reeds and the '
             'ditch, with a new railing, signs for the visitors and a gate at the car park', status='waiting')
        write(self.vault / '.brain' / 'skills' / 'marsh-walk' / 'SKILL.md', SKILL)
        write(self.vault / '500-Projects' / 'heron-reserve.md',
              '# Heron reserve\n\nThe reserve project: the heron nest colony in the marsh reeds, its funding, '
              'its volunteers and the long plan for the water levels, the sluice gates, the grazing cattle, '
              'the bird hide, the school visits and the yearly report to the council. Grants, insurance, '
              'fencing, trail maps, a donation box, volunteer rotas and a newsletter round it off.\n')
        write(self.vault / '600-Life' / 'birding.md',
              '# Birding\n\nWeekend birding: the heron nest by the marsh reeds, the owl box in the barn, the '
              'swifts under the roof, the feeders in the garden, the notebook of first sightings, the '
              'binoculars, the camera lens, the spring migration and the autumn geese. Thermos, raincoat, '
              'wellies, field guide, snacks, a folding stool and patience for the long quiet hours.\n')
        recall.build('bm25')

    def hook(self, prompt: str = PROMPT, session: str = 's1') -> str:
        return prompt_recall.context_for(json.dumps({'session_id': session, 'prompt': prompt,
                                                     'cwd': str(self.vault)}))

    def shown(self, text: str) -> list[str]:
        root = self.vault.as_posix() + '/'
        return [line.split(' (', 1)[0].split('. ', 1)[1].removeprefix(root) for line in text.splitlines()[1:]]

    def test_the_receipts_do_outscore_everything_else(self):
        # The fixture's premise: the two best hits are receipts, every receipt outscores every
        # knowledge note, and five other files compete for four slots.
        ranked = [item['path'] for item in prompt_recall.strict_filter(
            PROMPT, recall.search('heron nest marsh reeds', k=40, refresh=False)[1])]
        self.assertEqual(ranked[:2], ['receipts/heron-1.md', 'receipts/heron-2.md'])
        self.assertLess(ranked.index('receipts/heron-3.md'), ranked.index('knowledge/concepts/heron-1.md'))
        self.assertEqual(len([path for path in ranked if not path.startswith(('receipts/', 'knowledge/'))]), 5)
        self.assertEqual(len(ranked), 12)

    def test_three_knowledge_notes_then_four_others_and_no_receipt(self):
        text = self.hook()
        lines = text.splitlines()
        self.assertEqual(lines[0], prompt_recall.HEADER)
        self.assertIn('knowledge notes first, then tasks, skills, project cards and hub files', lines[0])
        self.assertEqual(self.shown(text), [
            'knowledge/concepts/heron-1.md', 'knowledge/concepts/heron-2.md', 'knowledge/concepts/heron-3.md',
            '600-Life/birding.md', '500-Projects/heron-reserve.md', 'tasks/heron-survey.md',
            '.brain/skills/marsh-walk/SKILL.md'], text)
        self.assertNotIn('/receipts/', text)
        self.assertIn('[task active, due 2026-11-20]', text)
        self.assertNotIn('[…]', text)

    def test_with_no_knowledge_note_the_other_files_still_show(self):
        shutil.rmtree(self.vault / 'knowledge')
        recall.build('bm25')
        self.assertEqual(self.shown(self.hook()), [
            '600-Life/birding.md', '500-Projects/heron-reserve.md', 'tasks/heron-survey.md',
            '.brain/skills/marsh-walk/SKILL.md'])

    def test_a_vault_whose_only_hits_are_receipts_shows_nothing(self):
        for folder in ('knowledge', 'tasks', '.brain', '500-Projects', '600-Life'):
            shutil.rmtree(self.vault / folder)
        recall.build('bm25')
        self.assertEqual([row['path'] for row in prompt_recall.strict_filter(
            PROMPT, recall.search(PROMPT, k=40, refresh=False)[1])],
            ['receipts/heron-1.md', 'receipts/heron-2.md', 'receipts/heron-3.md'])
        self.assertEqual(self.hook(), prompt_recall.EMPTY)


class CapTests(unittest.TestCase):
    """Seven hits at full preview length, with task stamps and long paths, arrive whole."""

    def test_seven_full_lines_fit_under_the_cap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        vault = Path(tmp.name) / 'vault'
        hits = []
        for number in range(1, 8):
            folder = 'knowledge/concepts' if number <= 3 else 'tasks'
            # Each line's full path is 190 characters, whatever the temporary folder's length.
            name = f'{number}-'.ljust(190 - len(f'{vault.as_posix()}/{folder}/.md'), 'n')
            self.assertGreater(len(name), 40)
            if folder == 'tasks':
                task(vault, name, title=f'Task {number}', status='waiting', due_at='2026-12-01')
            preview = f'Preview {number}: ' + ' '.join(['wordy'] * 60)  # longer than PREVIEW_CHARS
            hits.append({'path': f'{folder}/{name}.md', 'score': 0.9 - number / 100, 'preview': preview})
        text = prompt_recall.format_hits(vault, hits)
        lines = text.splitlines()
        self.assertEqual(len(lines), 8, text)
        self.assertNotIn('[…]', text)
        for number, (line, hit) in enumerate(zip(lines[1:], hits), 1):
            with self.subTest(number):
                self.assertEqual(len(line.split(' (', 1)[0].split('. ', 1)[1]), 190)
                self.assertTrue(line.endswith('— ' + hit['preview'][:prompt_recall.PREVIEW_CHARS]), line)
                if number > 3:
                    self.assertIn('[task waiting, due 2026-12-01]', line)


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
