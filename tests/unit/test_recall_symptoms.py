"""A note's `symptoms:` frontmatter lines are indexed as chunks of their own: all lines joined, then
each line alone, every one with the `When needed: ` prefix and the note's label, so a question asked
with any one symptom finds the note titled after the fix; a note without the lines indexes as
before. An index from the earlier layout (the lines joined only) is re-chunked on the next build,
only for notes with symptom lines, and answers queries until then. A preview never shows the line.
The first build records the notes already there, which doctor.py does not flag for a missing line."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from helpers import make_vault, write, write_config

import prompt_recall
import recall

# The title and body name the fix. Of the question's words (why, does, the, grain, not, show, on,
# other, screen) they share only "the", so only the symptoms lines can carry the question here.
FIX = ('A custom property that holds url(...) resolves against the stylesheet that reads it. '
       'A texture declared in theme.css breaks when page.css uses that variable: '
       'store an absolute path in the property instead.')
TITLE = 'CSS variable url() resolves against the stylesheet'
LINES = ['why does the grain not show on the other screen', 'grainy backdrop gone in settings']
SYMPTOMS = json.dumps(LINES)
JOINED = 'When needed: why does the grain not show on the other screen / grainy backdrop gone in settings'
QUERY = 'why does the grain not show on the other screen'
TARGET = 'knowledge/concepts/css-variable-url.md'
LABEL = f'knowledge/concepts — {TITLE}'


def note(symptoms: str | None = None, body: str = FIX) -> str:
    head = f'---\ntitle: "{TITLE}"\n'
    if symptoms is not None:
        head += f'symptoms: {symptoms}\n'
    return head + f'type: concept\n---\n\n# {TITLE}\n\n{body}\n'.replace('\n\n\n', '\n')


def content_chunks(text: str) -> int:
    return len(recall._split(recall._body('', text))[0])


def grain_embedder(texts: list[str]) -> list[list[float]]:
    """A stand-in embedder: a text is "about" the symptom when it holds the word grain."""
    return [[1.0, 0.0] if 'grain' in text else [0.0, 1.0] for text in texts]


TOPICS = ('grain', 'icons', 'emoji', 'sync')


def topic_embedder(texts: list[str]) -> list[list[float]]:
    """A stand-in embedder that averages like a real one: a text's vector counts the topic words it
    holds, so four unrelated lines in one chunk point between their four topics."""
    vectors = []
    for text in texts:
        words = text.casefold().split()
        vector = [float(words.count(topic)) for topic in TOPICS]
        vectors.append(vector + [0.0] if any(vector) else [0.0] * len(TOPICS) + [1.0])
    return vectors


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
        # Notes that share some of the question's words; without the symptoms lines one of them wins.
        write(self.concepts / 'screen-time.md', '# Screen time\n\nThe phone screen goes grey after nine.\n')
        write(self.concepts / 'other-tools.md', '# Other tools\n\nThe other editor is on the shelf.\n')

    def paths(self, query: str) -> list[str]:
        return [row['path'] for row in recall.search(query, k=5)[1]]

    def ollama(self, embedder):
        """The ollama engine with a stand-in embedder (no Ollama in the tests)."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, {'NEOMYELIN_RECALL': 'ollama'}))
        stack.enter_context(mock.patch.object(recall, '_embed', side_effect=embedder))
        return stack

    def earlier_index(self, engine: str) -> dict:
        """The index the release before this layout wrote: the symptom lines joined in one chunk,
        no layout mark. Returns its records."""
        docs = {}
        for key, path in recall.documents(self.vault).items():
            data = path.read_bytes()
            raw = data.decode('utf-8-sig')
            body = recall._body(key, raw)
            if not body or (recall._meta(raw) or {}).get('generated') is True:
                docs[key] = {'hash': hashlib.sha256(data).hexdigest(), 'label': key, 'chunks': []}
                continue
            pieces = recall._split(body)[0]
            if recall.symptom_lines(raw):
                pieces.append(recall.SYMPTOM_CHUNK + ' / '.join(recall.symptom_lines(raw)))
            label = recall._label(key, raw)
            docs[key] = {'hash': hashlib.sha256(data).hexdigest(), 'label': label,
                         'chunks': recall._chunks(engine, label, pieces)}
        recall._save_index(engine, {'engine': recall._engine_id(engine), 'docs': docs})
        return json.loads(json.dumps(docs))

    # --- the layout -------------------------------------------------------------------------

    def test_a_symptom_worded_question_finds_the_note_titled_after_the_fix(self):
        write(self.vault / TARGET, note(SYMPTOMS))
        self.assertEqual(self.paths(QUERY)[0], TARGET)

    def test_without_the_line_the_symptom_misses_it_and_its_content_still_finds_it(self):
        write(self.vault / TARGET, note())
        self.assertNotEqual(self.paths(QUERY)[0], TARGET)
        self.assertEqual(self.paths('stylesheet custom property url')[0], TARGET)

    def test_the_lines_are_joined_then_one_chunk_each_and_the_content_chunks_do_not_change(self):
        write(self.concepts / 'with.md', note(SYMPTOMS))
        write(self.concepts / 'without.md', note())
        recall.build('bm25')
        docs = recall._load_index('bm25')['docs']
        with_chunks = docs['knowledge/concepts/with.md']['chunks']
        without_chunks = docs['knowledge/concepts/without.md']['chunks']
        self.assertEqual(len(without_chunks), content_chunks(note()))
        self.assertEqual(with_chunks[:len(without_chunks)], without_chunks)  # same label, same content
        self.assertEqual([chunk['text'] for chunk in with_chunks[len(without_chunks):]],
                         [JOINED] + [f'When needed: {line}' for line in LINES])
        for chunk in with_chunks[len(without_chunks):]:  # the label rides on every symptom chunk
            self.assertTrue({'knowledge', 'concepts', 'css', 'stylesheet'} <= set(chunk['tf']), chunk['text'])

    def test_a_note_without_symptom_lines_indexes_exactly_as_before(self):
        raw = write(self.concepts / 'without.md', note()).read_bytes().decode('utf-8')  # as written (CRLF on Windows)
        recall.build('bm25')
        record = recall._load_index('bm25')['docs']['knowledge/concepts/without.md']
        label = recall._label('knowledge/concepts/without.md', raw)
        self.assertEqual(record['chunks'], recall._chunks('bm25', label, recall._split(recall._body('', raw))[0]))

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

    def test_the_ollama_engine_embeds_each_symptom_chunk_with_the_label(self):
        write(self.vault / TARGET, note(SYMPTOMS))
        embedded: list[str] = []

        def embed(texts):
            embedded.extend(texts)
            return grain_embedder(texts)

        with self.ollama(embed):
            engine, rows = recall.search(QUERY, k=5)
        self.assertEqual((engine, [row['path'] for row in rows]), ('ollama', [TARGET]))
        for piece in [JOINED] + [f'When needed: {line}' for line in LINES]:
            self.assertIn(f'{LABEL}\n\n{piece}', embedded)
        chunks = recall._load_index('ollama')['docs'][TARGET]['chunks']
        self.assertEqual(len(chunks), content_chunks(note()) + 1 + len(LINES))

    def test_each_line_alone_finds_the_note_its_joined_chunk_averaged_away(self):
        # Four unrelated lines in one chunk point between four topics: asked about one of them, a
        # note half about it outscored the note written for it (cosine 0.71 vs 0.50).
        lines = ['why is the grain gone', 'the icons vanish', 'an emoji breaks the path', 'sync stops at night']
        write(self.vault / TARGET, note(json.dumps(lines)))
        write(self.concepts / 'picker.md', '# Picker\n\nThe grain filter sits next to the emoji picker.\n')
        with self.ollama(topic_embedder):
            rows = recall.search('where did the grain go', k=5)[1]
        self.assertEqual(rows[0]['path'], TARGET)
        self.assertAlmostEqual(rows[1]['score'], round(1 / math.sqrt(2), 4))

    def test_bm25_finds_the_note_by_one_line_in_other_words(self):
        # The short per-line chunk outscores a short note that shares two of the words; the joined
        # chunk, four lines long, did not.
        lines = ["the grain doesn't show on the second screen", 'background image missing after build',
                 'icons vanish when the site sits in a subfolder', 'the hero banner looks blurry on retina']
        write(self.vault / TARGET, note(json.dumps(lines)))
        write(self.concepts / 'second-monitor.md', '# Second monitor\n\nThe second screen shows the dock.\n')
        write(self.concepts / 'film-grain.md', '# Film grain\n\nGrain shows in dark scenes.\n')
        self.assertEqual(self.paths('the grain is gone on my second screen')[0], TARGET)

    # --- the upgrade from the joined-only layout --------------------------------------------

    def test_an_update_rechunks_only_the_notes_with_symptom_lines_and_only_once(self):
        write(self.vault / TARGET, note(SYMPTOMS))
        for engine, embedder in (('bm25', None), ('ollama', grain_embedder)):
            with self.subTest(engine=engine), (self.ollama(embedder) if embedder else contextlib.nullcontext()):
                old = self.earlier_index(engine)
                self.assertEqual(recall.staleness(old, recall.documents(self.vault)), (1, 0))
                with mock.patch.object(recall, '_chunks', wraps=recall._chunks) as chunked:
                    summary = recall.build(engine)  # an incremental build: no full=True, no --rebuild
                    self.assertEqual([call.args[1] for call in chunked.call_args_list], [LABEL])
                    self.assertEqual((summary['changed'], summary['rechunked']), (1, 1))
                    new = recall._load_index(engine)['docs']
                    self.assertEqual(len(new[TARGET]['chunks']), len(old[TARGET]['chunks']) + len(LINES))
                    for key in old.keys() - {TARGET}:  # kept as they were, now marked
                        self.assertEqual(new[key]['chunks'], old[key]['chunks'], key)
                    self.assertEqual({record['scheme'] for record in new.values()}, {recall.SCHEME})
                    self.assertEqual(recall.staleness(new, recall.documents(self.vault)), (0, 0))
                    chunked.reset_mock()
                    self.assertEqual(recall.build(engine)['changed'], 0)
                    chunked.assert_not_called()

    def test_an_earlier_record_without_symptom_lines_is_not_stale_and_is_never_rechunked(self):
        old = self.earlier_index('bm25')  # the two setUp notes and the companion files, no symptoms
        self.assertEqual(recall.staleness(old, recall.documents(self.vault)), (0, 0))
        with mock.patch.object(recall, '_chunks', wraps=recall._chunks) as chunked:
            summary = recall.build('bm25')
        chunked.assert_not_called()
        self.assertEqual((summary['changed'], summary['rechunked']), (0, 0))
        self.assertEqual({record.get('scheme') for record in recall._load_index('bm25')['docs'].values()},
                         {recall.SCHEME})

    def test_an_earlier_record_with_symptoms_but_no_chunks_is_marked_and_not_stale_again(self):
        # A generated view has no chunks in either layout; its lines must not keep it stale.
        write(self.vault / 'view.md', '---\n{"generated": true}\n---\nsymptoms: ["grain"]\nzebra\n')
        old = self.earlier_index('bm25')
        self.assertEqual(recall.staleness(old, recall.documents(self.vault)), (1, 0))
        recall.build('bm25')
        new = recall._load_index('bm25')['docs']
        self.assertEqual((new['view.md']['chunks'], new['view.md']['scheme']), ([], recall.SCHEME))
        self.assertEqual(recall.staleness(new, recall.documents(self.vault)), (0, 0))

    def test_while_the_rechunk_runs_queries_are_answered_from_the_old_index(self):
        write(self.vault / TARGET, note(SYMPTOMS))
        embedding, release = threading.Event(), threading.Event()

        def embed(texts):
            if any(text.endswith(f'When needed: {LINES[0]}') for text in texts):  # the re-chunk's own line
                embedding.set()
                release.wait(10)
            return grain_embedder(texts)

        with self.ollama(embed):
            self.earlier_index('ollama')
            worker = threading.Thread(target=recall.build, args=('ollama',), daemon=True)
            worker.start()
            try:
                self.assertTrue(embedding.wait(10), 'the build never reached the re-chunk')
                lines: list[str] = []
                engine, rows = recall.search(QUERY, k=5, refresh=False, log=lines.append)
                hook = prompt_recall.find(QUERY, True)  # the prompt hook's no-refresh search
            finally:
                release.set()
                worker.join(10)
            self.assertFalse(worker.is_alive())
        self.assertEqual((engine, [row['path'] for row in rows]), ('ollama', [TARGET]))
        self.assertTrue(any('[stale index] 1 notes changed' in line for line in lines), lines)
        self.assertEqual([row['path'] for row in hook], [TARGET])
        record = recall._load_index('ollama')['docs'][TARGET]
        self.assertEqual((len(record['chunks']), record['scheme']), (content_chunks(note()) + 1 + len(LINES), recall.SCHEME))

    # --- previews ---------------------------------------------------------------------------

    def test_a_preview_never_shows_the_symptom_line_even_when_the_note_has_no_summary(self):
        # A note that is only a heading has no first paragraph; its preview fell back to the best
        # chunk, which a symptom-worded question makes a `When needed:` chunk.
        write(self.vault / TARGET, note(SYMPTOMS, body=''))
        self.assertIsNone(recall._summary(self.vault, TARGET))
        heading = f'# {TITLE}'
        rows = recall.search(QUERY, k=5)[1]
        self.assertEqual((rows[0]['path'], rows[0]['preview']), (TARGET, heading))
        with self.ollama(grain_embedder):
            rows = recall.search(QUERY, k=5)[1]
            self.assertEqual((rows[0]['path'], rows[0]['preview']), (TARGET, heading))
            self.earlier_index('ollama')  # the joined-only index an update answers from
            rows = recall.search(QUERY, k=5, refresh=False)[1]
            self.assertEqual((rows[0]['path'], rows[0]['preview']), (TARGET, heading))

    def test_a_summary_still_wins_and_the_json_output_keeps_its_shape(self):
        write(self.vault / TARGET, note(SYMPTOMS))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(recall.main([QUERY, '--json']), 0)
        rows = json.loads(out.getvalue())
        self.assertEqual([sorted(row) for row in rows], [['path', 'preview', 'score']] * len(rows))
        self.assertEqual((rows[0]['path'], rows[0]['preview']), (TARGET, FIX))
        self.assertFalse([row for row in rows if 'When needed' in row['preview']])

    # --- the doctor baseline ----------------------------------------------------------------

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


if __name__ == '__main__':
    unittest.main()
