"""skills_hub.py: the vault's skills hub, its links into each harness and adoption of the user's skills."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import write

import skills_hub as hub  # noqa: E402

ALL = ['claude', 'codex', 'agy']
LINKED = ['claude', 'codex']  # agy gets no link: it reads the hub through its skills.json
NAMES = {'user_name': 'Alex', 'assistant_name': 'Nova', 'language': 'English', 'harnesses': ALL}


def snapshot(*bases: Path) -> dict[str, str]:
    """Every file (read through links) and every link with its target."""
    out = {}
    for base in bases:
        for root, folders, files in os.walk(base):
            for name in folders + files:
                path = Path(root) / name
                target = hub.link_target(path)
                if target is not None:
                    out[str(path)] = f'link:{target}'
                elif path.is_file():
                    out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            folders[:] = [name for name in folders if hub.link_target(Path(root) / name) is None]
    return out


class HubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.vault = self.vault_at('vault')
        self.home = self.base / 'home'
        self.dirs = hub.skill_dirs(self.home)
        write(self.vault / '.brain' / 'skills' / 'limit' / 'SKILL.md', 'limit skill\n')

    def vault_at(self, name: str) -> Path:
        vault = self.base / name
        write(vault / '.brain' / 'config.json', json.dumps({**NAMES, 'vault': str(vault)}))
        return vault

    def skill(self, folder: Path, text: str) -> Path:
        write(folder / 'SKILL.md', text)
        write(folder / 'scripts' / 'run.py', 'print(1)\n')
        return folder

    def test_links_every_hub_skill_and_a_rerun_writes_nothing(self):
        self.assertEqual(set(self.dirs), set(LINKED))
        report = hub.link(self.vault, self.home, ALL)
        self.assertIn('  skills linked: claude limit, codex limit', report)
        for harness in LINKED:
            entry = self.dirs[harness] / 'limit'
            self.assertEqual(hub.kind(entry, self.vault), 'linked')
            self.assertEqual((entry / 'SKILL.md').read_text(encoding='utf-8'), 'limit skill\n')
        self.assertFalse((self.home / '.gemini').exists(), 'link() wrote into agy\'s folder')
        before = snapshot(self.vault, self.home)
        self.assertIn('  skills linked: unchanged', hub.link(self.vault, self.home, ALL))
        self.assertEqual(before, snapshot(self.vault, self.home))
        self.assertEqual(hub.problems(self.vault, self.home, LINKED), [])

    def test_a_foreign_link_and_the_users_own_folder_stay(self):
        elsewhere = self.skill(self.base / 'dotfiles' / 'limit', 'their limit\n')
        self.assertTrue(hub.make_link(self.dirs['claude'] / 'limit', elsewhere))
        own = self.skill(self.dirs['codex'] / 'limit', 'my limit\n')
        report = hub.link(self.vault, self.home, ALL)
        self.assertEqual('  skills linked: unchanged', report[1])
        self.assertIn('claude limit (a link to', report[-1])
        self.assertIn('codex limit (a folder of your own)', report[-1])
        self.assertEqual((elsewhere / 'SKILL.md').read_text(encoding='utf-8'), 'their limit\n')
        self.assertEqual((own / 'SKILL.md').read_text(encoding='utf-8'), 'my limit\n')
        found = hub.problems(self.vault, self.home, LINKED)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(found[0].startswith('claude limit: points elsewhere'), found)

    def test_another_neomyelin_vaults_link_moves_here_and_its_skill_stays(self):
        other = self.vault_at('other')
        write(other / '.brain' / 'skills' / 'limit' / 'SKILL.md', 'other limit\n')
        hub.link(other, self.home, ['claude'])
        report = hub.link(self.vault, self.home, ['claude'])
        self.assertIn('moved over from another NeoMyelin install: claude limit', '\n'.join(report))
        self.assertEqual(hub.kind(self.dirs['claude'] / 'limit', self.vault), 'linked')
        self.assertEqual((other / '.brain' / 'skills' / 'limit' / 'SKILL.md').read_text(encoding='utf-8'),
                         'other limit\n')

    def test_a_broken_link_into_a_gone_hub_is_replaced_one_elsewhere_is_kept(self):
        gone = self.skill(self.base / 'old-vault' / '.brain' / 'skills' / 'limit', 'x\n')
        hub.make_link(self.dirs['claude'] / 'limit', gone)
        drive = self.skill(self.base / 'drive' / 'limit', 'y\n')
        hub.make_link(self.dirs['codex'] / 'limit', drive)
        hub.rmtree(self.base / 'old-vault')
        hub.rmtree(self.base / 'drive')
        self.assertIn('codex limit: broken link to', '\n'.join(hub.problems(self.vault, self.home, ALL)))
        hub.link(self.vault, self.home, ALL)
        self.assertEqual(hub.kind(self.dirs['claude'] / 'limit', self.vault), 'linked')
        self.assertEqual(hub.kind(self.dirs['codex'] / 'limit', self.vault), 'link')

    def test_a_copy_where_no_link_can_be_made_is_refreshed_and_released(self):
        with mock.patch.object(hub, 'make_link', return_value=False):
            report = hub.link(self.vault, self.home, ['claude'])
            self.assertIn('  skills copied, a link could not be made there (a rerun refreshes the copy): '
                          'claude limit', report)
            entry = self.dirs['claude'] / 'limit'
            self.assertEqual(hub.kind(entry, self.vault), 'copy')
            self.assertIn('  skills linked: unchanged', hub.link(self.vault, self.home, ['claude']))
            write(self.vault / '.brain' / 'skills' / 'limit' / 'SKILL.md', 'limit skill v2\n')
            self.assertEqual(hub.problems(self.vault, self.home, ['claude']), ['claude limit: copy out of date'])
            hub.link(self.vault, self.home, ['claude'])
        self.assertEqual((entry / 'SKILL.md').read_text(encoding='utf-8'), 'limit skill v2\n')
        self.assertEqual(hub.release(self.vault, self.home, set()), [f'  claude skill copy removed: {entry}'])
        self.assertFalse(entry.exists())
        self.assertTrue((self.vault / '.brain' / 'skills' / 'limit' / 'SKILL.md').is_file())

    def test_a_limit_copy_from_before_the_hub_becomes_a_link(self):
        legacy = write(self.dirs['claude'] / 'limit' / 'SKILL.md', 'old\n<!-- neomyelin:skill -->\n').parent
        self.assertEqual(hub.kind(legacy, self.vault), 'neomyelin')
        hub.link(self.vault, self.home, ['claude'])
        self.assertEqual(hub.kind(legacy, self.vault), 'linked')

    def test_adopt_moves_identical_skills_once_and_release_puts_them_back(self):
        mine = [self.skill(self.dirs[h] / 'mine', 'my skill\n') for h in ('claude', 'codex')]
        clash = [self.skill(self.dirs['claude'] / 'clash', 'a\n'), self.skill(self.dirs['codex'] / 'clash', 'b\n')]
        user_limit = self.skill(self.dirs['codex'] / 'limit', 'my own limit\n')
        # A folder agy does not read: nothing in it is adopted.
        agy_old = self.skill(hub.old_dirs(self.home)['agy'] / 'agyonly', 'old agy skill\n')
        self.assertIn('mine (claude, codex)', hub.describe_own(self.vault, self.home, ALL)[0])
        report = hub.adopt(self.vault, self.home, ALL, now=dt.datetime(2026, 10, 2, 1, 2, 3))
        backup = self.vault / '.brain' / '.backup' / 'skills-20261002-010203'
        self.assertIn(f'  skills adopted into the hub: mine (claude, codex) (backup: {backup}', report[0])
        self.assertIn('clash (claude, codex hold different content under this name)', report[1])
        self.assertIn('limit (codex: differs from', report[1])
        self.assertEqual(hub.kind(agy_old, self.vault), 'own')
        self.assertNotIn('agyonly', '\n'.join(report))
        source = self.vault / '.brain' / 'skills' / 'mine'
        self.assertEqual((source / 'scripts' / 'run.py').read_text(encoding='utf-8'), 'print(1)\n')
        for harness, path in zip(('claude', 'codex'), mine):
            self.assertEqual(hub.kind(path, self.vault), 'linked')
            self.assertEqual((backup / harness / 'mine' / 'SKILL.md').read_text(encoding='utf-8'), 'my skill\n')
            self.assertFalse((path.parent / '.mine.neomyelin-adopt').exists())
        index = json.loads((self.vault / '.brain' / 'skills.json').read_text(encoding='utf-8'))
        self.assertEqual(index, {'mine': {'harnesses': ['claude', 'codex'],
                                          'origins': {'claude': str(mine[0]), 'codex': str(mine[1])}}})
        for path, text in zip(clash, ('a\n', 'b\n')):
            self.assertEqual(hub.kind(path, self.vault), 'own')
            self.assertEqual((path / 'SKILL.md').read_text(encoding='utf-8'), text)
        self.assertEqual(hub.kind(user_limit, self.vault), 'own')

        hub.link(self.vault, self.home, ALL)
        self.assertEqual(hub.kind(self.dirs['claude'] / 'limit', self.vault), 'linked')
        self.assertEqual(hub.kind(agy_old, self.vault), 'own')
        before = snapshot(self.vault, self.home)
        self.assertEqual(hub.adopt(self.vault, self.home, ALL)[0], '  skills adopted: none found')
        self.assertEqual(before, snapshot(self.vault, self.home))

        hub.release(self.vault, self.home, set())
        for path in mine:
            self.assertIsNone(hub.link_target(path))
            self.assertEqual(hub.tree(path), hub.tree(source))
        self.assertEqual((agy_old / 'SKILL.md').read_text(encoding='utf-8'), 'old agy skill\n')
        self.assertFalse(os.path.lexists(self.dirs['claude'] / 'limit'))
        self.assertEqual((user_limit / 'SKILL.md').read_text(encoding='utf-8'), 'my own limit\n')
        self.assertEqual(hub.hub_skills(self.vault), ['limit', 'mine'])
        after = snapshot(self.vault, self.home)
        self.assertEqual(hub.release(self.vault, self.home, set()), [])
        self.assertEqual(after, snapshot(self.vault, self.home))

    def test_adoption_that_cannot_link_leaves_the_skill_where_it_was(self):
        mine = self.skill(self.dirs['claude'] / 'mine', 'my skill\n')
        with mock.patch.object(hub, 'make_link', return_value=False):
            report = hub.adopt(self.vault, self.home, ['claude'])
        self.assertIn('no link could be made', '\n'.join(report))
        self.assertEqual((mine / 'SKILL.md').read_text(encoding='utf-8'), 'my skill\n')
        self.assertIsNone(hub.link_target(mine))
        self.assertFalse((self.vault / '.brain' / 'skills' / 'mine').exists())
        self.assertFalse((self.vault / '.brain' / 'skills.json').exists())

    def test_an_adoption_stopped_part_way_keeps_its_record_and_release_puts_all_back(self):
        names = ('a1', 'a2', 'a3')
        for name in names:
            self.skill(self.dirs['claude'] / name, f'{name}\n')
        real_rmtree, calls = hub.rmtree, []

        def stop_after_second(path):
            real_rmtree(path)
            calls.append(path)
            if len(calls) == 2:
                raise KeyboardInterrupt  # Ctrl+C after a2's original folder went

        with mock.patch.object(hub, 'rmtree', side_effect=stop_after_second):
            with self.assertRaises(KeyboardInterrupt):
                hub.adopt(self.vault, self.home, ['claude'], now=dt.datetime(2026, 10, 2, 1, 0, 0))
        index = json.loads((self.vault / '.brain' / 'skills.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(index), ['a1', 'a2'])
        self.assertIn('a3 (claude)', hub.adopt(self.vault, self.home, ['claude'],
                                               now=dt.datetime(2026, 10, 2, 1, 0, 1))[0])
        hub.release(self.vault, self.home, set())
        for name in names:
            path = self.dirs['claude'] / name
            self.assertIsNone(hub.link_target(path), name)
            self.assertEqual((path / 'SKILL.md').read_text(encoding='utf-8'), f'{name}\n')

    def test_an_adoption_stopped_before_its_link_is_put_back_and_its_aside_cleared(self):
        mine = [self.skill(self.dirs[h] / 'a1', 'my skill\n') for h in ('claude', 'codex')]
        with mock.patch.object(hub, 'make_link', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                hub.adopt(self.vault, self.home, LINKED, now=dt.datetime(2026, 10, 2, 1, 0, 0))
        aside = self.dirs['claude'] / '.a1.neomyelin-adopt'
        self.assertEqual(sorted(os.listdir(self.dirs['claude'])), ['.a1.neomyelin-adopt'])
        index = json.loads((self.vault / '.brain' / 'skills.json').read_text(encoding='utf-8'))
        self.assertEqual(index['a1']['origins'], {'claude': str(mine[0])})
        hub.adopt(self.vault, self.home, LINKED, now=dt.datetime(2026, 10, 2, 1, 0, 1))  # the rerun
        hub.link(self.vault, self.home, LINKED)
        index = json.loads((self.vault / '.brain' / 'skills.json').read_text(encoding='utf-8'))
        self.assertEqual(index['a1']['origins'], {'claude': str(mine[0]), 'codex': str(mine[1])})
        report = '\n'.join(hub.release(self.vault, self.home, set()))
        self.assertIn(f'removed {aside}', report)
        for path in mine:
            self.assertEqual(sorted(os.listdir(path.parent)), ['a1'])
            self.assertIsNone(hub.link_target(path))
            self.assertEqual((path / 'SKILL.md').read_text(encoding='utf-8'), 'my skill\n')
        self.assertEqual(hub.hub_skills(self.vault), ['a1', 'limit'])

    def test_a_link_to_a_skill_removed_from_the_hub_is_cleared(self):
        write(self.vault / '.brain' / 'skills' / 'gone' / 'SKILL.md', 'g\n')
        hub.link(self.vault, self.home, ['claude'])
        hub.rmtree(self.vault / '.brain' / 'skills' / 'gone')
        self.assertIn('claude gone: broken link to', '\n'.join(hub.problems(self.vault, self.home, ['claude'])))
        report = hub.link(self.vault, self.home, ['claude'])
        self.assertIn('  skill links removed, their skill is no longer in the hub: claude gone', report)
        self.assertFalse(os.path.lexists(self.dirs['claude'] / 'gone'))

    def test_old_agy_links_and_copies_go_the_users_own_stay(self):
        old = hub.old_dirs(self.home)['agy']
        self.assertEqual(old, self.home / '.gemini' / 'antigravity' / 'skills')
        self.assertTrue(hub.make_link(old / 'limit', self.vault / '.brain' / 'skills' / 'limit'))
        other = self.vault_at('other')
        self.skill(other / '.brain' / 'skills' / 'theirs', 'other vault\n')
        self.assertTrue(hub.make_link(old / 'theirs', other / '.brain' / 'skills' / 'theirs'))
        write(old / 'pre-hub' / 'SKILL.md', 'x\n<!-- neomyelin:skill -->\n')  # a pre-hub install's copy
        own = self.skill(old / 'mine', 'my agy skill\n')
        elsewhere = self.skill(self.base / 'dotfiles' / 'tool', 'tool\n')
        self.assertTrue(hub.make_link(old / 'tool', elsewhere))
        report = '\n'.join(hub.link(self.vault, self.home, ALL))
        self.assertIn('old skill links removed from a folder the harness does not read', report)
        for gone in ('limit', 'theirs', 'pre-hub'):
            self.assertFalse(os.path.lexists(old / gone), gone)
        self.assertEqual((self.vault / '.brain' / 'skills' / 'limit' / 'SKILL.md').read_text(encoding='utf-8'),
                         'limit skill\n')
        self.assertEqual((other / '.brain' / 'skills' / 'theirs' / 'SKILL.md').read_text(encoding='utf-8'),
                         'other vault\n')
        self.assertEqual(hub.kind(own, self.vault), 'own')
        self.assertEqual(hub.kind(old / 'tool', self.vault), 'link')
        # Uninstall clears this vault's old link too (an install from before this change).
        self.assertTrue(hub.make_link(old / 'limit', self.vault / '.brain' / 'skills' / 'limit'))
        self.assertIn(f'  agy skill link removed: {old / "limit"}', hub.release(self.vault, self.home, set()))
        self.assertTrue((self.vault / '.brain' / 'skills' / 'limit' / 'SKILL.md').is_file())


class AgySkillsJsonTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.vault = self.base / 'vault'
        write(self.vault / '.brain' / 'config.json', json.dumps({**NAMES, 'vault': str(self.vault)}))
        write(self.vault / '.brain' / 'skills' / 'limit' / 'SKILL.md', 'limit skill\n')
        self.home = self.base / 'home'
        self.ours = (self.vault / '.brain' / 'skills').as_posix()
        # Not a snap here, so agy's skills.json is the plain ~/.gemini one.
        snap = mock.patch.object(hub.config, 'agy_snap', return_value=None)
        snap.start()
        self.addCleanup(snap.stop)
        self.index = hub.agy_index(self.home)

    def register(self) -> list[str]:
        return hub.register_agy(hub.plan_agy(self.vault, self.home), self.vault)

    def backups(self) -> list[Path]:
        return sorted(self.index.parent.glob('skills.json.neomyelin-*.bak'))

    def test_a_users_file_gets_one_entry_and_comes_back_byte_identical(self):
        self.assertEqual(self.index, self.home / '.gemini' / 'config' / 'skills.json')
        original = ('{\r\n    "inherits": [{"path": "~/team/skills.json"}],\r\n    "entries": [\r\n'
                    '        {"path": "~/my-skills", "exclude": ["old-.*"]}\r\n    ],\r\n    "x-note": "mine"\r\n}\r\n')
        self.index.parent.mkdir(parents=True)
        self.index.write_bytes(original.encode('utf-8'))
        self.assertIn('updated', self.register()[0])
        data = json.loads(self.index.read_text(encoding='utf-8'))
        self.assertEqual(data['entries'], [{'path': '~/my-skills', 'exclude': ['old-.*']}, {'path': self.ours}])
        self.assertEqual((data['inherits'], data['x-note']), ([{'path': '~/team/skills.json'}], 'mine'))
        self.assertTrue(any(path.read_bytes() == original.encode('utf-8') for path in self.backups()))
        self.assertEqual(hub.problems(self.vault, self.home, ['agy']), [])

        before = snapshot(self.home)
        self.assertIn('unchanged', self.register()[0])
        self.assertEqual(before, snapshot(self.home))

        report = hub.unregister_agy(self.vault, self.home)
        self.assertIn('as it was before install', report[0])
        self.assertEqual(self.index.read_bytes(), original.encode('utf-8'))
        before = snapshot(self.home)
        self.assertEqual(hub.unregister_agy(self.vault, self.home), [])
        self.assertEqual(before, snapshot(self.home))

    def test_a_file_install_created_goes_with_uninstall_twice_over(self):
        for _ in range(2):
            self.assertIn('created', self.register()[0])
            self.assertEqual(json.loads(self.index.read_text(encoding='utf-8')), {'entries': [{'path': self.ours}]})
            self.assertIn('removed, it held only our entry', hub.unregister_agy(self.vault, self.home)[0])
            self.assertFalse(self.index.exists())

    def test_an_entry_already_naming_the_hub_is_kept_and_nothing_added(self):
        self.index.parent.mkdir(parents=True)
        mine = {'entries': [{'path': str(self.vault / '.brain' / 'skills'), 'exclude': ['limit']}]}
        self.index.write_text(json.dumps(mine), encoding='utf-8')
        raw = self.index.read_bytes()
        self.assertIn('unchanged', self.register()[0])
        self.assertEqual(hub.unregister_agy(self.vault, self.home), [])  # the user's entry stays
        self.assertEqual(self.index.read_bytes(), raw)

    def test_entries_added_after_install_stay_on_uninstall(self):
        self.register()
        data = json.loads(self.index.read_text(encoding='utf-8'))
        data['entries'].append({'path': '~/later'})
        self.index.write_text(json.dumps(data), encoding='utf-8')
        hub.unregister_agy(self.vault, self.home)
        self.assertEqual(json.loads(self.index.read_text(encoding='utf-8')), {'entries': [{'path': '~/later'}]})

    def test_a_broken_file_stops_install_and_is_reported_on_uninstall(self):
        self.index.parent.mkdir(parents=True)
        for text in ('{"entries": [', '[]', '{"entries": {}}'):
            with self.subTest(text=text):
                self.index.write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    hub.plan_agy(self.vault, self.home)
                self.assertEqual(self.index.read_text(encoding='utf-8'), text)
                self.assertIn('by hand', hub.unregister_agy(self.vault, self.home)[0])
                self.assertTrue(hub.problems(self.vault, self.home, ['agy']))

    def test_another_neomyelin_vaults_entry_is_replaced_the_users_stay(self):
        other = self.base / 'other'
        write(other / '.brain' / 'config.json', json.dumps({**NAMES, 'vault': str(other)}))
        write(other / '.brain' / 'skills' / 'limit' / 'SKILL.md', 'other limit\n')
        self.register()  # this vault first
        hub.register_agy(hub.plan_agy(other, self.home), other)
        entries = json.loads(self.index.read_text(encoding='utf-8'))['entries']
        self.assertEqual(entries, [{'path': (other / '.brain' / 'skills').as_posix()}])
        data = {'entries': [{'path': '~/mine'}, *entries]}
        self.index.write_text(json.dumps(data), encoding='utf-8')
        report = self.register()
        self.assertIn("replaced another NeoMyelin install's entry", report[0])
        self.assertEqual(json.loads(self.index.read_text(encoding='utf-8'))['entries'],
                         [{'path': '~/mine'}, {'path': self.ours}])
        self.assertEqual(hub.unregister_agy(other, self.home), [])  # not its entry any more

    def test_doctor_problem_when_the_hub_is_not_named(self):
        found = hub.problems(self.vault, self.home, ['agy'])
        self.assertEqual(len(found), 1)
        self.assertIn('does not name', found[0])


if __name__ == '__main__':
    unittest.main()
