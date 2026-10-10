"""Nightly reaction audit: only verified, unhandled praise or objections become debt, and the
state file never loses open debt or skips a message."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, receipt, write

import evidence
import reactions

REAL_COMMITS = reactions.commits


def local(*parts: int) -> dt.datetime:
    return dt.datetime(*parts).astimezone()


NOW = local(2026, 10, 9, 23, 0)
END = NOW - reactions.SETTLE
RENAME = 'Please never rename my files without asking me first'
SHORT = 'I liked how you kept the summary to three lines'
LATER = 'Why did you skip the tests again, run them every time'


def item_id(quote: str) -> str:
    return hashlib.sha1(quote.encode('utf-8')).hexdigest()[:8]


def proposal(quote: str, handled: object = False, kind: str = 'objection', **extra) -> dict:
    return {'kind': kind, 'quote': quote, 'lesson': 'Ask before renaming files.', 'where': 'Rules',
            'handled': handled, 'evidence': '', **extra}


class ReactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / 'home'
        self.vault, self.cfg = make_vault(self.base)
        self.cfg['harnesses'] = ['claude']
        self.state = self.vault / reactions.STATE
        self.lines: list[str] = []
        clock = mock.patch.object(reactions, '_now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        # No git in a unit test; the commit section has its own tests below.
        git = mock.patch.object(reactions, 'commits', return_value='(no commits)')
        git.start()
        self.addCleanup(git.stop)

    def message(self, when: dt.datetime, text: str) -> str:
        stamp = when.isoformat()
        self.lines.append(json.dumps({'type': 'user', 'entrypoint': 'cli', 'timestamp': stamp,
                                      'message': {'content': text}}))
        write(self.home / '.claude' / 'projects' / 'sample' / 's.jsonl', '\n'.join(self.lines) + '\n')
        return stamp

    def run_audit(self, answer=None, **kwargs):
        """(ok, message, ask mock, stdout)"""
        ask = mock.Mock(return_value=answer if answer is not None else [])
        if isinstance(answer, Exception):
            ask = mock.Mock(side_effect=answer)
        output = io.StringIO()
        with mock.patch.object(reactions.stage, 'ask_json', ask), contextlib.redirect_stdout(output):
            ok, text = reactions.run(self.cfg, home=self.home, **kwargs)
        return ok, text, ask, output.getvalue()

    def read(self) -> dict:
        return json.loads(self.state.read_text(encoding='utf-8'))

    def put_state(self, payload: dict) -> bytes:
        write(self.state, json.dumps(payload))
        return self.state.read_bytes()

    # --- the run ---------------------------------------------------------------------------

    def test_a_verified_unhandled_reaction_becomes_debt_and_last_moves_to_the_window_end(self):
        self.message(local(2026, 10, 9, 10, 0), RENAME)
        ok, text, ask, _ = self.run_audit([proposal('never rename my files without asking')])
        self.assertTrue(ok, text)
        self.assertEqual(ask.call_args.kwargs['job'], 'reactions')
        self.assertIn(RENAME, ask.call_args.args[0])
        state = self.read()
        self.assertEqual(state['last'], END.isoformat(timespec='seconds'))
        self.assertEqual(state['open'], [{
            'id': item_id('never rename my files without asking'), 'day': '2026-10-09',
            'kind': 'objection', 'quote': 'never rename my files without asking',
            'lesson': 'Ask before renaming files.', 'where': 'Rules'}])
        self.assertTrue(self.state.read_bytes().endswith(b'}\n'))
        self.assertNotIn(b'\r', self.state.read_bytes())

    def test_handled_never_becomes_debt(self):
        self.message(local(2026, 10, 9, 10, 0), RENAME)
        ok, text, _, _ = self.run_audit([proposal('never rename my files without asking', handled=True,
                                                  evidence='abc1234')])
        self.assertTrue(ok, text)
        self.assertIn('1 handled', text)
        self.assertEqual(self.read()['open'], [])

    def test_a_quote_the_messages_do_not_hold_is_dropped(self):
        self.message(local(2026, 10, 9, 10, 0), RENAME)
        ok, _, _, _ = self.run_audit([proposal('never move my files without asking'),
                                      proposal('you should always ask me before renaming')])
        self.assertTrue(ok)
        self.assertEqual(self.read()['open'], [])

    def test_duplicates_and_known_ids_are_not_added_again(self):
        self.message(local(2026, 10, 9, 10, 0), RENAME)
        self.message(local(2026, 10, 9, 11, 0), SHORT)
        self.message(local(2026, 10, 9, 12, 0), LATER)
        open_item = {'id': item_id('never rename my files'), 'quote': 'never rename my files',
                     'kind': 'objection', 'day': '2026-10-08', 'lesson': 'x', 'where': 'Rules'}
        self.put_state({'last': local(2026, 10, 9, 9, 0).isoformat(), 'open': [open_item],
                        'closed': {item_id('kept the summary to three lines'): '2026-10-08 -> Rules'}})
        ok, text, _, _ = self.run_audit([
            proposal('never rename my files'),                       # already open
            proposal('kept the summary to three lines', kind='praise'),  # already closed
            proposal('skip the tests again'),
            proposal('skip the tests again.'),                       # same after the strip
        ])
        self.assertTrue(ok, text)
        state = self.read()
        self.assertEqual([item['quote'] for item in state['open']],
                         ['never rename my files', 'skip the tests again'])
        self.assertIn(item_id('kept the summary to three lines'), state['closed'])

    def test_a_corrupt_or_malformed_state_is_left_byte_identical_and_the_run_fails(self):
        self.message(local(2026, 10, 9, 10, 0), RENAME)
        cases = {
            'not json': b'{"open": [{"id": "abcd1234", "quote": "a debt"',
            'a list': b'[]',
            'open not a list': b'{"open": "abcd1234"}',
            'item without an id': b'{"open": [{"quote": "a debt that must stay"}]}',
            'id not a string': b'{"open": [{"id": 7, "quote": "a debt that must stay"}]}',
            'closed not an object': b'{"open": [], "closed": ["abcd1234"]}',
            'unreadable last': b'{"open": [], "last": "yesterday evening"}',
            'not utf-8': b'{"open": [{"id": "ab", "quote": "\xff\xfe"}]}',
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.state.parent.mkdir(parents=True, exist_ok=True)
                self.state.write_bytes(raw)
                ok, text, ask, _ = self.run_audit([proposal('never rename my files without asking')])
                self.assertFalse(ok)
                self.assertIn('untouched', text)
                ask.assert_not_called()
                self.assertEqual(self.state.read_bytes(), raw)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(reactions.list_open(self.cfg), 1)
                    self.assertEqual(reactions.close('abcd1234', 'Rules', self.cfg), 1)
                self.assertEqual(self.state.read_bytes(), raw)

    def test_a_model_failure_keeps_last_and_the_open_debt(self):
        self.message(local(2026, 10, 9, 10, 0), RENAME)
        before = self.put_state({'last': local(2026, 10, 9, 8, 0).isoformat(),
                                 'open': [{'id': 'abcd1234', 'quote': 'an older debt'}]})
        for error in (RuntimeError('model CLI unavailable'), ValueError('model reply must be a JSON list'),
                      OSError('disk')):
            with self.subTest(type(error).__name__):
                ok, text, ask, _ = self.run_audit(error)
                self.assertFalse(ok)
                self.assertIn('model call failed', text)
                ask.assert_called_once()
                self.assertEqual(self.state.read_bytes(), before)

    def test_an_overflow_moves_last_only_to_the_last_message_in_the_prompt(self):
        first = self.message(local(2026, 10, 9, 10, 0), RENAME)
        self.message(local(2026, 10, 9, 11, 0), SHORT)
        self.message(local(2026, 10, 9, 12, 0), LATER)
        with mock.patch.object(reactions, 'MAX_MESSAGES', 1):   # only the first fits
            ok, text, ask, _ = self.run_audit([proposal('never rename my files without asking'),
                                               proposal('skip the tests again')])
        self.assertTrue(ok, text)
        self.assertIn('2 message(s) wait', text)
        prompt = ask.call_args.args[0]
        self.assertIn(RENAME, prompt)
        self.assertNotIn(LATER, prompt)
        state = self.read()
        self.assertEqual(state['last'], reactions._instant(first).isoformat(timespec='seconds'))
        # The quote from a message that waits is not verified against it yet.
        self.assertEqual([item['quote'] for item in state['open']], ['never rename my files without asking'])
        # The next run starts right after it: the waiting messages come, the scanned one does not.
        ok, text, ask, _ = self.run_audit([proposal('skip the tests again')])
        self.assertTrue(ok, text)
        prompt = ask.call_args.args[0]
        self.assertNotIn(RENAME, prompt)
        self.assertIn(SHORT, prompt)
        self.assertIn(LATER, prompt)
        state = self.read()
        self.assertEqual(state['last'], END.isoformat(timespec='seconds'))
        self.assertEqual([item['quote'] for item in state['open']],
                         ['never rename my files without asking', 'skip the tests again'])

    def test_no_record_and_interrupted_messages_are_left_out(self):
        self.message(local(2026, 10, 9, 10, 0), f'[no-record] {RENAME}')
        self.message(local(2026, 10, 9, 10, 30), '[Request interrupted by user for tool use]')
        self.message(local(2026, 10, 9, 11, 0), SHORT)
        items = reactions.messages(END - dt.timedelta(days=1), END, self.cfg, self.home)
        self.assertEqual([text for _, _, text in items], [SHORT])
        ok, _, ask, _ = self.run_audit([proposal('never rename my files without asking')])
        self.assertTrue(ok)
        self.assertNotIn('rename', ask.call_args.args[0])
        self.assertEqual(self.read()['open'], [])

    def test_a_message_newer_than_two_hours_waits_for_the_next_run(self):
        self.message(NOW - dt.timedelta(hours=1), RENAME)
        ok, text, ask, _ = self.run_audit([proposal('never rename my files without asking')])
        self.assertTrue(ok)
        self.assertEqual(text, 'no new user messages')
        ask.assert_not_called()
        self.assertEqual(self.read()['last'], END.isoformat(timespec='seconds'))
        later = NOW + dt.timedelta(hours=3)
        with mock.patch.object(reactions, '_now', return_value=later):
            ok, text, ask, _ = self.run_audit([proposal('never rename my files without asking')])
        self.assertTrue(ok, text)
        self.assertEqual(len(self.read()['open']), 1)

    def test_the_first_run_looks_back_one_day(self):
        self.message(END - dt.timedelta(hours=25), 'A message from long before the first run')
        self.message(END - dt.timedelta(hours=23), RENAME)
        ok, _, ask, _ = self.run_audit([])
        self.assertTrue(ok)
        prompt = ask.call_args.args[0]
        self.assertIn(RENAME, prompt)
        self.assertNotIn('long before the first run', prompt)

    def test_a_future_last_is_no_window(self):
        before = self.put_state({'last': NOW.isoformat(), 'open': []})
        ok, text, ask, _ = self.run_audit([])
        self.assertEqual((ok, text), (True, 'no settled window yet'))
        ask.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)

    def test_dry_run_and_no_write_record_nothing(self):
        self.message(local(2026, 10, 9, 10, 0), RENAME)
        ok, text, ask, output = self.run_audit([proposal('never rename my files without asking')], dry_run=True)
        self.assertTrue(ok)
        ask.assert_not_called()
        self.assertIn(RENAME, output)
        self.assertFalse(self.state.exists())
        ok, text, ask, output = self.run_audit([proposal('never rename my files without asking')], no_write=True)
        self.assertTrue(ok)
        self.assertIn('(not recorded)', text)
        self.assertIn('never rename my files without asking', output)
        self.assertFalse(self.state.exists())
        # No messages at all: a trial still writes nothing.
        ok, text, _, _ = self.run_audit([], dry_run=True, since=local(2026, 9, 1))
        self.assertFalse(self.state.exists())

    def test_no_messages_moves_last_without_a_model_call(self):
        ok, text, ask, _ = self.run_audit([])
        self.assertEqual((ok, text), (True, 'no new user messages'))
        ask.assert_not_called()
        self.assertEqual(self.read(), {'last': END.isoformat(timespec='seconds')})

    # --- verification ----------------------------------------------------------------------

    def test_verification_drops_every_weak_proposal(self):
        texts = [(local(2026, 10, 9, 10, 0).isoformat(), f'{RENAME}, he said "stop that" twice'),
                 (local(2026, 10, 9, 11, 0).isoformat(), 'Nice “clean diff” there, keep doing it like this')]
        weak = {
            'not in any message': proposal('never move my files without asking'),
            'a double quote': proposal('he said "stop that" twice'),
            'curly quotes': proposal('Nice “clean diff” there'),
            'shorter than MIN_QUOTE': proposal('ask me?!!'),
            'short once the punctuation is stripped': proposal('ask me...!!!'),
            'handled true': proposal(RENAME, handled=True),
            'handled missing': {key: value for key, value in proposal(RENAME).items() if key != 'handled'},
            'handled as a string': proposal(RENAME, handled='false'),
            'unknown kind': proposal(RENAME, kind='question'),
            'empty lesson': proposal(RENAME, lesson='   '),
            'not an object': RENAME,
        }
        self.assertLess(len('ask me'), evidence.MIN_QUOTE)
        for name, item in weak.items():
            with self.subTest(name):
                self.assertEqual(reactions.verified([item], texts), [])
        kept = reactions.verified([proposal('  never rename   my files  ')], texts)
        self.assertEqual([(item['quote'], item['day']) for item in kept], [('never rename my files', '2026-10-09')])

    def test_verification_keeps_at_most_max_reactions(self):
        text = ' '.join(f'objection number {n:02d} here' for n in range(20))
        found = reactions.verified([proposal(f'objection number {n:02d}') for n in range(20)],
                                   [(local(2026, 10, 9, 10, 0).isoformat(), text)])
        self.assertEqual(len(found), reactions.MAX_REACTIONS)

    # --- receipts and commits --------------------------------------------------------------

    def test_receipts_leave_out_habit_and_older_receipts_and_keep_marked_lines_past_the_cut(self):
        start = local(2026, 10, 9, 0, 0)
        after = local(2026, 10, 9, 12, 0).isoformat()
        receipt(self.vault, 'old', 'Old work before the window', created_at=local(2026, 10, 8, 12, 0).isoformat(),
                event_id='2026-10-08-garden-old')
        receipt(self.vault, 'habit', 'OBSERVATION: habit scan', created_at=after,
                event_id='2026-10-09-neomyelin-habit')
        long_body = '\n'.join(f'filler line {n:03d} ' + 'x' * 40 for n in range(40))
        receipt(self.vault, 'work', long_body + '\n- unmarked tail line\n- CORRECTION: "stop guessing paths"\n'
                                    '* REACTION: "the table was clear" praised',
                created_at=after, event_id='2026-10-09-garden-work')
        text = reactions.receipts(start, self.cfg)
        self.assertNotIn('Old work', text)
        self.assertNotIn('habit scan', text)
        self.assertIn('filler line 000', text)
        self.assertIn('…', text)
        self.assertNotIn('filler line 039', text)
        self.assertNotIn('unmarked tail line', text)
        self.assertIn('- CORRECTION: "stop guessing paths"', text)
        self.assertIn('* REACTION: "the table was clear" praised', text)
        self.assertEqual(reactions.receipts(local(2026, 10, 10, 0, 0), self.cfg), '(no receipts)')

    def test_a_marked_line_across_the_body_cut_is_kept_whole(self):
        line = '- CORRECTION: "never push to the main branch without asking first" -> Rules'
        body = 'f' * (reactions.MAX_RECEIPT_BODY - 20) + '\n' + line
        receipt(self.vault, 'work', body, created_at=local(2026, 10, 9, 12, 0).isoformat(),
                event_id='2026-10-09-garden-work')
        text = reactions.receipts(local(2026, 10, 9, 0, 0), self.cfg)
        self.assertIn(line, text)

    def test_git_log_lists_one_line_per_commit_without_receipts_and_daily(self):
        stdout = ('@@ abc1234 10-09 12:00 Rule: ask before renaming\n'
                  'Rules.md\nreceipts/x.md\ndaily/2026-10-09.md\n\n'
                  '@@ def5678 10-09 13:00 Many files\n' + ''.join(f'f{n}.py\n' for n in range(8)) +
                  '@@ 0011223 10-09 14:00 Only a receipt\nreceipts/y.md\n')
        done = mock.Mock(returncode=0, stdout=stdout)
        with mock.patch.object(reactions.subprocess, 'run', return_value=done):
            text = reactions._git_log(self.vault, local(2026, 10, 9, 0, 0))
        self.assertEqual(text.splitlines(), [
            'abc1234 10-09 12:00 Rule: ask before renaming — Rules.md',
            'def5678 10-09 13:00 Many files — f0.py, f1.py, f2.py, f3.py, f4.py, f5.py (+2)',
            '0011223 10-09 14:00 Only a receipt'])
        with mock.patch.object(reactions.subprocess, 'run', return_value=mock.Mock(returncode=128, stdout='x')):
            self.assertEqual(reactions._git_log(self.vault, local(2026, 10, 9, 0, 0)), '')

    def test_commits_read_the_vault_once_and_every_repository_under_projects_root(self):
        (self.vault / '.git').mkdir()
        (self.base / 'garden' / '.git').mkdir(parents=True)
        (self.base / 'notes-only').mkdir()
        self.cfg['projects_root'] = str(self.base)
        seen = []

        def log(repo, start):
            seen.append(repo.name)
            return f'abc {repo.name}'

        with mock.patch.object(reactions, '_git_log', side_effect=log):
            text = REAL_COMMITS(local(2026, 10, 9), self.cfg)    # setUp patches reactions.commits
        self.assertEqual(seen, ['vault', 'garden'])
        self.assertEqual(text, '### vault\nabc vault\n\n### garden\nabc garden')

    # --- list, close and the command line --------------------------------------------------

    def test_list_and_close(self):
        first = {'id': 'aaaa1111', 'day': '2026-10-09', 'kind': 'objection', 'quote': 'never rename my files',
                 'lesson': 'Ask first.', 'where': 'Rules'}
        second = {**first, 'id': 'bbbb2222', 'quote': 'kept the summary short', 'kind': 'praise'}
        last = local(2026, 10, 9, 21, 0).isoformat(timespec='seconds')
        self.put_state({'last': last, 'open': [first, second], 'closed': {}})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(reactions.list_open(self.cfg), 0)
        self.assertIn('aaaa1111', output.getvalue())
        self.assertIn('"kept the summary short"', output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reactions.close('aaaa1111', 'Rules.md', self.cfg), 0)
        state = self.read()
        self.assertEqual([item['id'] for item in state['open']], ['bbbb2222'])
        self.assertEqual(state['closed'], {'aaaa1111': f'{dt.date.today().isoformat()} -> Rules.md'})
        self.assertEqual(state['last'], last)
        before = self.state.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reactions.close('aaaa1111', 'Rules.md', self.cfg), 1)
            self.assertEqual(reactions.close('nope', 'Rules.md', self.cfg), 1)
        self.assertEqual(self.state.read_bytes(), before)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reactions.close('bbbb2222', 'skill:summaries', self.cfg), 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(reactions.list_open(self.cfg), 0)
        self.assertEqual(output.getvalue().strip(), 'no open reaction debt')

    def test_list_with_no_state_file(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(reactions.list_open(self.cfg), 0)
        self.assertEqual(output.getvalue().strip(), 'no open reaction debt')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reactions.close('aaaa1111', 'Rules', self.cfg), 1)
        self.assertFalse(self.state.exists())

    def test_command_line_usage_errors(self):
        self.put_state({'open': [{'id': 'aaaa1111', 'quote': 'never rename my files'}]})
        before = self.state.read_bytes()
        with mock.patch.object(reactions.config, 'load', return_value=self.cfg), \
                mock.patch.object(reactions.config, 'force_utf8'), \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reactions.main(['close', 'aaaa1111']), 2)
            self.assertEqual(reactions.main(['close', 'aaaa1111', '   ']), 2)
            with self.assertRaises(SystemExit) as raised:
                reactions.main(['--since', '2026-10-01'])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(self.state.read_bytes(), before)
            self.assertEqual(reactions.main(['close', 'aaaa1111', ' Rules ']), 0)
        self.assertEqual(self.read()['closed']['aaaa1111'], f'{dt.date.today().isoformat()} -> Rules')


if __name__ == '__main__':
    unittest.main()
