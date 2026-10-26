"""`[Memory: Projects]`: every session start lists the user's open project cards (Task-28).

Why: a session in a folder that was no project proposed building, from scratch, a project the user
had already released; nothing at session start named the user's projects. Each test builds a
temporary vault holding the folder notes install writes (`index.md`); nothing reads a real vault.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest import mock

from helpers import COMPANION, ROOT, make_vault, receipt, task, write

import config
import memory_context

HEADING = '[Memory: Projects]'
FOLDER_NOTE = (ROOT / 'templates' / 'folders' / '500-Projects.md').read_text(encoding='utf-8')
GARDEN = '- Garden (active): Raised-bed planner and watering log for the back garden'
THESIS = '- Thesis (waiting): Master thesis on soil moisture sensing, waiting for the supervisor.'
LEGACY = '- Recipes: Recipes collected before cards had a status.'


def context_of(cfg: dict, cwd: Path) -> str:
    hook_input = json.dumps({'session_id': 'projects', 'cwd': str(cwd)})
    output = memory_context.session_start_context(hook_input, cfg=cfg, emit=False, warm=False)
    return json.loads(output)['hookSpecificOutput']['additionalContext']


def projects_section(context: str) -> list[str] | None:
    """The section's lines, its first (heading) line included; None when the section is absent."""
    found = [block for block in context.split('\n\n') if block.startswith(HEADING)]
    if len(found) > 1:
        raise AssertionError(f'{len(found)} project sections')
    return found[0].splitlines() if found else None


def section(context: str, heading: str) -> str:
    start = context.index(heading) + len(heading) + 1
    end = context.find('\n\n[Memory', start)
    return context[start:] if end == -1 else context[start:end]


def card(vault: Path, name: str, body: str = '', sub: str = '', **fields: str) -> Path:
    """A card as a person writes it: YAML-style frontmatter (none without fields), a heading, text."""
    front = ''.join(f'{key}: {value}\n' for key, value in fields.items())
    return write(vault / config.PROJECTS / sub / f'{name}.md',
                 (f'---\n{front}---\n' if fields else '') + f'# {name}\n\n{body}\n')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.vault, self.cfg = make_vault(self.base)
        # install's folder notes: one per folder, the template text, no frontmatter
        write(self.vault / config.PROJECTS / 'index.md', FOLDER_NOTE)
        write(self.vault / config.PROJECTS / 'Side Projects' / 'index.md', '# Side Projects\n\nSmall things.\n')
        write(self.vault / config.FOLDERS[0] / 'index.md', '# Goals\n')
        self.garden = self.base / 'code' / 'garden'
        self.garden.mkdir(parents=True)
        self.scratch = self.base / 'scratch'  # a folder that is no project
        self.scratch.mkdir()

    def example_cards(self) -> None:
        """The task's example: Garden (active), Thesis (waiting, no code folder), Old Blog (done)."""
        card(self.vault, 'Garden', 'A longer text that is not the summary.', path=self.garden.as_posix(),
             label='Garden', status='active', summary='Raised-bed planner and watering log for the back garden')
        card(self.vault, 'Thesis', 'Master thesis on soil moisture sensing, waiting for the supervisor.',
             label='Thesis', status='waiting')
        card(self.vault, 'Old Blog', 'A blog about tomatoes.', label='Old Blog', status='done',
             path=(self.base / 'code' / 'blog').as_posix())


