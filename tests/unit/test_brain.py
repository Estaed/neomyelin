"""engine/brain.py: receipts, tasks, history, the daily view and the engine's self-check."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, write

SPEC = importlib.util.spec_from_file_location('neomyelin_brain', ROOT / 'engine' / 'brain.py')
brain = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = brain
SPEC.loader.exec_module(brain)

SUMMARY = '[Garden] Watered the beds (Model: test)\n**Done**\n- Watered the tomato beds.'


def frontmatter(path: Path) -> tuple[dict, str]:
    return brain.split(path.read_text(encoding='utf-8'))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        (self.vault / '.brain').mkdir()
        write(self.vault / 'AGENTS.md', '# Vault\n')

    def files(self) -> dict[str, bytes]:
        return {path.relative_to(self.vault).as_posix(): path.read_bytes()
                for path in sorted(self.vault.rglob('*')) if path.is_file() and '.state' not in path.parts}

    def test_receipt_file_daily_view_and_idempotency(self):
        payload = {'event_id': '2026-10-01-garden-water', 'summary': SUMMARY, 'refs': ['AGENTS.md'],
                   'session': 'abc123'}
        result = brain.receipt(self.vault, payload, 'claude')
        name = hashlib.sha256(b'2026-10-01-garden-water').hexdigest()
        self.assertEqual((result['status'], result['source'], result['written'], result['secrets_redacted']),
                         ('succeeded', f'receipts/{name}.md', True, 0))
        raw = (self.vault / result['source']).read_text(encoding='utf-8')
        metadata, body = brain.split(raw)
        self.assertEqual(list(metadata), ['kind', 'event_id', 'harness', 'refs', 'visibility', 'created_at', 'session'])
        self.assertEqual((metadata['kind'], metadata['harness'], metadata['refs'], metadata['visibility']),
                         ('receipt', 'claude', ['AGENTS.md'], 'internal'))
        self.assertTrue(raw.startswith('---\n{\n  "kind": "receipt",\n'))
        self.assertEqual(body, SUMMARY + '\n')

        local = brain._instant(metadata['created_at']).astimezone()
        day = self.vault / 'daily' / f'{local.date().isoformat()}.md'
        self.assertEqual(result['daily'], [f'daily/{day.name}'])
        view = day.read_text(encoding='utf-8')
        self.assertTrue(view.startswith('---\n{\n  "generated": true,\n  "kind": "receipt-index"\n}\n---\n'
                                        f'# Daily: {local.date().isoformat()}\n\n'))
        self.assertIn(f'### {local:%H:%M} — [Garden] Watered the beds (Model: test) · Local PC\n\n'
                      '**Done**\n- Watered the tomato beds.\n\n'
                      f'[[receipts/{name}.md|receipt]]\n', view)

        before = self.files()
        again = brain.receipt(self.vault, payload, 'claude')
        self.assertEqual((again['status'], again['written'], again['daily']), ('succeeded', False, []))
        self.assertEqual(self.files(), before)
        with self.assertRaises(brain.ReceiptConflict):
            brain.receipt(self.vault, {**payload, 'summary': SUMMARY + ' changed'}, 'claude')

    def test_refs_must_be_existing_vault_files(self):
        base = {'event_id': 'e', 'summary': 'x'}
        for refs in (None, [], [''], ['missing.md'], ['../outside.md'], ['/abs.md'], ['.brain']):
            with self.subTest(refs=refs), self.assertRaises(brain.Refused):
                brain.receipt(self.vault, {**base, 'refs': refs})
        self.assertFalse((self.vault / 'receipts').exists())
        write(self.vault / '500-Projects' / 'Garden.md', '# Garden\n')
        result = brain.receipt(self.vault, {**base, 'refs': ['500-Projects\\Garden.md']})
        self.assertEqual(frontmatter(self.vault / result['source'])[0]['refs'], ['500-Projects/Garden.md'])

    def test_secrets_are_masked_and_counted(self):
        secrets = ['sk-' + 'a1B2' * 10, 'ghp_' + 'x' * 36, 'AKIA' + 'A' * 16, 'xoxb-1234-abcdefghijkl',
                   'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N']
        text = ('Keys: ' + ' and '.join(secrets) + '. password: hunter2hunter2, url https://bob:pa55word@localhost/x\n'
                '-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\nToken budget: 120 tokens.')
        masked, count = brain.redact(text)
        for secret in secrets + ['hunter2hunter2', 'pa55word', 'MIIEow']:
            self.assertNotIn(secret, masked)
        self.assertEqual(count, len(secrets) + 3)  # + the password, the URL password, the key block
        self.assertIn('password: [REDACTED]', masked)
        self.assertIn('https://bob:[REDACTED]@localhost', masked)
        self.assertIn('Token budget: 120 tokens.', masked)
        self.assertEqual(brain.redact(masked), (masked, 0))
        result = brain.receipt(self.vault, {'event_id': 's', 'summary': text, 'refs': ['AGENTS.md']})
        self.assertEqual((result['redacted'], result['secrets_redacted']), (count, count))
        self.assertEqual(frontmatter(self.vault / result['source'])[1], masked + '\n')

    def test_task_create_update_conflict_and_history(self):
        payload = {'source': 'tasks/water.md', 'text': 'Water the beds before the heat.',
                   'metadata': {'id': 'water', 'title': 'Water', 'status': 'active', 'owner': 'Sam',
                                'project': 'Garden', 'next_action': 'Fill the can', 'due_at': '2026-10-02'}}
        created = brain.task_create(self.vault, payload)
        self.assertEqual((created['status'], created['revision']), ('succeeded', 1))
        path = self.vault / 'tasks' / 'water.md'
        metadata, body = frontmatter(path)
        self.assertEqual(list(metadata), ['id', 'title', 'status', 'owner', 'project', 'next_action', 'due_at',
                                          'kind', 'revision'])
        self.assertEqual(body, 'Water the beds before the heat.\n')
        with self.assertRaisesRegex(brain.Refused, 'task-update'):
            brain.task_create(self.vault, payload)

        updated = brain.task_update(self.vault, {'id': 'water', 'expected_revision': 1,
                                                 'changes': {'status': 'done'}})
        self.assertEqual(updated['revision'], 2)
        with self.assertRaises(brain.RevisionConflict):
            brain.task_update(self.vault, {'id': 'water', 'expected_revision': 1, 'changes': {'status': 'active'}})
        self.assertEqual(frontmatter(path)[0]['status'], 'done')

        log = brain.history(self.vault, 'tasks/water.md')
        self.assertEqual([(e['sequence'], e['event_type'], e['revision']) for e in log],
                         [(1, 'create', 1), (2, 'update', 2)])
        self.assertEqual(brain.history(self.vault, 'water'), log)

        # A hand edit after the last logged event shows as the file's own entry at the end.
        text = path.read_text(encoding='utf-8').replace('"revision": 2', '"revision": 5')
        path.write_text(text, encoding='utf-8')
        self.assertEqual(brain.history(self.vault, 'water')[-1]['revision'], 5)
        self.assertEqual(brain.history(self.vault, 'water')[-1]['event_type'], 'file')

    def test_task_input_is_checked_before_any_write(self):
        good = {'id': 't1', 'status': 'active', 'owner': 'Sam'}
        bad = [
            {'source': 'notes/t1.md', 'text': 'x', 'metadata': good},
            {'source': 'tasks/t1.md', 'text': '', 'metadata': good},
            {'source': 'tasks/t1.md', 'text': 'x', 'metadata': {**good, 'priority': 'high'}},
            {'source': 'tasks/t1.md', 'text': 'x', 'metadata': {**good, 'status': 'someday'}},
            {'source': 'tasks/t1.md', 'text': 'x', 'metadata': {**good, 'id': '../t1'}},
            {'source': 'tasks/t1.md', 'text': 'x', 'metadata': {**good, 'revision': 2}},
            {'source': 'tasks/t1.md', 'text': 'x', 'metadata': {**good, 'due_at': 'tomorrow'}},
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(brain.Refused):
                brain.task_create(self.vault, payload)
        self.assertFalse((self.vault / 'tasks').exists())

    def test_sync_never_overwrites_a_daily_note_that_is_not_generated(self):
        created = dt.datetime(2026, 1, 5, 12, 0, tzinfo=dt.timezone.utc)
        meta = {'kind': 'receipt', 'event_id': 'hand', 'refs': ['AGENTS.md'], 'created_at': created.isoformat()}
        write(self.vault / 'receipts' / 'hand-made.md', '---\n' + json.dumps(meta) + '\n---\nNo title line.\n')
        day = created.astimezone().date().isoformat()
        mine = write(self.vault / 'daily' / f'{day}.md', '# My own diary\n')
        result = brain.sync(self.vault)
        self.assertEqual(result['written'], [])
        self.assertEqual(mine.read_text(encoding='utf-8'), '# My own diary\n')
        self.assertEqual(result['skipped'][0]['source'], f'daily/{day}.md')
        mine.unlink()
        self.assertEqual(brain.sync(self.vault)['written'], [f'daily/{day}.md'])
        self.assertIn('· Manual\n\nNo title line.\n\n[[receipts/hand-made.md|receipt]]\n', mine.read_text(encoding='utf-8'))
        self.assertEqual(brain.sync(self.vault)['written'], [])

    def test_doctor_reports_unreadable_files_and_stale_views(self):
        self.assertEqual(brain.doctor(self.vault)['status'], 'ok')
        write(self.vault / 'tasks' / 'broken.md', 'no frontmatter\n')
        meta = {'kind': 'receipt', 'event_id': 'r', 'refs': ['AGENTS.md'], 'created_at': '2026-01-05T12:00:00+00:00'}
        write(self.vault / 'receipts' / 'r.md', '---\n' + json.dumps(meta) + '\n---\n[X] y\n')
        report = brain.doctor(self.vault)
        self.assertEqual(report['status'], 'warning')
        text = '\n'.join(report['problems'])
        self.assertIn('tasks/broken.md', text)
        self.assertIn('daily view out of date', text)


class CommandLineTests(unittest.TestCase):
    def test_the_installed_file_runs_from_the_vault_and_refuses_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / 'vault'
            vault.mkdir()
            shutil.copyfile(ROOT / 'engine' / 'brain.py', vault / 'brain.py')
            env = {**os.environ, 'PYTHONUTF8': '1'}

            def run(*args: str, stdin: str = '') -> subprocess.CompletedProcess:
                return subprocess.run([sys.executable, str(vault / 'brain.py'), *args], cwd=vault, input=stdin,
                                      capture_output=True, text=True, encoding='utf-8', env=env, timeout=60)

            refused = run('sync')
            self.assertEqual(refused.returncode, 1)
            self.assertIn('.brain', json.loads(refused.stderr)['message'])
            (vault / '.brain').mkdir()
            write(vault / 'AGENTS.md', '# Vault\n')
            payload = json.dumps({'event_id': 'cli', 'summary': '[CLI] works', 'refs': ['AGENTS.md']})
            done = run('receipt', '--file', '-', '--harness', 'codex', stdin=payload)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)['status'], 'succeeded')
            bad = run('receipt', '--file', '-', stdin='{"event_id": "x", "summary": "y"}')
            self.assertEqual(bad.returncode, 1)
            self.assertEqual(json.loads(bad.stderr)['error'], 'Refused')
            self.assertEqual(json.loads(run('doctor').stdout)['receipts'], 1)


if __name__ == '__main__':
    unittest.main()
