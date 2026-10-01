from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helpers import COMPANION, ROOT, make_vault, receipt, task, write

import config
import memory_context

SCRIPT = ROOT / 'brain' / 'scripts' / 'memory_context.py'


def context_of(cfg: dict, cwd: Path, session_id: str | None = 'abc') -> str:
    hook_input = json.dumps({'session_id': session_id, 'cwd': str(cwd)} if session_id else {'cwd': str(cwd)})
    output = memory_context.session_start_context(hook_input, cfg=cfg, emit=False, warm=False)
    payload = json.loads(output)['hookSpecificOutput']
    assert payload['hookEventName'] == 'SessionStart'
    return payload['additionalContext']


def section(context: str, heading: str) -> str:
    start = context.index(heading) + len(heading) + 1
    end = context.find('\n\n[Memory', start)
    return context[start:] if end == -1 else context[start:end]


def card(vault: Path, name: str, folder: Path, label: str, sub: str = '') -> Path:
    """A project card in 500-Projects, YAML-style frontmatter as a person writes it."""
    return write(vault / config.PROJECTS / sub / f'{name}.md',
                 f'---\ntitle: {name}\ntype: project\npath: {folder.as_posix()}\nlabel: {label}\n---\n'
                 f'# {name}\n\nWhat it is.\n')


class SessionStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vault, self.cfg = make_vault(self.base, language='Türkçe')
        self.project = self.base / 'Garden'
        self.project.mkdir()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(memory_context.SMOKE_ENV, None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity_session_and_companion_files(self):
        context = context_of(self.cfg, self.project)
        self.assertTrue(context.startswith("[Memory: Identity] You are Nova, Alex's assistant. "
                                           'Speak and write vault notes in Türkçe.'))
        session = hashlib.sha256(b'abc').hexdigest()[:24]
        self.assertIn(f'[Memory: Session] Add "session": "{session}" to the receipt JSON.', context)
        self.assertEqual(section(context, '[Memory: Rules]'), '# Rules\n\nAlways cite the source.')
        self.assertEqual(section(context, '[Memory: Core]'), '# Core\n\nThinking partner.')
        self.assertIn('- Likes tea.', section(context, '[Memory: Personality]'))
        self.assertNotIn('title:', context)  # frontmatter stripped
        self.assertNotIn('[Memory: Check]', context)

    def test_inside_vault_prints_rules_and_core_too(self):
        context = context_of(self.cfg, self.vault)
        self.assertEqual(section(context, '[Memory: Rules]'), '# Rules\n\nAlways cite the source.')
        self.assertEqual(section(context, '[Memory: Core]'), '# Core\n\nThinking partner.')
        self.assertIn('[Memory: Personality]', context)

    def test_no_session_line_without_a_session_id(self):
        self.assertNotIn('[Memory: Session]', context_of(self.cfg, self.vault, session_id=None))

    def test_last_receipt_is_the_newest_of_this_project_and_never_hermes(self):
        receipt(self.vault, 'a', '[Garden] Old work', event_id='e1', created_at='2026-01-01T10:00:00+00:00')
        receipt(self.vault, 'b', '[Garden] New work', event_id='e2', created_at='2026-02-01T10:00:00+00:00')
        receipt(self.vault, 'c', 'Newest by slug', event_id='2026-03-01-garden-x',
                created_at='2026-03-01T10:00:00+00:00')
        receipt(self.vault, 'd', '[Garden] From the web', event_id='e4', harness='hermes',
                created_at='2026-04-01T10:00:00+00:00')
        receipt(self.vault, 'e', '[Kitchen] Other project', event_id='e5', created_at='2026-05-01T10:00:00+00:00')
        last = section(context_of(self.cfg, self.project), '[Memory: Last Receipt]')
        self.assertIn('Newest by slug', last)
        self.assertNotIn('From the web', last)
        self.assertNotIn('Other project', last)

    def test_long_receipt_is_clipped_with_its_source(self):
        receipt(self.vault, 'long', '[Garden] start\n' + '\n'.join('line %d' % i for i in range(800)),
                event_id='e1', created_at='2026-01-01T10:00:00+00:00')
        last = section(context_of(self.cfg, self.project), '[Memory: Last Receipt]')
        self.assertLessEqual(len(last.encode('utf-8')), memory_context.CAPS['last_receipt'])
        self.assertTrue(last.endswith('[clipped — full text: receipts/long.md]'))

    def test_reminders_scope_to_the_project_but_keep_due_tasks(self):
        today = dt.date.today().isoformat()
        task(self.vault, 'due', title='Pay rent', status='active', project='Kitchen', due_at=today, next_action='transfer')
        task(self.vault, 'mine', title='Prune roses', status='active', project='Garden', next_action='buy shears')
        task(self.vault, 'other', title='Paint wall', status='active', project='Kitchen')
        task(self.vault, 'closed', title='Old', status='done', project='Garden')
        write(self.vault / 'tasks' / 'broken.md', 'no metadata\n')
        reminders = section(context_of(self.cfg, self.project), '[Memory: Reminders]')
        self.assertIn(f'- [due {today}] Pay rent — transfer (Kitchen)', reminders)
        self.assertIn('- Prune roses — buy shears (Garden)', reminders)
        self.assertIn('- unreadable: tasks/broken.md', reminders)
        self.assertNotIn('Paint wall', reminders)
        self.assertNotIn('Old', reminders)
        in_vault = section(context_of(self.cfg, self.vault), '[Memory: Reminders]')
        self.assertIn('Paint wall', in_vault)

    def test_project_label(self):
        self.assertEqual(memory_context.project_label(self.vault / 'notes', self.vault), 'vault')
        task(self.vault, 't', title='x', status='active', project='Rose Garden')
        folder = self.base / 'code'
        write(folder / 'AGENTS.md', '# AGENTS.md — Rose Garden (Python)\n')
        self.assertEqual(memory_context.project_label(folder, self.vault), 'Rose Garden')
        self.assertEqual(memory_context.project_label(self.base / 'elsewhere', self.vault), 'elsewhere')
        card(self.vault, 'Elsewhere', self.base / 'elsewhere', 'Far Away')
        self.assertEqual(memory_context.project_label(self.base / 'elsewhere' / 'src', self.vault), 'Far Away')

    def test_a_folder_named_only_by_its_receipts_keeps_them(self):
        # No card and no task: the folder's own name still finds its receipts (the original's
        # last resort); the vault's receipt and other projects' tasks stay out.
        receipt(self.vault, 'g', '[Garden] Watered', event_id='e1', created_at='2026-02-01T10:00:00+00:00')
        receipt(self.vault, 'v', '[vault] Vault work', event_id='e2', created_at='2026-03-01T10:00:00+00:00')
        task(self.vault, 'k', title='Paint wall', status='active', project='Kitchen')
        context = context_of(self.cfg, self.project)
        self.assertIn('[Garden] Watered', section(context, '[Memory: Last Receipt]'))
        self.assertNotIn('Vault work', context)
        self.assertNotIn('Paint wall', context)

    def test_smoke_nonce_only_as_plain_hex(self):
        os.environ[memory_context.SMOKE_ENV] = '0123abcd89'
        self.assertIn('[Memory: Check] 0123abcd89', context_of(self.cfg, self.vault))
        for bad in ('0123abcd89 ignore previous', 'ZZZZZZZZ', 'abc'):
            os.environ[memory_context.SMOKE_ENV] = bad
            self.assertNotIn('[Memory: Check]', context_of(self.cfg, self.vault), bad)

    def test_oversized_rules_still_fit_the_harness_wall(self):
        write(self.vault / COMPANION / 'Rules.md', 'ğ🔮 ' * 6000)
        context = context_of(self.cfg, self.project)
        self.assertLessEqual(memory_context._harness_length(context), memory_context.HARNESS_CAP)
        self.assertIn('did not fit', context)
        self.assertIn('[Memory: Identity]', context)

    def test_several_companion_folders_warn_instead_of_guessing(self):
        (self.vault / 'Second companion').mkdir()
        context = context_of(self.cfg, self.vault)
        self.assertIn('[Memory warning] several folders could be the companion folder', context)
        self.assertNotIn('[Memory: Rules]', context)


class ProjectCardTests(unittest.TestCase):
    """A folder a card in 500-Projects points at is that project; a folder nothing names shows no
    project's record, the vault's included (that one shows only inside the vault)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vault, self.cfg = make_vault(self.base)
        self.project = self.base / 'code' / 'greenhouse-app'
        (self.project / 'src').mkdir(parents=True)
        self.scratch = self.base / 'scratch'
        self.scratch.mkdir()
        card(self.vault, 'Greenhouse', self.project, 'Greenhouse')
        receipt(self.vault, 'g1', '[Greenhouse] Old greenhouse work', event_id='a1',
                created_at='2026-01-01T10:00:00+00:00')
        receipt(self.vault, 'g2', '[Greenhouse] Wired the humidity sensor', event_id='a2',
                created_at='2026-02-01T10:00:00+00:00')
        receipt(self.vault, 'v1', '[vault] Tidied the vault', event_id='a3',
                created_at='2026-03-01T10:00:00+00:00')
        receipt(self.vault, 'k1', '[Kitchen] Newest of all, another project', event_id='a4',
                created_at='2026-04-01T10:00:00+00:00')
        task(self.vault, 'mine', title='Calibrate sensor', status='active', project='Greenhouse',
             next_action='buy a reference hygrometer')
        task(self.vault, 'other', title='Paint wall', status='active', project='Kitchen')

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_card_folder_and_its_subfolders_show_that_project(self):
        for cwd in (self.project, self.project / 'src'):
            context = context_of(self.cfg, cwd)
            last = section(context, '[Memory: Last Receipt]')
            self.assertIn('[Greenhouse] Wired the humidity sensor', last, cwd)
            self.assertNotIn('Old greenhouse work', last)
            reminders = section(context, '[Memory: Reminders]')
            self.assertIn('- Calibrate sensor — buy a reference hygrometer (Greenhouse)', reminders)
            self.assertNotIn('Paint wall', reminders)
            self.assertNotIn('[hygiene]', reminders)

    def test_a_folder_with_no_card_shows_no_projects_record(self):
        write(self.vault / 'knowledge' / 'index.md', '# Index\n')  # an unindexed note: a hygiene line in the vault
        write(self.vault / 'knowledge' / 'concepts' / 'orphan-note.md', '# Orphan\n')
        today = dt.date.today().isoformat()
        task(self.vault, 'due', title='Pay rent', status='active', project='Kitchen', due_at=today)
        context = context_of(self.cfg, self.scratch)
        self.assertIn('[Memory: Identity]', context)
        self.assertNotIn('[Memory: Last Receipt]', context)
        for other in ('Tidied the vault', 'humidity sensor', 'Newest of all', 'Calibrate sensor',
                      'Paint wall', '[hygiene]'):
            self.assertNotIn(other, context)
        self.assertIn(f'- [due {today}] Pay rent', section(context, '[Memory: Reminders]'))  # due tasks show anywhere
        in_vault = context_of(self.cfg, self.vault)
        self.assertIn('Tidied the vault', section(in_vault, '[Memory: Last Receipt]'))
        for line in ('Calibrate sensor', 'Paint wall', '[hygiene]'):
            self.assertIn(line, section(in_vault, '[Memory: Reminders]'))

    def test_the_session_uses_the_hook_cwd_else_the_process_folder(self):
        before = os.getcwd()
        os.chdir(self.project)
        try:
            from_process = context_of(self.cfg, self.scratch, session_id='x')
            hook_input = json.dumps({'session_id': 'x'})
            output = memory_context.session_start_context(hook_input, cfg=self.cfg, emit=False, warm=False)
        finally:
            os.chdir(before)
        self.assertNotIn('humidity sensor', from_process)  # the hook's cwd wins over the process's
        context = json.loads(output)['hookSpecificOutput']['additionalContext']
        self.assertIn('Wired the humidity sensor', section(context, '[Memory: Last Receipt]'))

    def test_the_deepest_card_wins(self):
        card(self.vault, 'Code', self.base / 'code', 'All Code', sub='Old')
        receipt(self.vault, 'c1', '[All Code] Shared tooling', event_id='a5', created_at='2026-05-01T10:00:00+00:00')
        self.assertEqual(memory_context.project_label(self.project / 'src', self.vault), 'Greenhouse')
        other = self.base / 'code' / 'other-tool'
        self.assertEqual(memory_context.project_label(other, self.vault), 'All Code')
        self.assertIn('Shared tooling', section(context_of(self.cfg, other), '[Memory: Last Receipt]'))

    def test_a_card_needs_an_absolute_path_and_a_label(self):
        write(self.vault / config.PROJECTS / 'Relative.md', '---\npath: code/scratch\nlabel: Scratch\n---\n')
        write(self.vault / config.PROJECTS / 'NoLabel.md', f'---\npath: {self.scratch.as_posix()}\n---\n')
        write(self.vault / config.PROJECTS / 'Quoted.md',
              f'---\npath: "{self.base.as_posix()}/quoted dir"\nlabel: \'Quoted One\'\n---\n')
        write(self.vault / config.PROJECTS / 'Json.md',  # brain.py's frontmatter style, native separators
              '---\n' + json.dumps({'path': str(self.base / 'json dir'), 'label': 'Json One'}) + '\n---\n')
        cards = memory_context._project_cards(self.vault)
        self.assertEqual(sorted(label for _, label in cards), ['Greenhouse', 'Json One', 'Quoted One'])
        self.assertEqual(memory_context.project_label(self.scratch, self.vault), 'scratch')
        self.assertEqual(memory_context.project_label(self.base / 'quoted dir', self.vault), 'Quoted One')
        self.assertEqual(memory_context.project_label(self.base / 'json dir' / 'x', self.vault), 'Json One')

    def test_the_folder_note_explains_the_card(self):
        note = (ROOT / 'templates' / 'folders' / '500-Projects.md').read_text(encoding='utf-8')
        self.assertIn('path:', note)
        self.assertIn('label:', note)
        self.assertIn('projects_root', note)
        self.assertEqual(memory_context._simple_frontmatter(note), {})  # the folder note is no card


class ReminderLineTests(unittest.TestCase):
    """Knowledge debt, hygiene and the per-line failure guard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vault, self.cfg = make_vault(self.base)
        self.project = self.base / 'projects' / 'greenhouse-app'
        self.project.mkdir(parents=True)
        card(self.vault, 'Greenhouse', self.project, 'Greenhouse')
        task(self.vault, 'mine', title='Calibrate sensor', status='active', project='Greenhouse')

    def tearDown(self):
        self.tmp.cleanup()

    def reminders(self, cwd: Path | None = None, cfg: dict | None = None) -> list[str]:
        context = context_of(cfg or self.cfg, cwd or self.vault)
        return section(context, '[Memory: Reminders]').splitlines() if '[Memory: Reminders]' in context else []

    def audit(self, payload: object) -> None:
        write(self.vault / '.brain' / '.state' / 'knowledge-audit.json', json.dumps(payload))

    def test_knowledge_debt_comes_first_in_every_session(self):
        self.audit({'status': 'updated', 'gaps': [{'id': 'r:1'}, {'id': 'r:2'}],
                    'conflicts': [{'id': 'r:3'}], 'pending_items': []})
        line = ('- [knowledge debt] 2 lesson(s) to verify; 1 possible conflict(s) to check — '
                '.brain/.state/knowledge-audit.json; checked: knowledge_audit.py close <id>')
        self.assertEqual(self.reminders()[0], line)
        self.assertEqual(self.reminders(self.project)[0], line)

    def test_a_failed_audit_says_how_many_lessons_wait(self):
        self.audit({'status': 'error', 'error': 'model: timeout', 'gaps': [{'id': 'old'}],
                    'pending_items': [{'id': 'r:1'}, {'id': 'r:2'}, {'id': 'r:3'}]})
        self.assertEqual(self.reminders()[0], '- [knowledge debt] The nightly knowledge audit failed; '
                                              '3 lesson(s) wait — .brain/.state/knowledge-audit.json')

    def test_no_debt_no_line(self):
        cases = {'clear': {'status': 'clear', 'gaps': [], 'conflicts': []},
                 'not an object': ['gaps'], 'broken': None}
        for name, payload in cases.items():
            with self.subTest(name):
                if payload is None:
                    write(self.vault / '.brain' / '.state' / 'knowledge-audit.json', '{not json')
                else:
                    self.audit(payload)
                self.assertFalse([line for line in self.reminders() if 'knowledge debt' in line])

    def test_hygiene_names_card_less_project_folders_and_unindexed_notes_in_the_vault_only(self):
        projects = self.base / 'projects'
        (projects / 'loose-ends').mkdir()
        (projects / '.cache').mkdir()
        write(projects / 'notes.txt', 'a file, not a project\n')
        write(self.vault / 'knowledge' / 'index.md', '| [[known-note|Known]] | x |\n')
        write(self.vault / 'knowledge' / 'concepts' / 'known-note.md', '# Known\n')
        write(self.vault / 'knowledge' / 'concepts' / 'orphan-note.md', '# Orphan\n')
        cfg = {**self.cfg, 'projects_root': str(projects)}
        for number in range(6):
            task(self.vault, f'extra{number}', title=f'Extra {number}', status='active', project='Kitchen')
        lines = self.reminders(cfg=cfg)
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[-2:], [
            f'- [hygiene] project folder(s) with no card in {config.PROJECTS}: loose-ends',
            '- [hygiene] knowledge note(s) with no row in knowledge/index.md: 1 (e.g. orphan-note)'])
        self.assertFalse([line for line in self.reminders(self.project, cfg) if '[hygiene]' in line])
        # Without projects_root only the knowledge half runs.
        self.assertEqual([line for line in self.reminders() if '[hygiene]' in line],
                         ['- [hygiene] knowledge note(s) with no row in knowledge/index.md: 1 (e.g. orphan-note)'])

    def test_a_vault_inside_projects_root_is_not_a_card_less_project(self):
        cfg = {**self.cfg, 'projects_root': str(self.base)}
        lines = [line for line in self.reminders(cfg=cfg) if '[hygiene]' in line]
        self.assertEqual(lines, [f'- [hygiene] project folder(s) with no card in {config.PROJECTS}: projects'])

    def test_an_unreadable_projects_root_is_said(self):
        missing = self.base / 'no-such-folder'
        cfg = {**self.cfg, 'projects_root': str(missing)}
        self.assertIn(f'- [hygiene] projects_root unreadable: {missing.resolve().as_posix()} (.brain/config.json)',
                      self.reminders(cfg=cfg))

    def test_projects_root_must_be_absolute(self):
        with self.assertRaises(config.ConfigError):
            config.validate({**self.cfg, 'projects_root': 'relative/projects'})
        self.assertIsNone(config.projects_root(self.cfg))
        self.assertEqual(config.projects_root({**self.cfg, 'projects_root': str(self.base)}), self.base.resolve())

    def test_a_broken_line_costs_only_itself(self):
        for name in ('_knowledge_debt_line', '_hygiene_lines', '_health_lines', '_project_cards'):
            with self.subTest(name), mock.patch.object(memory_context, name, side_effect=RuntimeError('boom')):
                context = context_of(self.cfg, self.vault)
                self.assertIn('[Memory: Rules]', context)
                self.assertIn('Calibrate sensor', section(context, '[Memory: Reminders]'))
                self.assertIn('[Memory warning] a memory source failed: RuntimeError: boom', context)

    def test_the_warning_names_the_failed_source(self):
        self.audit({'status': 'clear'})
        warnings: list[str] = []
        with mock.patch.object(memory_context.json, 'loads', side_effect=RecursionError('deep')):
            self.assertEqual(memory_context._quiet('', warnings, memory_context._knowledge_debt_line, self.vault), '')
        self.assertEqual(warnings, ['[Memory warning] _knowledge_debt_line failed: RecursionError: deep'])