class ListTests(Base):
    def test_the_example_vault_in_a_folder_that_is_no_project(self):
        self.example_cards()
        card(self.vault, 'Recipes', 'Recipes collected before cards had a status.', sub='Side Projects')
        lines = projects_section(context_of(self.cfg, self.scratch))
        self.assertEqual(lines[0], "[Memory: Projects] Alex's open projects (cards in 500-Projects 🏰/). "
                                   'Check this list before proposing a new product, tool or project; '
                                   'if one already covers it, ask whether the work belongs there.')
        self.assertEqual(lines[1:], [GARDEN, THESIS, LEGACY])

    def test_the_list_shows_in_every_folder(self):
        self.example_cards()
        for cwd in (self.scratch, self.garden, self.garden / 'src', self.vault, self.vault / config.PROJECTS):
            lines = projects_section(context_of(self.cfg, cwd))
            self.assertEqual(lines[1:], [GARDEN, THESIS], cwd)

    def test_only_active_waiting_and_no_status_are_listed(self):
        self.example_cards()
        card(self.vault, 'Shelved', 'Gone.', label='Shelved', status='archived')
        card(self.vault, 'Someday', 'Maybe.', label='Someday', status='someday')
        card(self.vault, 'Paused', 'Quoted and capitalised.', label='Paused', status='"Waiting"')
        card(self.vault, 'Blank', 'Status line with no value.', label='Blank', status='')
        write(self.vault / config.PROJECTS / 'Json.md',  # brain.py's frontmatter style, no status
              '---\n' + json.dumps({'label': 'Json Card'}) + '\n---\n# Json\n\nWritten by a script.\n')
        lines = projects_section(context_of(self.cfg, self.scratch))[1:]
        self.assertEqual(lines, [GARDEN, '- Paused (waiting): Quoted and capitalised.', THESIS,
                                 '- Blank: Status line with no value.', '- Json Card: Written by a script.'])
        text = '\n'.join(lines)
        for left_out in ('Old Blog', 'Shelved', 'Someday', 'index', 'Side Projects', 'Small things'):
            self.assertNotIn(left_out, text)

    def test_folder_notes_are_never_cards(self):
        # Not even one carrying a card's frontmatter: index.md is the folder's own note.
        write(self.vault / config.PROJECTS / 'Side Projects' / 'index.md',
              '---\nlabel: Side\nstatus: active\n---\n# Side Projects\n')
        write(self.vault / config.PROJECTS / 'INDEX.md', '# Upper case\n')
        self.assertIsNone(projects_section(context_of(self.cfg, self.scratch)))
        self.assertEqual(memory_context._open_projects(self.vault), [])

    def test_a_card_needs_no_path_and_matching_still_needs_path_and_label(self):
        self.example_cards()
        card(self.vault, 'Nameless', 'Folder only.', path=(self.base / 'nameless').as_posix(), status='active')
        lines = projects_section(context_of(self.cfg, self.scratch))[1:]
        self.assertIn(THESIS, lines)  # no path:
        self.assertIn('- Nameless (active): Folder only.', lines)  # no label: the file name
        # Which project a session is in: only cards with both, as before.
        self.assertEqual([label for _, label in memory_context._project_cards(self.vault)], ['Garden', 'Old Blog'])
        self.assertEqual(memory_context.project_label(self.garden / 'src', self.vault), 'Garden')
        self.assertEqual(memory_context.project_label(self.base / 'thesis-work', self.vault), 'thesis-work')
        self.assertEqual(memory_context.project_label(self.base / 'nameless', self.vault), 'nameless')

    def test_the_record_and_tasks_stay_the_sessions_own(self):
        self.example_cards()
        receipt(self.vault, 'g', '[Garden] Planted the beans', event_id='e1', created_at='2026-02-01T10:00:00+00:00')
        task(self.vault, 't', title='Dig the bed', status='active', project='Garden', next_action='buy a spade')
        context = context_of(self.cfg, self.scratch)
        self.assertIn(GARDEN, context)
        for other in ('Planted the beans', 'Dig the bed', '[Memory: Last Receipt]'):
            self.assertNotIn(other, context)
        in_garden = context_of(self.cfg, self.garden)
        self.assertIn('Planted the beans', section(in_garden, '[Memory: Last Receipt]'))
        self.assertIn('Dig the bed', section(in_garden, '[Memory: Reminders]'))

    def test_summary_field_else_first_text_under_the_heading_else_nothing(self):
        card(self.vault, 'Field', 'Body text loses to the field.', label='Field', status='active',
             summary='"Quoted summary"')
        card(self.vault, 'Prose', '## Status\n\n```\nnot this code\n```\n| a | table |\n\n'
                                  '<!-- a comment -->\n- **What:** bees   in the orchard.\nSecond line.',
             label='Prose', status='active')
        card(self.vault, 'Bare', '', label='Bare', status='active')
        write(self.vault / config.PROJECTS / 'Heading.md', '---\nstatus: waiting\n---\n')  # no body at all
        card(self.vault, 'Long', 'word ' * 60, label='Long', status='waiting')
        lines = projects_section(context_of(self.cfg, self.scratch))[1:]
        self.assertEqual(lines[:3], ['- Bare (active)', '- Field (active): Quoted summary',
                                     '- Prose (active): **What:** bees in the orchard.'])
        self.assertEqual(lines[3], '- Heading (waiting)')
        self.assertTrue(lines[4].startswith('- Long (waiting): word word'), lines[4])
        self.assertLessEqual(len(lines[4]), memory_context.PROJECT_LINE)
        self.assertGreater(len(lines[4]), memory_context.PROJECT_LINE - 6)
        self.assertTrue(lines[4].endswith('…'))

    def test_a_comment_over_several_lines_is_no_summary(self):
        card(self.vault, 'Comment', '<!--\nTODO: replace this old outline\n-->\n\nRaised-bed planner.',
             label='Comment', status='active')
        self.assertEqual(projects_section(context_of(self.cfg, self.scratch))[1:],
                         ['- Comment (active): Raised-bed planner.'])

    def test_order_active_then_waiting_then_no_status_each_by_name(self):
        card(self.vault, 'zed', 'z.')  # no frontmatter: no status
        card(self.vault, 'Ant', 'a.')
        card(self.vault, 'beta', 'b.', status='active')
        card(self.vault, 'Alpha', 'a.', status='active')
        card(self.vault, 'Aardvark', 'w.', status='waiting')
        card(self.vault, 'Mole', 'w.', status='waiting', sub='Side Projects')
        names = [line.split(' ', 2)[1].rstrip(':') for line in projects_section(context_of(self.cfg, self.scratch))[1:]]
        self.assertEqual(names, ['Alpha', 'beta', 'Aardvark', 'Mole', 'Ant', 'zed'])

    def test_absent_when_no_card_qualifies(self):
        self.assertIsNone(projects_section(context_of(self.cfg, self.scratch)))  # folder notes only
        card(self.vault, 'Old Blog', 'Done.', status='done')
        card(self.vault, 'Shelved', 'Gone.', status='archived')
        context = context_of(self.cfg, self.scratch)
        self.assertNotIn(HEADING, context)
        self.assertNotIn('\n\n\n', context)
        shutil.rmtree(self.vault / config.PROJECTS)
        self.assertNotIn(HEADING, context_of(self.cfg, self.scratch))  # no folder at all

    def test_a_broken_card_is_skipped_alone(self):
        self.example_cards()
        write(self.vault / config.PROJECTS / 'Unclosed.md', '---\nlabel: Unclosed\nstatus: active\n# Unclosed\n\nText.\n')
        write(self.vault / config.PROJECTS / 'BadJson.md', '---\n{"label": "Bad Json", "status": \n---\n# Bad\n')
        write(self.vault / config.PROJECTS / 'ListJson.md', '---\n["label", "List Json"]\n---\n# List\n')
        path = self.vault / config.PROJECTS / 'Binary.md'
        path.write_bytes(b'---\nlabel: Binary\n---\n\xff\xfe\x00 not utf-8 \x81\n')
        (self.vault / config.PROJECTS / 'Folder.md').mkdir()  # matches *.md, cannot be read as a file
        context = context_of(self.cfg, self.scratch)
        self.assertEqual(projects_section(context)[1:], [GARDEN, THESIS])
        self.assertNotIn('[Memory warning]', context)
        self.assertIn('[Memory: Rules]', context)

    def test_a_failing_source_costs_only_its_section(self):
        self.example_cards()
        with mock.patch.object(memory_context, '_open_projects', side_effect=RuntimeError('disk gone')):
            context = context_of(self.cfg, self.scratch)
        self.assertIn('[Memory warning]', context)
        self.assertIn('disk gone', context)
        self.assertNotIn(HEADING, context)
        self.assertIn('Always cite the source.', section(context, '[Memory: Rules]'))


