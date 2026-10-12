"""A prompt naming a skill in the vault's hub gets one `[Skill]` line; recall indexes each SKILL.md."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, write, write_config

import config
import prompt_recall
import recall
import skills_hub

SKILL = '---\nname: {name}\ndescription: The {name} skill.\n---\n\n# {title}\n\n{body}\n'
OTTER = 'Where did we write that the sea otter wraps itself in kelp?'


class SkillHintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault, self.cfg = make_vault(self.base)
        self.state = self.base / 'recall-state'
        patches = [
            mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(self.base, self.cfg)),
                                         'NEOMYELIN_RECALL': 'bm25'}),
            mock.patch.object(recall, 'STATE_DIR', self.state),
            mock.patch.object(recall, 'OLLAMA_URL', 'http://127.0.0.1:9'),  # nothing listens there
            mock.patch.object(prompt_recall, 'STATE_DIR', self.state),
            mock.patch.object(prompt_recall, 'background_refresh', mock.Mock()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for name in (prompt_recall.SMOKE_ENV, prompt_recall.INVOKED_ENV):
            os.environ.pop(name, None)
        self.hub = skills_hub.hub(self.vault)
        for name, body in (('chef', 'Pick the model and effort before spawning a helper agent.'),
                           ('otopilot', 'Run a wave of written tasks overnight.'),
                           ('plan-wave', 'Write the next wave of tasks.'),
                           ('limit', 'Show the quota windows.')):
            write(self.hub / name / 'SKILL.md', SKILL.format(name=name, title=name.title(), body=body))
        write(self.hub / 'chef' / 'references' / 'models.md', '# Models\n\nThe helper agent model table.\n')
        write(self.hub / 'notaskill' / 'README.md', '# Not a skill\n\nNo SKILL.md here.\n')
        write(self.hub / '.hidden' / 'SKILL.md', SKILL.format(name='hidden', title='Hidden', body='Hidden.'))
        write(self.vault / '.brain' / 'notes.md', '# Layer notes\n\nThe helper agent spawning log.\n')
        write(self.vault / 'knowledge' / 'otter.md', '# Kelp forest\n\nThe sea otter wraps itself in kelp to sleep.\n')
        for name in ('bread', 'tide', 'garden', 'bike'):
            write(self.vault / 'knowledge' / f'{name}.md', f'# {name}\n\nA note about the {name}.\n')
        recall.build('bm25')

    def skill_md(self, name: str) -> str:
        return f'`{self.vault.as_posix()}/.brain/skills/{name}/SKILL.md`'

    def line(self, prompt: str, cfg: dict | None = None) -> str:
        return prompt_recall.skill_line(prompt, self.vault, cfg or self.cfg)

    def hook(self, prompt: str, session: str = 's1', **extra) -> str:
        return prompt_recall.context_for(json.dumps({'session_id': session, 'prompt': prompt,
                                                     'cwd': str(self.vault), **extra}))

    def test_a_named_skill_points_at_its_skill_md_by_absolute_path(self):
        self.assertEqual(self.line('How does chef pick a model for a bee?'),
                         f'[Skill] This turn names chef. Before describing it or proposing a change to it, '
                         f'read {self.skill_md("chef")} (and the reference it points to): say what already '
                         'exists first.')
        path = Path(self.skill_md('chef').strip('`'))
        self.assertTrue(path.is_absolute())
        self.assertTrue(path.is_file())

    def test_whole_word_any_case_not_inside_a_word_or_a_path(self):
        fires = ('How does Chef work?', 'what does CHEF do', 'Use chef.', 'ask (chef) first', 'the `chef` skill',
                 "chef's rule", 'chef: explain', 'chef, then the rest', 'ÇAY and chef')
        silent = ('chefs are cooks', 'the sous-chef said', 'a chef-like role', 'mischef', 'chef_x', 'chefé',
                  '.brain/skills/chef/SKILL.md', 'see skills/chef/', 'run chef.py', 'C:\\work\\chef',
                  'chef/notes.md', 'open .chef')
        for prompt in fires:
            with self.subTest(fires=prompt):
                self.assertEqual(prompt_recall.named_skills(prompt, self.vault, set()), ['chef'])
        for prompt in silent:
            with self.subTest(silent=prompt):
                self.assertEqual(prompt_recall.named_skills(prompt, self.vault, set()), [])

    def test_a_hyphenated_name_is_one_word(self):
        self.assertEqual(prompt_recall.named_skills('run PLAN-WAVE next', self.vault, set()), ['plan-wave'])
        for prompt in ('plan the wave', 'plan wave', 'plan-waves', 'the plan-wave-old notes'):
            with self.subTest(prompt):
                self.assertEqual(prompt_recall.named_skills(prompt, self.vault, set()), [])

    def test_several_skills_share_one_line(self):
        text = self.line('Should chef or otopilot run this?')
        self.assertEqual(len(text.splitlines()), 1)
        self.assertTrue(text.startswith('[Skill] This turn names chef, otopilot. '), text)
        self.assertIn(f'read {self.skill_md("chef")}, {self.skill_md("otopilot")} (and', text)

    def test_everyday_names_never_fire_and_the_config_list_replaces_the_default(self):
        self.assertEqual(self.line('What is the limit on uploads?'), '')
        self.assertTrue(self.line('What is the limit on uploads?', {**self.cfg, 'everyday_skills': []})
                        .startswith('[Skill] This turn names limit. '))
        self.assertEqual(self.line('chef or otopilot?', {**self.cfg, 'everyday_skills': ['CHEF']}),
                         self.line('otopilot?'))
        self.assertTrue(self.line('the limit', {**self.cfg, 'everyday_skills': ['chef']}).startswith(
            '[Skill] This turn names limit. '))

    def test_only_folders_holding_skill_md_count(self):
        self.assertEqual(self.line('is notaskill a skill, or hidden?'), '')
        shutil.rmtree(self.hub)
        self.assertEqual(self.line('How does chef pick a model?'), '')

    def test_the_line_rides_along_whether_or_not_recall_finds_hits(self):
        skill = self.line('chef')
        with_hits = self.hook(f'{OTTER} Ask chef.').splitlines()
        self.assertEqual(with_hits[0], prompt_recall.HEADER)
        self.assertIn('/knowledge/otter.md (', with_hits[1])
        self.assertEqual(with_hits[-1], skill)
        self.assertEqual(self.hook('quantum chromodynamics, ask chef', session='s2', cwd=''),
                         f'{prompt_recall.EMPTY}\n{skill}')
        # A later prompt with no hit says nothing of recall; the skill line still comes.
        self.assertEqual(self.hook('gluon plasma lattice, ask chef', session='s2', cwd=''), skill)

    def test_short_repeated_and_sessionless_prompts_still_get_the_line(self):
        skill = self.line('chef')
        self.assertEqual(self.hook('chef?'), skill)
        self.assertIn(skill, self.hook('Tell me what chef decides here', session='r'))
        self.assertEqual(self.hook('Tell me what chef decides here', session='r'), skill)
        self.assertEqual(prompt_recall.context_for(json.dumps({'prompt': 'what does chef do'})), skill)

    def test_silent_turns_stay_silent(self):
        self.assertEqual(self.hook('[no-record] what does chef do'), '')
        self.assertEqual(self.hook('<task-notification> chef finished'), '')
        self.assertEqual(self.hook('what does chef do', origin={'kind': 'peer'}), '')
        self.assertNotIn('[Skill]', self.hook('Ask about other things, the kitchen chefs too'))

    def test_an_unreadable_hub_costs_only_the_line(self):
        with mock.patch.object(skills_hub, 'hub_skills', side_effect=PermissionError('denied')):
            text = self.hook(f'{OTTER} Ask chef.')
        self.assertIn('/knowledge/otter.md (', text)
        self.assertNotIn('[Skill]', text)
        self.assertIn('skill hint: PermissionError: denied', (self.state / 'errors.log').read_text(encoding='utf-8'))


class SkillIndexTests(unittest.TestCase):
    """recall indexes each skill's SKILL.md in the hub and nothing else under a dot folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.vault, cfg = make_vault(base)
        patches = [
            mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(base, cfg)),
                                         'NEOMYELIN_RECALL': 'bm25'}),
            mock.patch.object(recall, 'STATE_DIR', base / 'state'),
            mock.patch.object(recall, 'OLLAMA_URL', 'http://127.0.0.1:9'),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        hub = skills_hub.hub(self.vault)
        write(hub / 'chef' / 'SKILL.md', SKILL.format(name='chef', title='Chef',
                                                      body='Pick the model and effort before spawning a helper.'))
        write(hub / 'chef' / 'references' / 'models.md', '# Models\n\nThe helper model table.\n')
        write(hub / 'notaskill' / 'README.md', '# Not a skill\n\nThe helper readme.\n')
        write(hub / '.hidden' / 'SKILL.md', SKILL.format(name='hidden', title='Hidden', body='The helper.'))
        write(self.vault / '.brain' / 'notes.md', '# Layer notes\n\nThe helper log.\n')
        write(self.vault / '.claude' / 'skills' / 'x' / 'SKILL.md', SKILL.format(name='x', title='X', body='helper'))
        write(self.vault / 'knowledge' / 'tea.md', '# Tea\n\nGreen tea steeps for two minutes.\n')

    def test_only_skill_md_from_the_hub(self):
        dotted = sorted(key for key in recall.documents(self.vault) if key.startswith('.'))
        self.assertEqual(dotted, ['.brain/skills/chef/SKILL.md'])

    def test_a_skill_is_found_by_what_it_says(self):
        rows = recall.search('spawning a helper with which model and effort')[1]
        self.assertEqual(rows[0]['path'], '.brain/skills/chef/SKILL.md')
        self.assertEqual(rows[0]['preview'], 'Pick the model and effort before spawning a helper.')
        self.assertEqual([row['path'] for row in rows], ['.brain/skills/chef/SKILL.md'])

    def test_a_new_skill_makes_the_index_stale(self):
        recall.build('bm25')
        write(skills_hub.hub(self.vault) / 'otopilot' / 'SKILL.md',
              SKILL.format(name='otopilot', title='Otopilot', body='Runs a wave.'))
        stored = recall._load_index('bm25')['docs']
        self.assertEqual(recall.staleness(stored, recall.documents(self.vault)), (1, 0))


class EverydaySkillsConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _, self.cfg = make_vault(Path(tmp.name))

    def test_default_and_override(self):
        self.assertIn('limit', config.everyday_skills(self.cfg))
        self.assertEqual(config.everyday_skills({**self.cfg, 'everyday_skills': [' Chef ']}), {'chef'})
        self.assertEqual(config.everyday_skills({**self.cfg, 'everyday_skills': []}), set())
        self.assertEqual(config.validate({**self.cfg, 'everyday_skills': ['limit']})['everyday_skills'], ['limit'])

    def test_a_bad_list_is_named(self):
        for value in ('limit', ['limit', ''], [3], {'limit': True}):
            with self.subTest(value=value), self.assertRaisesRegex(config.ConfigError, 'everyday_skills'):
                config.validate({**self.cfg, 'everyday_skills': value})


if __name__ == '__main__':
    unittest.main()