class MainTests(unittest.TestCase):
    def setUp(self):
        # The hook starts a background recall warm-up that may still hold a file at cleanup.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, script: Path = SCRIPT, **env) -> subprocess.CompletedProcess:
        full = {key: value for key, value in os.environ.items() if not key.startswith('NEOMYELIN_')}
        full.update(PYTHONDONTWRITEBYTECODE='1', **env)
        return subprocess.run([sys.executable, str(script), '--session-start'], input='{}',
                              capture_output=True, text=True, encoding='utf-8', env=full, timeout=60)

    def test_a_process_started_by_the_layer_loads_no_memory(self):
        result = self.run_main(NEOMYELIN_INVOKED_BY='test', NEOMYELIN_CONFIG=str(self.base / 'none.json'))
        self.assertEqual((result.returncode, result.stdout), (0, ''))

    def test_a_missing_config_is_said_in_the_session(self):
        result = self.run_main(NEOMYELIN_CONFIG=str(self.base / 'none.json'))
        self.assertEqual(result.returncode, 0)
        context = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('[Memory warning]', context)
        self.assertIn('config not found', context)

    def test_end_to_end_from_the_installed_layout(self):
        vault, cfg = make_vault(self.base)
        scripts = vault / '.brain' / 'scripts'
        for name in ('config.py', 'memory_context.py', 'recall.py'):
            write(scripts / name, (SCRIPT.parent / name).read_text(encoding='utf-8'))
        write(vault / '.brain' / 'config.json', json.dumps(cfg))
        result = self.run_main(scripts / 'memory_context.py', NEOMYELIN_RECALL='bm25')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[Memory: Rules]', json.loads(result.stdout)['hookSpecificOutput']['additionalContext'])


if __name__ == '__main__':
    unittest.main()
