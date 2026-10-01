from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import COMPANION, make_vault, write, write_config

import config


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault, self.cfg = make_vault(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_config_passes(self):
        self.assertEqual(config.validate(self.cfg), self.cfg)

    def test_each_bad_field_is_named(self):
        cases = {
            'user_name': {**self.cfg, 'user_name': ' '},
            'vault': {**self.cfg, 'vault': 'relative/path'},
            'harnesses': {**self.cfg, 'harnesses': ['claude', 'vim']},
            'nightly_at': {**self.cfg, 'nightly_at': '25:00'},
            'engine': {**self.cfg, 'engine': 'unknown'},
        }
        for field, data in cases.items():
            with self.subTest(field=field), self.assertRaisesRegex(config.ConfigError, field):
                config.validate(data)
        with self.assertRaises(config.ConfigError):
            config.validate({**self.cfg, 'harnesses': ['claude', 'claude']})
        with self.assertRaises(config.ConfigError):
            config.validate({**self.cfg, 'companion_dir': '../outside'})
        with self.assertRaises(config.ConfigError):
            config.validate(['not', 'an', 'object'])

    def test_load_reads_the_env_override_and_names_the_installer_when_missing(self):
        path = write_config(Path(self.tmp.name), self.cfg)
        with mock.patch.dict(os.environ, {config.CONFIG_ENV: str(path)}):
            self.assertEqual(config.load()['assistant_name'], 'Nova')
        with self.assertRaisesRegex(config.ConfigError, 'install.py'):
            config.load(Path(self.tmp.name) / 'missing.json')

    def test_dumps_is_canonical_utf8(self):
        text = config.dumps({**self.cfg, 'language': 'Türkçe'})
        self.assertTrue(text.endswith('}\n'))
        self.assertIn('Türkçe', text)
        self.assertEqual(text, config.dumps(dict(config.validate({**self.cfg, 'language': 'Türkçe'}))))


class CompanionDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / 'vault'
        self.vault.mkdir()
        self.cfg = {'vault': str(self.vault), 'user_name': 'A', 'assistant_name': 'B',
                    'language': 'English', 'harnesses': ['claude']}

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_to_the_numbered_companion_folder(self):
        self.assertEqual(COMPANION, '850-Companion \N{CRYSTAL BALL}')
        self.assertEqual(config.companion_dir(self.cfg), self.vault / COMPANION)

    def test_finds_a_folder_named_companion_or_holding_core(self):
        (self.vault / 'My Companion').mkdir()
        self.assertEqual(config.companion_dir(self.cfg), self.vault / 'My Companion')

    def test_an_emoji_before_or_after_the_name_still_counts(self):
        for name in ('850-Companion \N{CRYSTAL BALL}', '\N{CRYSTAL BALL} 850-Companion'):
            with self.subTest(name=name):
                (self.vault / name).mkdir()
                self.assertEqual(config.companion_dir(self.cfg), self.vault / name)
                (self.vault / name).rmdir()

    def test_finds_a_folder_holding_core_md(self):
        write(self.vault / 'Identity' / 'Core.md', '# Core\n')
        self.assertEqual(config.companion_dir(self.cfg), self.vault / 'Identity')

    def test_refuses_to_guess_between_two_and_ignores_archives(self):
        (self.vault / 'Old Archive companion').mkdir()
        (self.vault / 'A companion').mkdir()
        self.assertEqual(config.companion_dir(self.cfg), self.vault / 'A companion')
        (self.vault / 'B companion').mkdir()
        self.assertIsNone(config.companion_dir(self.cfg))

    def test_config_override_wins(self):
        (self.vault / 'A companion').mkdir()
        (self.vault / 'B companion').mkdir()
        cfg = {**self.cfg, 'companion_dir': 'B companion'}
        self.assertEqual(config.companion_dir(cfg), self.vault / 'B companion')


class AgyDirTests(unittest.TestCase):
    def test_plain_agy_uses_home_gemini(self):
        home = Path('/home/sam')
        self.assertIsNone(config.agy_snap('/usr/local/bin/agy'))
        self.assertEqual(config.agy_dir(home), home / '.gemini')

    def test_snap_agy_uses_the_snap_common_folder(self):
        home = Path('/home/sam')
        self.assertEqual(config.agy_dir(home, 'antigravity-cli'),
                         home / 'snap' / 'antigravity-cli' / 'common' / '.gemini')

    @unittest.skipIf(os.name == 'nt', 'snap links exist on Linux only')
    def test_snap_name_is_read_from_the_snap_bin_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / 'snap' / 'bin'
            bin_dir.mkdir(parents=True)
            (bin_dir / 'agy').symlink_to('antigravity-cli')
            self.assertEqual(config.agy_snap(str(bin_dir / 'agy')), 'antigravity-cli')


if __name__ == '__main__':
    unittest.main()
