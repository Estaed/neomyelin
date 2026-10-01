from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, task, write, write_config

import config
import recall


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.vault, cfg = make_vault(base)
        patches = [
            mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(base, cfg)),
                                         'NEOMYELIN_RECALL': 'bm25'}),
            mock.patch.object(recall, 'STATE_DIR', base / 'state'),
            mock.patch.object(recall, 'OLLAMA_URL', 'http://127.0.0.1:9'),  # nothing listens there
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        write(self.vault / 'knowledge' / 'otter.md', '# Kelp forest\n\nThe sea otter wraps itself in kelp to sleep.\n')
        write(self.vault / 'knowledge' / 'bread.md', '# Bread\n\nSourdough needs a starter fed daily.\n')

    def tearDown(self):
        self.tmp.cleanup()

    def paths(self, query: str, **kwargs) -> list[str]:
        return [row['path'] for row in recall.search(query, **kwargs)[1]]

    def test_finds_the_note_by_a_phrase_with_another_word_form(self):
        self.assertEqual(self.paths('otter sleeping in kelp')[0], 'knowledge/otter.md')
        self.assertEqual(self.paths('quantum chromodynamics gluon'), [])

    def test_folding_matches_accents_and_turkish_letters(self):
        write(self.vault / 'notes' / 'tr.md', '# Kayıt\n\nGünlük kayıt düzeni ve café notları.\n')
        self.assertEqual(self.paths('kayitlari'), ['notes/tr.md'])
        self.assertEqual(self.paths('cafe'), ['notes/tr.md'])

    def test_scope_skips_what_is_not_a_note(self):
        write(self.vault / '.hidden' / 'x.md', 'zebra')
        write(self.vault / 'daily' / '2026-01-01.md', 'zebra')
        write(self.vault / '900-Archive' / 'old.md', 'zebra')
        write(self.vault / 'AGENTS.md', 'zebra')
        write(self.vault / 'view.md', '---\n{"generated": true}\n---\nzebra\n')
        task(self.vault, 'closed', title='zebra closed', status='done')
        task(self.vault, 'open', title='zebra open', status='active', next_action='feed')
        write(self.vault / 'projects' / 'AGENTS.md', 'zebra in a subfolder is a note')
        for relative in ('knowledge/index.md', 'knowledge/log.md'):
            write(self.vault / relative, 'zebra table')
        write(self.vault / 'knowledge' / 'concepts' / 'zebra.md', '# Zebra\n\nStripes.\n')
        self.assertEqual(sorted(self.paths('zebra', k=20)),
                         ['knowledge/concepts/zebra.md', 'projects/AGENTS.md', 'tasks/open.md'])

    def test_never_private_or_archive_but_every_other_numbered_folder(self):
        for name in config.FOLDERS:
            write(self.vault / name / 'note.md', f'# {name}\n\nThe heron waits in the reeds.\n')
        write(self.vault / config.FOLDERS[4] / 'Private' / 'papers.md', 'heron passport scan')
        write(self.vault / config.FOLDERS[1] / 'Old Archive' / 'x.md', 'heron in an old log')
        write(self.vault / config.COMPANION / 'Notes.md', 'heron seen by the companion')
        expected = {f'{name}/note.md' for name in config.FOLDERS
                    if not name.startswith(('700-', '900-'))} | {f'{config.COMPANION}/Notes.md'}
        self.assertEqual(set(self.paths('heron', k=20)), expected)
        self.assertFalse([key for key in recall.documents(self.vault)
                          if key.startswith((config.FOLDERS[5], config.FOLDERS[7]))])

    def test_historical_note_is_marked_in_the_preview(self):
        write(self.vault / 'knowledge' / 'old.md',
              '---\nstatus: historical\n---\n# Old pump\n\n> History: removed in spring.\n\n'
              'The walrus pump ran nightly.\n')
        rows = recall.search('walrus pump')[1]
        self.assertEqual(rows[0]['preview'], '(historical) The walrus pump ran nightly.')

    def test_task_found_by_its_title_and_next_step(self):
        task(self.vault, 'prune', title='Prune the roses', status='active', next_action='buy shears')
        self.assertEqual(self.paths('shears')[0], 'tasks/prune.md')

    def test_incremental_build_and_removal(self):
        first = recall.build('bm25')
        self.assertEqual(first['changed'], first['docs'])
        self.assertEqual(recall.build('bm25')['changed'], 0)
        (self.vault / 'knowledge' / 'otter.md').unlink()
        self.assertEqual(self.paths('otter kelp'), [])

    def test_stale_search_warns_instead_of_refreshing(self):
        recall.build('bm25')
        write(self.vault / 'knowledge' / 'new.md', 'walrus')
        lines: list[str] = []
        self.assertEqual(self.paths('walrus', refresh=False, log=lines.append), [])
        self.assertTrue(any('[stale index] 1 notes changed' in line for line in lines), lines)
        self.assertEqual(self.paths('walrus'), ['knowledge/new.md'])

    def test_non_utf8_file_is_reported_not_skipped_in_silence(self):
        (self.vault / 'knowledge' / 'latin.md').write_bytes('caf\xe9'.encode('latin-1'))
        summary = recall.build('bm25')
        self.assertEqual(summary['unreadable'], ['knowledge/latin.md'])
        self.assertIn('NOT searchable', '\n'.join(recall.build_report(summary)))

    def test_engine_choice(self):
        self.assertEqual(recall.choose_engine()[0], 'bm25')
        with mock.patch.dict(os.environ, {'NEOMYELIN_RECALL': ''}):
            engine, why = recall.choose_engine()
            self.assertEqual(engine, 'bm25')
            self.assertIn('not reachable', why)
        with mock.patch.dict(os.environ, {'NEOMYELIN_RECALL': 'elastic'}):
            with self.assertRaises(recall.RecallError):
                recall.choose_engine()

    def test_forced_ollama_without_ollama_fails_loudly(self):
        recall.build('bm25')
        with mock.patch.dict(os.environ, {'NEOMYELIN_RECALL': 'ollama'}):
            with self.assertRaisesRegex(recall.RecallError, 'did not answer'):
                recall.search('otter')

    def test_auto_falls_back_to_bm25_when_ollama_breaks(self):
        lines: list[str] = []
        with mock.patch.dict(os.environ, {'NEOMYELIN_RECALL': ''}), \
                mock.patch.object(recall, 'ollama_ready', return_value=True):
            engine, results = recall.search('otter kelp', log=lines.append)
        self.assertEqual(engine, 'bm25')
        self.assertEqual(results[0]['path'], 'knowledge/otter.md')
        self.assertTrue(any('answering with bm25' in line for line in lines), lines)

    def test_cli_json_and_preview(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = recall.main(['sea otter', '--json'])
        self.assertEqual(code, 0, err.getvalue())
        rows = json.loads(out.getvalue())
        self.assertEqual(rows[0]['path'], 'knowledge/otter.md')
        self.assertEqual(rows[0]['preview'], 'The sea otter wraps itself in kelp to sleep.')
        self.assertIn('engine: bm25', err.getvalue())

    def test_warm_builds_the_index_in_advance(self):
        self.assertEqual(recall.warm(), 0)
        self.assertEqual(recall.staleness(recall._load_index('bm25')['docs'], recall.documents(self.vault)), (0, 0))

    def test_terms(self):
        self.assertEqual(recall.terms('Sleeping in KELP'), ['sleeping', 'sleep*', 'in', 'kelp'])


if __name__ == '__main__':
    unittest.main()
