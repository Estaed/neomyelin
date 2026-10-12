"""The nightly knowledge audit asks for symptom lines and writes them as `symptoms:` in the note it
creates, in the form recall reads; a missing or malformed list drops only that line."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import make_vault, receipt, write
import knowledge_audit as audit
import recall

NOW = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)
GAP = {'id': 'lesson:1', 'reason': 'This durable lesson is missing from the notes.', 'target': 'new',
       'title': 'CSS variable url() resolves against the stylesheet',
       'note': 'A url() in a custom property resolves against the stylesheet that reads it.'}


class SymptomLineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault, self.cfg = make_vault(Path(temporary.name), language='Turkish')
        self.cfg['companion_dir'] = 'companion'
        write(self.vault / 'companion' / 'Evolution.md', '# Evolution\n')
        receipt(self.vault, 'lesson', '# Session\n\n**Learning**\n- A url() in a CSS variable resolves '
                'against the stylesheet.\n', created_at='2026-09-30T12:00:00+00:00', refs=[])
        self.prompts: list[str] = []

    def run_audit(self, **fields) -> str:
        """One nightly run whose model reply carries GAP plus `fields`; the new note's text."""
        reply = json.dumps({'reviewed_ids': ['lesson:1'], 'gaps': [{**GAP, **fields}],
                            'conflicts': [], 'ideas': []})

        def judge(prompt, stage, **kwargs):
            self.prompts.append(prompt)
            return reply

        with mock.patch.object(audit.stage, 'run_in_stage', side_effect=judge):
            self.assertEqual(audit.run(self.cfg, NOW), 0)
        notes = list((self.vault / 'knowledge' / 'concepts').glob('*.md'))
        self.assertEqual(len(notes), 1)
        return notes[0].read_text(encoding='utf-8')

    def test_the_symptoms_land_in_the_new_note_as_recall_reads_them(self):
        lines = ['why does the grain not show on the other screen', 'texture gone on the second page']
        text = self.run_audit(symptoms=lines)
        self.assertEqual(recall.symptom_lines(text), lines)
        self.assertIn('symptoms: ["why does the grain not show on the other screen", '
                      '"texture gone on the second page"]\n', text)
        self.assertTrue(text.startswith('---\ntitle: '), text)

    def test_each_line_is_one_line_of_at_most_120_chars_and_at_most_three_are_kept(self):
        text = self.run_audit(symptoms=['  first  ', 'two\nlines', 'line\u2028separator', 7, '', 'x' * 130,
                                        'third', 'fourth'])
        self.assertEqual(recall.symptom_lines(text), ['first', 'x' * 120, 'third'])

    def test_a_missing_or_malformed_list_drops_only_the_line(self):
        for symptoms in (None, 'why does the grain not show', {'a': 'b'}, [], ['\n', 3]):
            with self.subTest(symptoms=symptoms):
                for path in (self.vault / 'knowledge').rglob('*.md'):
                    path.unlink()
                text = self.run_audit() if symptoms is None else self.run_audit(symptoms=symptoms)
                self.assertNotIn('symptoms:', text)
                self.assertIn('A url() in a custom property resolves against the stylesheet', text)

    def test_the_prompt_asks_for_symptoms_in_the_vault_language(self):
        self.run_audit(symptoms=['grain gone'])
        prompt = self.prompts[0]
        self.assertIn('symptoms in Turkish', prompt)
        self.assertIn("the user's own casual words", prompt)
        self.assertIn("never the solution's name", prompt)
        self.assertIn('"symptoms":["2-3 lines, one line each, at most 120 chars"]', prompt)


if __name__ == '__main__':
    unittest.main()
