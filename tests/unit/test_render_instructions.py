"""render_instructions.py: the user-level instruction block for Claude, Codex and agy."""
from __future__ import annotations

import codecs
import contextlib
import hashlib
import io
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from helpers import make_vault, write, write_config

import config  # noqa: E402
import render_instructions as ri  # noqa: E402

ALL = ['claude', 'codex', 'agy']
PLACEHOLDER = re.compile(r'\{(?:assistant|user|language|vault|companion|python|harness|writer)\}')


def snapshot(base: Path) -> dict[str, str]:
    return {path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(base.rglob('*')) if path.is_file()}


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.vault, cfg = make_vault(base, language='Türkçe')
        self.cfg = {**cfg, 'harnesses': ALL}
        self.home = base / 'home'
        self.home.mkdir()

    def test_text_names_identity_and_quotes_the_working_commands(self):
        vault = self.vault.resolve().as_posix()
        python = ri.launcher()
        for harness in ('claude', 'codex', 'agy'):
            block = ri.text(self.cfg, harness)
            self.assertTrue(block.startswith(ri.NOTE + '\n') and block.endswith('\n'))
            self.assertIsNone(PLACEHOLDER.search(block), harness)
            self.assertIn('You are Nova, working for Alex.', block)
            self.assertIn('in Türkçe', block)
            self.assertIn(f'`{vault}`', block)
            self.assertIn(f'`{python} "{vault}/brain.py" receipt --file <json> --harness {harness}`', block)
            self.assertIn(f'`{python} "{vault}/brain.py" task-create --file <json>`', block)
            self.assertIn('`[no-record]`', block)
            self.assertIn(f'`{python} "{vault}/.brain/scripts/gardener.py" record --category <c> --symptom <s> '
                          '--evidence <e> --workaround <w> --proposal <p>`', block)
            self.assertIn(f'`{python} "{vault}/.brain/scripts/recall.py" "<question>" --k 5 --json`', block)
            self.assertIn(f'{vault}/850-Companion 🔮/Evolution.md', block)
            self.assertIn(f'{vault}/850-Companion 🔮/Rules.md', block)
            for label in ('OBSERVATION:', 'REACTION:', 'CORRECTION:', '**Learning**', '[Memory: Session]'):
                self.assertIn(label, block)
            self.assertIn(ri.WRITER[harness], block)
        self.assertNotIn('--harness claude', ri.text(self.cfg, 'codex'))

    def test_claude_imports_the_vault_file_codex_and_agy_hold_a_copy(self):
        source = self.vault.resolve() / '.brain' / 'instructions' / 'claude.md'
        self.assertEqual(ri.render(self.cfg, 'claude'),
                         f'{ri.BLOCK_START}\n@{source.as_posix()}\n{ri.BLOCK_END}')
        for harness in ('codex', 'agy'):
            block = ri.render(self.cfg, harness)
            inside = block[len(ri.BLOCK_START):-len(ri.BLOCK_END)]
            self.assertEqual(inside.strip(), ri.text(self.cfg, harness).strip())

    def test_import_line_escapes_spaces_and_refuses_other_whitespace(self):
        self.assertEqual(ri.import_line(Path('/v/My Vault/.brain/instructions/claude.md')),
                         '@/v/My\\ Vault/.brain/instructions/claude.md')
        self.assertIsNone(ri.import_line(Path('/v/tab\there/claude.md')))
        spaced = {**self.cfg, 'vault': str(Path(self.tmp.name) / 'my vault')}
        (Path(self.tmp.name) / 'my vault').mkdir()
        self.assertIn('my\\ vault/.brain/instructions/claude.md', ri.render(spaced, 'claude'))

    def test_full_text_block_of_an_older_install_becomes_the_import_line(self):
        path = self.home / ri.FILES['claude']
        old = f'Mine.\n\n{ri.BLOCK_START}\n<!-- old note -->\n# Nova: memory\nlots of text\n{ri.BLOCK_END}\n'
        write(path, old)
        report = ri.apply(ri.plan(self.cfg, ['claude'], self.home))
        self.assertTrue(report[-1].startswith(f'  claude instructions: updated {path} (backup: '), report)
        self.assertEqual(path.read_text(encoding='utf-8'), f'Mine.\n\n{ri.render(self.cfg, "claude")}\n')
        source = ri.source_path(self.cfg, 'claude')
        self.assertEqual(source.read_text(encoding='utf-8'), ri.text(self.cfg, 'claude'))
        # A template change rewrites the vault's file only; the import line stays as it is.
        before = path.read_bytes()
        report = ri.apply(ri.plan(self.cfg, ['claude'], self.home, template='Changed {assistant}.'))
        self.assertEqual(report, [f'  claude instructions: text written to {source}',
                                  f'  claude instructions: unchanged {path}'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(source.read_text(encoding='utf-8'), f'{ri.NOTE}\nChanged Nova.\n')

    def test_three_files_created_then_a_rerun_changes_nothing(self):
        report = ri.apply(ri.plan(self.cfg, ALL, self.home))
        for harness in ALL:
            path = self.home / ri.FILES[harness]
            self.assertEqual(path.read_bytes(), (ri.render(self.cfg, harness) + '\n').encode('utf-8'))
            self.assertIn(f'  {harness} instructions: created {path}', report)
            source = ri.source_path(self.cfg, harness)
            self.assertEqual(source.read_bytes(), ri.text(self.cfg, harness).encode('utf-8'))
            self.assertIn(f'  {harness} instructions: text written to {source}', report)
        before = snapshot(self.home)
        targets = ri.plan(self.cfg, ALL, self.home)
        self.assertTrue(all(target.current() for target in targets))
        self.assertEqual(ri.apply(targets), [f'  {h} instructions: unchanged {self.home / ri.FILES[h]}' for h in ALL])
        self.assertEqual(before, snapshot(self.home))

    def test_user_text_bom_and_line_endings_kept_and_the_old_file_backed_up(self):
        path = self.home / ri.FILES['codex']
        original = codecs.BOM_UTF8 + 'My own rule.\r\n\r\nNo trailing newline'.encode('utf-8')
        path.parent.mkdir(parents=True)
        path.write_bytes(original)
        report = ri.apply(ri.plan(self.cfg, ['codex'], self.home))
        new = path.read_bytes()
        self.assertTrue(new.startswith(original))
        self.assertNotIn(b'\n', new.replace(b'\r\n', b''))
        self.assertTrue(new.endswith(ri.BLOCK_END.encode('utf-8') + b'\r\n'))
        backups = [item for item in path.parent.iterdir() if item.name.startswith('AGENTS.md.neomyelin-')]
        self.assertEqual([item.read_bytes() for item in backups], [original])
        self.assertIn(f'(backup: {backups[0]})', report[-1])

        # A user paragraph after our block survives a template change; only the block is replaced.
        path.write_bytes(new + b'A paragraph after the block.\r\n')
        current = path.read_bytes()
        ri.apply(ri.plan(self.cfg, ['codex'], self.home, template='Changed {assistant}.'))
        text = path.read_bytes().decode('utf-8-sig')
        self.assertIn(f'{ri.BLOCK_START}\r\n{ri.NOTE}\r\nChanged Nova.\r\n{ri.BLOCK_END}', text)
        self.assertTrue(text.startswith('My own rule.\r\n\r\nNo trailing newline\r\n\r\n'))
        self.assertTrue(text.endswith(ri.BLOCK_END + '\r\nA paragraph after the block.\r\n'))
        self.assertIn(current, [item.read_bytes() for item in path.parent.iterdir() if '.neomyelin-' in item.name])

    def test_merge_appends_after_the_existing_text_unchanged(self):
        self.assertEqual(ri.merge('', 'B'), 'B\n')
        self.assertEqual(ri.merge('text', 'B'), 'text\n\nB\n')
        self.assertEqual(ri.merge('text\n\n\n', 'B'), 'text\n\n\n\nB\n')
        old = f'a\n{ri.BLOCK_START}\nold\n{ri.BLOCK_END}\nz\n'
        self.assertEqual(ri.merge(old, 'NEW'), 'a\nNEW\nz\n')
        for broken in (f'{ri.BLOCK_START}\n', f'{ri.BLOCK_END}\n{ri.BLOCK_START}\n', old + old):
            with self.assertRaises(ValueError):
                ri.merge(broken, 'B')

    def test_broken_markers_or_bad_encoding_stop_before_any_write(self):
        write(self.home / ri.FILES['codex'], f'mine\n{ri.BLOCK_START}\nno end\n')
        before = snapshot(self.home)
        with self.assertRaisesRegex(ValueError, 'nothing was written'):
            ri.plan(self.cfg, ALL, self.home)
        (self.home / ri.FILES['codex']).write_bytes(b'caf\xe9\n')
        with self.assertRaisesRegex(ValueError, 'not UTF-8'):
            ri.plan(self.cfg, ALL, self.home)
        self.assertEqual(set(snapshot(self.home)), set(before))
        self.assertFalse((self.home / ri.FILES['claude']).exists())

    def test_a_linked_file_is_not_written(self):
        elsewhere = write(Path(self.tmp.name) / 'dotfiles' / 'CLAUDE.md', 'shared rules\n')
        link = self.home / ri.FILES['claude']
        link.parent.mkdir(parents=True)
        try:
            os.symlink(elsewhere, link)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks are not available here')
        report = ri.apply(ri.plan(self.cfg, ['claude'], self.home))
        self.assertIn('is a link', report[-1])
        self.assertEqual(elsewhere.read_text(encoding='utf-8'), 'shared rules\n')
        self.assertTrue(link.is_symlink())

    def test_codex_override_is_named(self):
        write(self.home / ri.CODEX_OVERRIDE, 'Temporary override.\n')
        report = ri.apply(ri.plan(self.cfg, ['codex'], self.home))
        self.assertIn('AGENTS.override.md', '\n'.join(report))
        self.assertIn(ri.BLOCK_START, (self.home / ri.FILES['codex']).read_text(encoding='utf-8'))
        self.assertEqual((self.home / ri.CODEX_OVERRIDE).read_text(encoding='utf-8'), 'Temporary override.\n')

    def test_command_line_check_then_write(self):
        cfg_file = write_config(Path(self.tmp.name), self.cfg)

        def run(*args: str) -> tuple[int, str]:
            out = io.StringIO()
            with mock.patch.dict(os.environ, {config.CONFIG_ENV: str(cfg_file)}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                return ri.main(['--home', str(self.home), '--harness', 'claude', '--harness', 'agy', *args]), out.getvalue()

        code, out = run('--check')
        self.assertEqual(code, 1)
        self.assertIn('MISSING', out)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(run()[0], 0)
        code, out = run('--check')
        self.assertEqual((code, out.count('OK  ')), (0, 2))
        self.assertFalse((self.home / ri.FILES['codex']).exists())


if __name__ == '__main__':
    unittest.main()
