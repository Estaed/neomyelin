"""language_gate.py: is the prompt not English, is the reply English, in any language."""
from __future__ import annotations

import unittest

import language_gate as gate

ENGLISH_REPLY = ('I changed the installer so that it asks for the repository address. The nightly run '
                 'then commits and pushes the vault once a day, and the first push sets the upstream. '
                 'I ran the tests and they pass; the gate is clean.')


class PromptLanguageTests(unittest.TestCase):
    def test_prompts_in_other_languages_and_scripts_are_not_english(self):
        prompts = {
            'tr': 'bu dosyayı düzeltir misin, testler de geçsin lütfen',
            'es': 'puedes arreglar el instalador para que pregunte por el repositorio de git',
            'de': 'kannst du bitte den Installer so ändern, was er dann auch fragt, will ich wissen',
            'fr': 'peux-tu corriger le script pour que les tests passent sur Windows',
            'pt': 'você pode corrigir o instalador para perguntar pelo repositório',
            'nl': 'kun je de installer aanpassen zodat hij naar de repository vraagt',
            'ru': 'исправь установщик чтобы он спрашивал адрес репозитория',
            'zh': '请修复安装程序，让它询问仓库地址',
            'ja': 'インストーラーを直してリポジトリのアドレスを聞くようにして',
            'ar': 'أصلح المثبت ليسأل عن عنوان المستودع',
        }
        for language, text in prompts.items():
            with self.subTest(language=language):
                self.assertEqual(gate.prompt_language(text), gate.FOREIGN)

    def test_english_prompts_are_english_and_short_or_code_prompts_decide_nothing(self):
        self.assertEqual(gate.prompt_language('can you fix the installer so that it asks for the address'),
                         gate.ENGLISH)
        for text in ('fix it', 'tamam yap', '`git push -u origin main` src/x/y.py', ''):
            with self.subTest(text=text):
                self.assertEqual(gate.prompt_language(text), gate.UNCLEAR)


class ReplyLanguageTests(unittest.TestCase):
    def test_an_english_reply_is_english(self):
        self.assertTrue(gate.reply_is_english(ENGLISH_REPLY))

    def test_replies_in_other_languages_are_not_even_with_english_code_in_them(self):
        replies = {
            'es': ('He cambiado el instalador para que pregunte la dirección del repositorio. La ejecución '
                   'nocturna hace commit y push del vault una vez al día, y el primer push configura el '
                   'upstream. Ejecuté las pruebas y pasan.'),
            'de': ('Ich habe den Installer geändert, damit er nach der Adresse des Repositorys fragt. Der '
                   'nächtliche Lauf committet und pusht den Vault einmal am Tag, und der erste Push setzt '
                   'den Upstream. Die Tests laufen durch, was ich auch will.'),
            'tr': ('Kurulumu değiştirdim, artık repo adresini soruyor.\n```python\nif the_value is not None '
                   'and it has been set:\n    return this\n```\nGece çalışan betik her gün commit ve push '
                   'yapıyor; testler geçti, kapı temiz, `git push -u origin main` ilk seferde çalışıyor ve '
                   'bundan sonra her gece aynı dalı itiyor.'),
        }
        for language, text in replies.items():
            with self.subTest(language=language):
                self.assertFalse(gate.reply_is_english(text))

    def test_a_short_reply_is_not_judged(self):
        self.assertFalse(gate.reply_is_english('Done, the tests pass and it is all fine.'))


class DriftTests(unittest.TestCase):
    def test_drift_needs_a_foreign_prompt_an_english_reply_and_a_vault_not_set_to_english(self):
        self.assertTrue(gate.drifted(gate.FOREIGN, ENGLISH_REPLY, 'Español'))
        self.assertFalse(gate.drifted(gate.ENGLISH, ENGLISH_REPLY, 'Español'))
        self.assertFalse(gate.drifted(gate.UNCLEAR, ENGLISH_REPLY, 'Español'))
        self.assertFalse(gate.drifted(gate.FOREIGN, None, 'Español'))
        for language in ('English', 'english (UK)', 'en', 'en-GB', ' EN_us '):
            with self.subTest(language=language):
                self.assertFalse(gate.drifted(gate.FOREIGN, ENGLISH_REPLY, language))


if __name__ == '__main__':
    unittest.main()
