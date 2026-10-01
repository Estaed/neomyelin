from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import make_vault, receipt, write
import knowledge_audit as audit


NOW = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)


class KnowledgeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault, self.cfg = make_vault(Path(self.temp.name))
        self.cfg['companion_dir'] = 'companion'
        self.evolution = write(self.vault / 'companion' / 'Evolution.md', '# Evolution\n')
        self.item = receipt(self.vault, 'lesson', '# Session\n\n**Learning**\n'
                            '- Check the active folder before running commands.\n',
                            created_at='2026-09-30T12:00:00+00:00', refs=[])

    def response(self, *, gaps=None, conflicts=None, ideas=None):
        return json.dumps({'reviewed_ids': ['lesson:1'], 'gaps': gaps or [],
                           'conflicts': conflicts or [], 'ideas': ideas or []})

    def test_inventory_and_48_hour_window(self):
        self.assertEqual(len(audit.audit(self.cfg)), 1)
        note = write(self.vault / 'knowledge' / 'concepts' / 'folders.md',
                     '# Folders\n\nCheck the active folder before running commands.\n')
        self.assertEqual(audit.audit(self.cfg), [])
        self.assertEqual(len(audit._items(self.vault, NOW, [], [])), 1)
        self.item.write_text(self.item.read_text(encoding='utf-8').replace(
            '2026-09-30T12:00:00', '2026-09-25T12:00:00'), encoding='utf-8')
        self.assertEqual(audit._items(self.vault, NOW, [], []), [])
        note.unlink()

    def test_judge_writes_note_once_and_copies_knowledge(self):
        gap = {'id': 'lesson:1', 'reason': 'This durable lesson is missing from the notes.',
               'target': 'new', 'title': 'Working folder check',
               'note': 'Check the active folder before running commands.'}
        stages = []

        def judge(prompt, stage, **kwargs):
            self.assertTrue((stage / 'learnings.json').exists())
            self.assertTrue((stage / 'knowledge' / 'concepts').exists())
            stages.append(stage)
            return self.response(gaps=[gap])

        with mock.patch.object(audit.stage, 'run_in_stage', side_effect=judge):
            self.assertEqual(audit.run(self.cfg, NOW), 0)
            self.assertEqual(audit.run(self.cfg, NOW), 0)
        notes = list((self.vault / 'knowledge' / 'concepts').glob('*.md'))
        self.assertEqual(len(notes), 1)
        self.assertIn('Source: receipts/lesson.md', notes[0].read_text(encoding='utf-8'))
        self.assertEqual((self.vault / 'knowledge' / 'index.md').read_text(encoding='utf-8').count(
            'Working folder check'), 1)
        self.assertTrue(all(not stage.exists() for stage in stages))
        # A partial prior run may have written the note but missed the index row.
        (self.vault / 'knowledge' / 'index.md').write_text('# Knowledge index\n', encoding='utf-8')
        self.assertIn('already filed', audit._write_gap(self.vault, {**gap, 'receipt': 'receipts/lesson.md',
                                                                     'learning': 'Check the active folder'},
                                                        NOW.date()))
        self.assertIn('Working folder check',
                      (self.vault / 'knowledge' / 'index.md').read_text(encoding='utf-8'))

    def test_new_note_collision_preserves_existing_note(self):
        gap = {'id': 'lesson:1', 'receipt': 'receipts/lesson.md',
               'learning': 'Check the active folder', 'title': 'Working folder check',
               'target': 'new', 'note': 'Check the active folder before commands.'}
        stem = f"{audit._slug(gap['title'])}-{hashlib.sha256(gap['id'].encode()).hexdigest()[:8]}"
        path = write(self.vault / 'knowledge' / 'concepts' / f'{stem}.md', '# User-owned note\n')
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            audit._write_gap(self.vault, gap, NOW.date())
        self.assertEqual(path.read_bytes(), before)

    def test_conflict_pending_close_and_idea(self):
        target = write(self.vault / 'knowledge' / 'concepts' / 'folders.md', '# Folders\nOld rule.\n')
        conflict = {'id': 'lesson:1', 'reason': 'The receipt directly contradicts the old rule.',
                    'target': 'knowledge/concepts/folders.md'}
        with mock.patch.object(audit.stage, 'run_in_stage', return_value=self.response(conflicts=[conflict])):
            self.assertEqual(audit.run(self.cfg, NOW), 0)
        self.assertEqual(target.read_text(encoding='utf-8'), '# Folders\nOld rule.\n')
        state = self.vault / '.brain' / '.state' / 'knowledge-audit.json'
        self.assertEqual(json.loads(state.read_text(encoding='utf-8'))['conflicts'][0]['id'], 'lesson:1')
        self.assertEqual(audit.close(['lesson:1'], self.cfg), 0)
        self.assertEqual(audit._items(self.vault, NOW, [], ['lesson:1']), [])
        self.assertEqual(audit.close(['missing'], self.cfg), 1)
        idea = {'id': 'lesson:1', 'reason': 'The assistant should check state before retrying.',
                'proposal': 'Add a preflight folder check.'}
        with mock.patch.object(audit.stage, 'run_in_stage', return_value=self.response(ideas=[idea])):
            self.assertEqual(audit.run(self.cfg, NOW), 0)  # closed item is skipped
        self.assertEqual(self.evolution.read_text(encoding='utf-8'), '# Evolution\n')

    def test_idea_and_missing_model_error_are_visible_and_pending(self):
        idea = {'id': 'lesson:1', 'reason': 'The assistant should check state before retrying.',
                'proposal': 'Add a preflight folder check.'}
        with mock.patch.object(audit.stage, 'run_in_stage', return_value=self.response(ideas=[idea])):
            self.assertEqual(audit.run(self.cfg, NOW), 0)
            self.assertEqual(audit.run(self.cfg, NOW), 0)
        self.assertEqual(self.evolution.read_text(encoding='utf-8').count('### Idea'), 1)
        errors = io.StringIO()
        with mock.patch.object(audit.stage, 'run_in_stage', side_effect=RuntimeError('model CLI unavailable')), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(audit.run(self.cfg, NOW), 2)
        self.assertIn('model CLI unavailable', errors.getvalue())
        state = self.vault / '.brain' / '.state' / 'knowledge-audit.json'
        self.assertEqual(json.loads(state.read_text(encoding='utf-8'))['pending_items'][0]['id'], 'lesson:1')

    def test_rejects_unreviewed_and_unsafe_targets(self):
        items = audit._items(self.vault, NOW, [], [])
        with self.assertRaises(ValueError):
            audit._checked_output(json.dumps({'reviewed_ids': [], 'gaps': [], 'conflicts': [], 'ideas': []}),
                                  items, self.vault)
        bad = {'id': 'lesson:1', 'reason': 'Missing durable lesson in the notes.',
               'target': 'knowledge/concepts/../outside.md', 'title': 'Unsafe', 'note': 'Content'}
        with self.assertRaises(ValueError):
            audit._checked_output(self.response(gaps=[bad]), items, self.vault)


if __name__ == '__main__':
    unittest.main()
