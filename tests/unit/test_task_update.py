from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, write

import task_update


class TaskUpdateTests(unittest.TestCase):
    def test_history_then_update_use_live_revision_and_remove_temp_file(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            calls = []

            def call(root, *args):
                calls.append(args)
                if args[0] == 'history':
                    return mock.Mock(returncode=0, stdout='[{"revision": 4}]', stderr='')
                if args[0] == 'task-update':
                    data = json.loads(Path(args[-1]).read_text(encoding='utf-8'))
                    self.assertEqual(data['expected_revision'], 4)
                    self.assertEqual(data['changes'], {'status': 'done'})
                    self.assertTrue(Path(args[-1]).exists())
                return mock.Mock(returncode=0, stdout='', stderr='')

            with mock.patch.object(task_update.config, 'load', return_value={'vault': str(vault)}), \
                    mock.patch.object(task_update, '_call', side_effect=call):
                self.assertEqual(task_update.main(['task-1', 'status=done']), 0)
            self.assertEqual([call[0] for call in calls], ['history', 'task-update'])
            self.assertFalse(list((vault / '.tmp').glob('*.json')))

    def test_unknown_task_stops_before_the_update(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            with mock.patch.object(task_update.config, 'load', return_value={'vault': str(vault)}), \
                    mock.patch.object(task_update, '_call',
                                      return_value=mock.Mock(returncode=1, stdout='', stderr='no task')) as call:
                self.assertEqual(task_update.main(['task-1', '--json', '{"status":"done"}']), 1)
            call.assert_called_once_with(vault, 'history', 'task-1')

    def test_with_the_real_engine_a_hand_edited_task_updates_at_its_file_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            vault = Path(temp)
            (vault / '.brain').mkdir()
            shutil.copyfile(ROOT / 'engine' / 'brain.py', vault / 'brain.py')
            meta = {'id': 'water', 'title': 'Water the garden', 'status': 'active', 'owner': 'Sam',
                    'kind': 'task', 'revision': 3}
            task = write(vault / 'tasks' / 'water.md', '---\n' + json.dumps(meta, indent=2) + '\n---\nBy hand.\n')
            with mock.patch.object(task_update.config, 'load', return_value={'vault': str(vault)}):
                self.assertEqual(task_update.main(['water', 'status=done', 'next_action=rest']), 0)
            text = task.read_text(encoding='utf-8')
            metadata = json.loads(text.split('---')[1])
            self.assertEqual((metadata['status'], metadata['next_action'], metadata['revision']), ('done', 'rest', 4))
            self.assertTrue(text.endswith('---\nBy hand.\n'))


if __name__ == '__main__':
    unittest.main()
