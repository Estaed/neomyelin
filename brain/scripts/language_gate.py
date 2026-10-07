#!/usr/bin/env python3
"""Language drift: the user wrote in their own language and the reply came back in English.

After long English work (code, files, logs) a model's reply tends to slide into English. The
check is language-agnostic, because users write in any language: a prompt counts as "not English"
when its letters are mostly outside ASCII (Cyrillic, Greek, Arabic, CJK...) or when it has enough
words and almost none of them are English function words; a reply counts as English when
those function words make up a large share of its prose. Only words that are English and rare
elsewhere are counted ("a", "in", "no", "die", German "was", "will", "also" are left out), and code, paths, URLs and harness
tags are dropped first. Short or unclear text decides nothing, so the gate stays silent.

receipt_gate.py calls it: UserPromptSubmit stores the verdict on the prompt, Stop checks the
reply (Claude Code and Codex both send `last_assistant_message`). A vault configured for English
is left alone, because the instructions tell the assistant to speak the configured language.
"""
from __future__ import annotations

import re

EN_WORDS = frozenset(
    'the and is are were to of for with that this it its you your we they what which when '
    'have has had would can do does not but from there if then been these those their our '
    'about should could just only into than them he she his her who how why'.split()
)
_CODE_FENCE = re.compile(r'```.*?```', re.DOTALL)
_INLINE_CODE = re.compile(r'`[^`\n]*`')
_URL = re.compile(r'https?://\S+')
_TAG_BLOCK = re.compile(r'<([a-z][a-z0-9_-]*)[^>]*>.*?</\1>', re.DOTALL | re.IGNORECASE)
_PATH = re.compile(r'\S*[/\\]\S*')
_WORD = re.compile(r'[^\W\d_]+')

FOREIGN, ENGLISH, UNCLEAR = 'foreign', 'english', 'unclear'
PROMPT_WORDS = 4      # fewer words than this (Latin script) decides nothing
REPLY_WORDS = 25      # a reply shorter than this is not judged
NON_ASCII_SHARE = 0.3 # letters outside ASCII above this share: not English
PROMPT_EN_MAX = 0.05  # English function words at or below this share: not English
REPLY_EN_MIN = 0.18   # English function words at or above this share: English

REASON = ('[Memory: Language] {user} wrote in their own language, but your reply is in English. '
          'Rewrite the same reply in the language of {user}\'s message; code, file names, commands '
          'and quotes stay as they are. If {user} asked for English text (a translation, an '
          'English draft), keep it and add one line in their language saying so.')


def _prose(text: str) -> str:
    for pattern in (_TAG_BLOCK, _CODE_FENCE, _INLINE_CODE, _URL, _PATH):
        text = pattern.sub(' ', text)
    return text


def _measure(text: str) -> tuple[list[str], float, float]:
    """(words, share of letters outside ASCII, share of words that are English function words)."""
    words = [word.casefold() for word in _WORD.findall(_prose(text))]
    letters = sum(len(word) for word in words)
    if not letters:
        return words, 0.0, 0.0
    non_ascii = sum(1 for word in words for ch in word if ord(ch) > 127) / letters
    english = sum(word in EN_WORDS for word in words) / len(words)
    return words, non_ascii, english


def prompt_language(text: str) -> str:
    words, non_ascii, english = _measure(text)
    if not words:
        return UNCLEAR
    if non_ascii >= NON_ASCII_SHARE:
        return FOREIGN
    if len(words) < PROMPT_WORDS:
        return UNCLEAR
    if english <= PROMPT_EN_MAX:
        return FOREIGN
    return ENGLISH if english >= REPLY_EN_MIN else UNCLEAR


def reply_is_english(text: str) -> bool:
    words, non_ascii, english = _measure(text)
    return len(words) >= REPLY_WORDS and non_ascii < 0.05 and english >= REPLY_EN_MIN


def configured_english(language: object) -> bool:
    value = str(language or '').strip().casefold()
    return value in ('en', 'english') or value.startswith(('en-', 'en_', 'english'))


def drifted(prompt_verdict: str, reply: object, language: object) -> bool:
    return (prompt_verdict == FOREIGN and isinstance(reply, str) and not configured_english(language)
            and reply_is_english(reply))
