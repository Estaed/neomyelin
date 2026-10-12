"""doctor.check_symptom_lines: a knowledge note written without a `symptoms:` line recall can read
is a warning naming up to three files; notes older than this release and a fresh vault stay clean."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from helpers import write

import doctor
import recall

NOW = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.timezone.utc)
WITH = '---\ntitle: "Grain"\nsymptoms: ["why does the grain not show on the other screen"]\n---\n\n# Grain\n'
WITHOUT = '---\ntitle: "Grain"\n---\n\n# Grain\n\nA url() in a CSS variable resolves against the stylesheet.\n'
FIX = "; add 2-3 'when needed' lines in the user's own words"


class CheckSymptomLinesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.concepts = self.root / 'knowledge' / 'concepts'
        self.baseline = self.root / recall.SYMPTOMS_BASELINE

    def note(self, name: str, text: str = WITHOUT) -> Path:
        return write(self.concepts / name, text)

    def check(self) -> tuple[str, str]:
        return doctor.check_symptom_lines(self.root, NOW)

    def test_a_fresh_vault_stays_clean_and_checks_every_later_note(self):
        status, detail = self.check()
        self.assertEqual(status, doctor.OK, detail)
        self.assertEqual(json.loads(self.baseline.read_text(encoding='utf-8')),
                         {'recorded_at': '2026-10-12', 'notes': []})
        self.note('grain.md', WITH)
        self.assertEqual(self.check()[0], doctor.OK)
        self.note('forgotten.md')
        status, detail = self.check()
        self.assertEqual(status, doctor.WARNING)
        self.assertIn('1 knowledge note(s) lack a `symptoms: [...]` line', detail)
        self.assertTrue(detail.endswith(': forgotten.md' + FIX), detail)

    def test_notes_older_than_the_check_are_never_flagged(self):
        old = self.note('old-lesson.md')
        self.note('older-lesson.md')
        status, detail = self.check()
        self.assertEqual(status, doctor.OK, detail)
        self.assertIn('2 older not checked', detail)
        old.write_text(WITHOUT + '\n## Sourced addition\n\nMore.\n', encoding='utf-8')  # edited later
        self.assertEqual(self.check()[0], doctor.OK)
        self.note('new-lesson.md')
        status, detail = self.check()
        self.assertEqual(status, doctor.WARNING)
        self.assertTrue(detail.endswith(': new-lesson.md' + FIX), detail)

    def test_after_an_update_a_note_written_before_doctors_first_run_is_still_checked(self):
        # The update's first session start (or nightly run, before its knowledge audit) builds the
        # recall index, which records the notes there; doctor runs later, after notes were written.
        old = self.note('old-lesson.md')
        recall.record_symptoms_baseline(self.root, '2026-10-11')
        self.note('audit-lesson.md')  # the audit's model left the list out
        old.write_text(WITHOUT + '\n## Sourced addition\n\nMore.\n', encoding='utf-8')
        status, detail = self.check()
        self.assertEqual(status, doctor.WARNING)
        self.assertTrue(detail.endswith(': audit-lesson.md' + FIX), detail)
        self.note('audit-lesson.md', WITH)
        status, detail = self.check()
        self.assertEqual(status, doctor.OK, detail)
        self.assertIn('written since 2026-10-11', detail)
        self.assertIn('1 older not checked', detail)

    def test_a_line_recall_cannot_read_counts_as_missing(self):
        self.check()
        self.note('yaml.md', WITH.replace('["why does the grain not show on the other screen"]', '[grain, screen]'))
        self.note('block.md', WITHOUT.replace('---\n\n', 'symptoms:\n  - grain gone\n---\n\n'))
        self.note('empty.md', WITH.replace('["why does the grain not show on the other screen"]', '[]'))
        self.note('good.md', WITH)
        status, detail = self.check()
        self.assertEqual(status, doctor.WARNING)
        self.assertTrue(detail.endswith(': block.md, empty.md, yaml.md' + FIX), detail)

    def test_up_to_three_files_are_named(self):
        self.check()
        for name in ('a.md', 'b.md', 'c.md', 'd.md'):
            self.note(name)
        status, detail = self.check()
        self.assertEqual(status, doctor.WARNING)
        self.assertTrue(detail.startswith('4 knowledge note(s)'), detail)
        self.assertTrue(detail.endswith(': a.md, b.md, c.md ...' + FIX), detail)

    def test_three_of_the_audits_longest_names_fit_whole_and_the_fix_gives_way(self):
        self.check()
        # The audit names a note _slug(title)[:48] + '-' + 8 hex + '.md': 60 characters at most.
        names = [f'{letter * 48}-3f2a9c1d.md' for letter in 'abcd']
        for name in names:
            self.note(name)
        check = doctor._safe('Symptom lines', self.check)
        self.assertEqual(check.status, doctor.WARNING)
        self.assertLessEqual(len(check.detail), 240)
        self.assertTrue(check.detail.endswith(': ' + ', '.join(names[:3]) + ' ...'), check.detail)

    def test_longer_names_are_cut_so_that_all_three_still_show(self):
        self.check()
        for letter in 'abcd':
            self.note(f'{letter}-{"x" * 95}.md')
        check = doctor._safe('Symptom lines', self.check)
        self.assertEqual(check.status, doctor.WARNING)
        self.assertLessEqual(len(check.detail), 240)
        self.assertTrue(check.detail.endswith('… ...'), check.detail)
        for letter in 'abc':
            self.assertIn(f'{letter}-{"x" * 40}', check.detail)
        self.assertNotIn('d-x', check.detail)

    def test_an_unreadable_baseline_is_a_warning_that_says_what_to_do(self):
        write(self.baseline, '{"notes": "old.md"}')
        status, detail = self.check()
        self.assertEqual(status, doctor.WARNING)
        self.assertIn('delete it and run doctor.py again', detail)

    def test_the_check_is_part_of_the_report(self):
        self.check()
        self.note('forgotten.md')
        checks = {check.name: check for check in doctor.run_checks(self.root, NOW, home=self.root / 'home')}
        self.assertEqual(checks['Symptom lines'].status, doctor.WARNING, checks['Symptom lines'].detail)


if __name__ == '__main__':
    unittest.main()