class BudgetTests(Base):
    def use_shipped_companion_files(self) -> None:
        for name in ('Rules.md', 'Core.md', 'Personality.md'):
            write(self.vault / COMPANION / name, (ROOT / 'templates' / name).read_text(encoding='utf-8'))

    def alarm(self) -> str:
        """A nightly error in health.json: an alarm line at the head of the reminders."""
        write(self.vault / '.brain' / '.state' / 'health.json',
              json.dumps({'ts': time.time(), 'component': 'nightly', 'error': 'model call timed out'}))
        return 'Health: nightly error: model call timed out'

    def ten_cards(self, summary: str) -> list[str]:
        names = []
        for number in range(10):
            name = f'Project {number:02d}'
            card(self.vault, name, status='active' if number < 6 else 'waiting', summary=f'{summary} {number}',
                 sub='Side Projects' if number % 3 == 0 else '')
            names.append(name)
        return names

    def test_ten_cards_fit_whole_beside_the_shipped_files_a_full_receipt_and_reminders(self):
        self.use_shipped_companion_files()
        alarm = self.alarm()
        # The session's folder `garden` has no card: its receipts and tasks are found by its name.
        receipt(self.vault, 'long', '[Garden] start\n' + '\n'.join(f'line {i} of a long receipt' for i in range(200)),
                event_id='e1', created_at='2026-01-01T10:00:00+00:00')
        for number in range(6):
            task(self.vault, f't{number}', title=f'Task {number} ' + 'x' * 100, status='active',
                 project='Garden', next_action='next ' * 20)
        names = self.ten_cards('A long one-line summary that says what the project is, who it is for '
                               'and how far it has come, clipped')
        context = context_of(self.cfg, self.garden)
        lines = projects_section(context)
        self.assertEqual(len(lines), 11, lines)
        for name, line in zip(names, lines[1:]):
            self.assertTrue(line.startswith(f'- {name} ('), line)
            self.assertLessEqual(len(line), memory_context.PROJECT_LINE)
        self.assertLessEqual(len('\n'.join(lines[1:]).encode('utf-8')), memory_context.CAPS['projects'])
        self.assertLessEqual(len(context.encode('utf-8')), memory_context.TOTAL_CAP)
        self.assertLessEqual(memory_context._harness_length(context), memory_context.HARNESS_CAP)
        self.assertIn(alarm, section(context, '[Memory: Reminders]'))
        self.assertIn('line 10 of a long receipt', section(context, '[Memory: Last Receipt]'))
        self.assertEqual(section(context, '[Memory: Rules]'),
                         memory_context._frontmatter_body((ROOT / 'templates' / 'Rules.md').read_text(encoding='utf-8')))

    def test_ten_cards_fit_whole_in_a_language_of_wide_letters(self):
        # The cap counts bytes: a line clipped by characters overflowed it in Turkish or Japanese.
        for summary in ('Arka bahçenin sulama günlüğü ve yükseltilmiş yatak planlayıcısı, öğrenci için ' * 2,
                        '裏庭の水やり記録と花壇の計画を一つにまとめた小さな道具です' * 3):
            with self.subTest(summary=summary[:10]):
                shutil.rmtree(self.vault / config.PROJECTS / 'Side Projects', ignore_errors=True)
                for old in (self.vault / config.PROJECTS).glob('Project *.md'):
                    old.unlink()
                names = self.ten_cards(summary)
                lines = projects_section(context_of(self.cfg, self.scratch))
                self.assertEqual([line.split(' (')[0] for line in lines[1:]], [f'- {name}' for name in names])
                for line in lines[1:]:
                    self.assertLessEqual(len(line.encode('utf-8')), memory_context.PROJECT_LINE, line)
                    self.assertTrue(line.endswith('…'), line)

    def test_cards_past_the_cap_are_still_named(self):
        for number in range(30):
            card(self.vault, f'Card {number:02d}', status='active', summary='s' * 100)
        value = '\n'.join(projects_section(context_of(self.cfg, self.scratch))[1:])
        self.assertLessEqual(len(value.encode('utf-8')), memory_context.CAPS['projects'])
        last = value.splitlines()[-1]
        self.assertTrue(last.startswith('- Also open: '), last)
        for number in range(30):
            self.assertIn(f'Card {number:02d}', value)  # every card named, fewer full lines
        self.assertIn('- Card 00 (active): sss', value)
        for number in range(300):
            card(self.vault, f'Many {number:03d}', status='waiting')
        value = '\n'.join(projects_section(context_of(self.cfg, self.scratch))[1:])
        self.assertLessEqual(len(value.encode('utf-8')), memory_context.CAPS['projects'])
        self.assertRegex(value.splitlines()[-1], r', \+\d+ more in 500-Projects 🏰/$')

    def test_over_either_cap_the_list_gives_way_first(self):
        """Rules grows step by step past the cap. The list goes full -> names only -> gone, never
        back, and while any of it shows Rules, Core, Personality and the alarm line are whole."""
        # 'あ' is 3 bytes but one UTF-16 unit: only TOTAL_CAP bites. ASCII is one of each: the
        # 9800-unit HARNESS_CAP bites before the 11500-byte TOTAL_CAP.
        for name, char, step, cap, measure, other in (
                ('TOTAL_CAP', 'あ', 10, memory_context.TOTAL_CAP, lambda text: len(text.encode('utf-8')),
                 lambda text: memory_context._harness_length(text) < memory_context.HARNESS_CAP - 2000),
                ('HARNESS_CAP', 'a', 30, memory_context.HARNESS_CAP, memory_context._harness_length,
                 lambda text: len(text.encode('utf-8')) < memory_context.TOTAL_CAP - 1000)):
            with self.subTest(cap=name):
                self.sweep(char, step, cap, measure, other)

    def sweep(self, char: str, step: int, cap: int, measure, other_cap_idle) -> None:
        # Shipped Core and Personality and three task lines: sections long enough that cutting them
        # saves bytes (the small fixture files are shorter than their own clip notes).
        self.use_shipped_companion_files()
        rules_path = self.vault / COMPANION / 'Rules.md'
        write(rules_path, char)
        self.example_cards()
        card(self.vault, 'Recipes', 'Recipes collected before cards had a status.', sub='Side Projects')
        alarm = self.alarm()
        for number in range(3):
            task(self.vault, f'scratch-{number}', title=f'Tidy shelf {number}', status='active',
                 project='scratch', next_action='sort the jars by size ' * 4)
        first = context_of(self.cfg, self.scratch)
        core = section(first, '[Memory: Core]')
        personality = section(first, '[Memory: Personality]')
        self.assertIn('Tidy shelf 2', first)
        base = measure(first)
        size = max(1, (cap - base) // measure(char) - 40)  # 40 characters before the cap bites
        order = ['full', 'names', 'gone']
        seen: list[str] = []
        past_the_list = False
        for _ in range(400):
            rules = char * size
            write(rules_path, rules)
            context = context_of(self.cfg, self.scratch)
            self.assertLessEqual(len(context.encode('utf-8')), memory_context.TOTAL_CAP, size)
            self.assertLessEqual(memory_context._harness_length(context), memory_context.HARNESS_CAP, size)
            self.assertTrue(other_cap_idle(context), size)
            if 'did not fit' in context:
                break
            lines = projects_section(context)
            if lines is None:
                state = 'gone'
            elif lines[1:] == [GARDEN, THESIS, LEGACY]:
                state = 'full'
            elif lines[1:] == ['- Open: Garden, Thesis (waiting), Recipes']:
                state = 'names'
            else:
                self.fail(f'{size}: unexpected project section {lines}')
            if seen:
                self.assertGreaterEqual(order.index(state), order.index(seen[-1]), f'{size}: {seen} -> {state}')
            if not seen or seen[-1] != state:
                seen.append(state)
            whole = (section(context, '[Memory: Rules]') == rules
                     and section(context, '[Memory: Core]') == core
                     and section(context, '[Memory: Personality]') == personality
                     and alarm in context)
            if not whole:
                self.assertEqual(state, 'gone', f'{size}: something else was cut while the list showed')
                past_the_list = True
            size += step
        self.assertEqual(seen, order)
        self.assertTrue(past_the_list, 'the sweep never reached the sections after the list')


if __name__ == '__main__':
    unittest.main()
