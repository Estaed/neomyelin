"""install.py's layout step: the numbered folders, companion files, the AGENTS.md route table."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys
import tempfile
import unittest

from helpers import COMPANION, ROOT, write

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import install  # noqa: E402

OTHER_BLOCK = '<!-- other-tool:start -->\n## Another tool\n\nIts own text.\n<!-- other-tool:end -->'


def snapshot(base: Path) -> dict[str, str]:
    return {path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file() else 'dir' for path in sorted(base.rglob('*'))}


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.cfg = {'vault': str(self.vault), 'user_name': 'Sam', 'assistant_name': 'Juno',
                    'language': 'English', 'harnesses': ['claude']}

    def layout(self) -> tuple[install.Layout, list[str]]:
        planned = install.plan_layout(self.vault, self.cfg, config.companion_dir(self.cfg))
        return planned, install.apply_layout(planned, self.vault)

    def test_templates_cover_every_folder_and_the_block_names_them_all(self):
        notes = sorted(path.name for path in (ROOT / 'templates' / 'folders').glob('*.md'))
        self.assertEqual(notes, sorted(install.folder_template(name).name for name in config.FOLDERS))
        for name in config.FOLDERS:
            text = install.folder_template(name).read_text(encoding='utf-8')
            self.assertRegex(text, r'(?m)^# \w+', name)
            self.assertRegex(text, r'(?m)^Example sub-folder: `[^`]+/`$', name)
        block = install.render_block({**self.cfg, 'language': 'Türkçe'}, config.companion_dir(self.cfg))
        self.assertTrue(block.startswith(install.BLOCK_START) and block.endswith(install.BLOCK_END))
        self.assertEqual(re.findall(r'\{\w+\}', block), [])
        for name in config.FOLDERS:
            self.assertIn(f'`{name}/`', block)
        self.assertIn(f'`{COMPANION}/Decisions.md`', block)
        self.assertIn('`Rules.md` (rules, binding)', block)
        self.assertIn('Write vault notes in Türkçe', block)
        self.assertIn(f'{install.launcher()} brain.py receipt --file <json> --harness <claude/codex/agy>', block)
        self.assertIn(f'{install.launcher()} brain.py task-create --file <json>', block)

    def test_empty_folder_gets_the_structure_and_a_second_run_writes_nothing(self):
        planned, report = self.layout()
        self.assertEqual(config.companion_dir(self.cfg), self.vault / COMPANION)
        for name in config.FOLDERS:
            self.assertEqual((self.vault / name / install.FOLDER_NOTE).read_bytes(),
                             install.folder_template(name).read_bytes())
        self.assertEqual(install.COMPANION_FILES, ('Core.md', 'Rules.md', 'Personality.md', 'Evolution.md',
                                                   'Decisions.md'))
        for name in install.COMPANION_FILES:
            self.assertEqual((self.vault / COMPANION / name).read_bytes(),
                             (ROOT / 'templates' / name).read_bytes())
        self.assertEqual((self.vault / '.brain' / 'patterns.md').read_bytes(),
                         (ROOT / 'templates' / 'patterns.md').read_bytes())
        agents = (self.vault / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertTrue(agents.startswith(install.BLOCK_START), agents[:200])
        self.assertTrue(agents.endswith(install.BLOCK_END + '\n'))
        self.assertIn('  AGENTS.md: NeoMyelin block added', report)

        before = snapshot(self.vault)
        again, report = self.layout()
        self.assertEqual((again.folders, again.files, again.agents), ([], {}, None))
        self.assertEqual(snapshot(self.vault), before)
        self.assertIn('  files created: none', report)
        self.assertIn('  AGENTS.md: NeoMyelin block unchanged', report)

    def test_an_existing_vault_keeps_every_note_and_its_companion_folder(self):
        old_companion = self.vault / '\N{CRYSTAL BALL} 850-Companion'
        core = write(old_companion / 'Core.md', '# Core\n\nMy own identity line.\n')
        evolution = write(old_companion / 'Evolution.md', '# Evolution\n\nMy own line.\n')
        goals = write(self.vault / config.FOLDERS[0] / install.FOLDER_NOTE, '# My goals\n')
        inbox = write(self.vault / '\N{INBOX TRAY} 000-Inbox' / 'Dump' / 'idea.md', 'A note of mine.\n')
        empty = self.vault / 'Templates'
        empty.mkdir()
        agents = write(self.vault / 'AGENTS.md', 'Intro.\n\n' + OTHER_BLOCK + '\n')
        kept = {path: path.read_bytes() for path in (core, evolution, goals, inbox)}

        self.assertEqual(config.companion_dir(self.cfg), old_companion)
        _, report = self.layout()
        for path, data in kept.items():
            self.assertEqual(path.read_bytes(), data, path)
        self.assertTrue(empty.is_dir(), 'an empty folder of the user was removed')
        self.assertFalse((self.vault / COMPANION).exists(), 'a second companion folder was made')
        self.assertEqual((old_companion / 'Rules.md').read_bytes(), (ROOT / 'templates' / 'Rules.md').read_bytes())
        kept_line = next(line for line in report if 'kept as they are' in line)
        self.assertIn(f'{old_companion.name}/Core.md', kept_line)
        self.assertIn(f'{old_companion.name}/Evolution.md', kept_line)
        after = agents.read_text(encoding='utf-8')
        self.assertTrue(after.startswith('Intro.\n\n' + OTHER_BLOCK + '\n\n' + install.BLOCK_START), after[:200])

    def test_rerun_replaces_only_our_block_and_keeps_crlf(self):
        old = (f'Intro paragraph.\n\n{OTHER_BLOCK}\n\n{install.BLOCK_START}\nold table\n'
               f'{install.BLOCK_END}\n\nA user paragraph after our block.\n').replace('\n', '\r\n')
        (self.vault / 'AGENTS.md').write_bytes(old.encode('utf-8'))
        _, report = self.layout()
        new = (self.vault / 'AGENTS.md').read_bytes().decode('utf-8')
        self.assertNotIn('old table', new)
        self.assertNotIn('\n', new.replace('\r\n', ''))
        head, tail = old.split(install.BLOCK_START)
        self.assertTrue(new.startswith(head))
        self.assertTrue(new.endswith(tail.split(install.BLOCK_END)[1]))
        self.assertIn('  AGENTS.md: NeoMyelin block updated', report)

    def test_broken_markers_or_a_file_in_a_folders_place_stop_before_any_write(self):
        write(self.vault / 'AGENTS.md', OTHER_BLOCK + '\n' + install.BLOCK_START + '\nno end\n')
        before = snapshot(self.vault)
        with self.assertRaisesRegex(ValueError, 'nothing was written'):
            install.plan_layout(self.vault, self.cfg, config.companion_dir(self.cfg))
        write(self.vault / 'AGENTS.md', OTHER_BLOCK + '\n')
        write(self.vault / config.FOLDERS[2], 'a file where a folder belongs')
        with self.assertRaisesRegex(ValueError, 'is not a folder'):
            install.plan_layout(self.vault, self.cfg, config.companion_dir(self.cfg))
        self.assertEqual(set(snapshot(self.vault)), set(before) | {config.FOLDERS[2]})

    def test_merge_block_into_an_empty_or_missing_file(self):
        self.assertEqual(install.merge_block('', 'B'), 'B\n')
        self.assertEqual(install.merge_block('text\n\n\n', 'B'), 'text\n\nB\n')
        with self.assertRaises(ValueError):
            install.merge_block(f'{install.BLOCK_END}\n{install.BLOCK_START}\n', 'B')


class EngineFileTests(unittest.TestCase):
    def test_a_brain_py_that_is_not_ours_stops_the_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            install._check_engine(vault)  # absent: fine
            write(vault / 'brain.py', install.ENGINE.read_text(encoding='utf-8'))
            install._check_engine(vault)  # ours: fine
            write(vault / 'brain.py', 'print("my own brain")\n')
            with self.assertRaisesRegex(ValueError, 'not NeoMyelin'):
                install._check_engine(vault)
        self.assertIn(install.ENGINE_MARK, install.ENGINE.read_bytes())


if __name__ == '__main__':
    unittest.main()
