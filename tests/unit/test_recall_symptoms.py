"""A note's `symptoms:` frontmatter line is indexed as its own chunk, so a question asked with the
symptom finds the note titled after the fix; a note without the line indexes as before. The first
build records the notes already there, which doctor.py does not flag for a missing line."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, write, write_config

import recall

# The title and body name the fix. Of the question's words (why, does, the, grain, not, show, on,
# other, screen) they share only "the", so only the symptoms line can carry the question here.
FIX = ('A custom property that holds url(...) resolves against the stylesheet that reads it. '
       'A texture declared in theme.css breaks when page.css uses that variable: '
       'store an absolute path in the property instead.')
SYMPTOMS = '["why does the grain not show on the other screen", "grainy backdrop gone in settings"]'
CHUNK = 'When needed: why does the grain not show on the other screen / grainy backdrop gone in settings'
QUERY = 'why does the grain not show on the other screen'
TARGET = 'knowledge/concepts/css-variable-url.md'


def note(symptoms: str | None = None) -> str:
    head = '---\ntitle: "CSS variable url() resolves against the stylesheet"\n'
    if symptoms is not None:
        head += f'symptoms: {symptoms}\n'
    return head + 'type: concept\n---\n\n# CSS variable url() resolves against the stylesheet\n\n' + FIX + '\n'


class SymptomChunkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.vault, cfg = make_vault(base)
        for patch in (mock.patch.dict(os.environ, {'NEOMYELIN_CONFIG': str(write_config(base, cfg)),
                                                   'NEOMYELIN_RECALL': 'bm25'}),
                      mock.patch.object(recall, 'STATE_DIR', base / 'state'),
                      mock.patch.object(recall, 'OLLAMA_URL', 'http://127.0.0.1:9')):  # nothing listens
            patch.start()
            self.addCleanup(patch.stop)
        self.concepts = self.vault / 'knowledge' / 'concepts'
        # Notes that share some of the question's words; without the symptoms line one of them wins.
        write(self.concepts / 'screen-time.md', '# Screen time\n\nThe phone screen goes grey after nine.\n')
        write(self.concepts / 'other-tools.md', '# Other tools\n\nThe other editor is on the shelf.\n')

    def paths(self, query: str) -> list[str]:
        return [row['path'] for row in recall.search(query, k=5)[1]]

    def test_a_symptom_worded_question_finds_the_note_titled_after_the_fix(self):
        write(self.vault / TARGET, note(SYMPTOMS))
        self.assertEqual(self.paths(QUERY)[0], TARGET)

    def test_without_the_line_the_symptom_misses_it_and_its_content_still_finds_it(self):
        write(self.vault / TARGET, note())
        self.assertNotEqual(self.paths(QUERY)[0], TARGET)
        self.assertEqual(self.paths('stylesheet custom property url')[0], TARGET)

    def test_the_symptoms_are_one_extra_chunk_and_the_content_chunks_do_not_change(self):
        write(self.concepts / 'with.md', note(SYMPTOMS))
        write(self.concepts / 'without.md', note())
        recall.build('bm25')
        docs = recall._load_index('bm25')['docs']
        with_chunks = docs['knowledge/concepts/with.md']['chunks']
        without_chunks = docs['knowledge/concepts/without.md']['chunks']
        self.assertEqual(len(without_chunks), len(recall._split(recall._body('', note()))[0]))
        self.assertEqual(with_chunks[:-1], without_chunks)  # same label, same content chunks
        self.assertEqual(with_chunks[-1]['text'], CHUNK)

    def test_a_line_recall_cannot_read_adds_nothing(self):
        # Same rules as `aliases`: a one-line JSON list in the first 20 lines, else nothing.
        for name, symptoms in (('yaml', '[grain not showing, other screen]'), ('empty', '[]'),
                               ('text', 'why does the grain not show')):
            write(self.concepts / f'{name}.md', note(symptoms))
        write(self.concepts / 'block.md', note().replace('type: concept\n', 'symptoms:\n  - grain not showing\n'))
        write(self.concepts / 'plain.md', note())
        recall.build('bm25')
        docs = recall._load_index('bm25')['docs']
        for name in ('yaml', 'empty', 'text', 'block'):
            self.assertEqual(docs[f'knowledge/concepts/{name}.md']['chunks'],
                             docs['knowledge/concepts/plain.md']['chunks'], name)

    def test_the_first_build_records_the_notes_doctor_does_not_check(self):
        # Session start and the nightly run build the index before any note is written after an
        # update, so the notes there then are the ones that predate this release.
        recall.build('bm25')
        baseline = self.vault / recall.SYMPTOMS_BASELINE
        recorded = json.loads(baseline.read_text(encoding='utf-8'))
        self.assertEqual(recorded['notes'], ['other-tools.md', 'screen-time.md'])
        write(self.vault / TARGET, note())
        recall.build('bm25')
        self.assertEqual(json.loads(baseline.read_text(encoding='utf-8')), recorded)

    def test_the_ollama_engine_embeds_the_symptom_chunk_and_finds_the_note_by_it(self):
        write(self.vault / TARGET, note(SYMPTOMS))
        embedded: list[str] = []

        def embed(texts: list[str]) -> list[list[float]]:
            # A stand-in embedder: a text is "about" the symptom when it holds the word grain.
            embedded.extend(texts)
            return [[1.0, 0.0] if 'grain' in text else [0.0, 1.0] for text in texts]

        with mock.patch.dict(os.environ, {'NEOMYELIN_RECALL': 'ollama'}), \
                mock.patch.object(recall, '_embed', side_effect=embed):
            engine, rows = recall.search(QUERY, k=5)
        self.assertEqual((engine, [row['path'] for row in rows]), ('ollama', [TARGET]))
        self.assertIn('knowledge/concepts — CSS variable url() resolves against the stylesheet\n\n' + CHUNK,
                      embedded)
        chunks = recall._load_index('ollama')['docs'][TARGET]['chunks']
        self.assertEqual(len(chunks), len(recall._split(recall._body('', note()))[0]) + 1)
        self.assertEqual(chunks[-1]['text'], CHUNK)


if __name__ == '__main__':
    unittest.main()
