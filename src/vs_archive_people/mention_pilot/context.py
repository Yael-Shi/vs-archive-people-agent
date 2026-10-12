"""Local context checks for pilot spans.

These checks do not assign identity. A known-name hit is not proof that an
occurrence names a person. Street rejection uses the introducer ``רחוב``, not a
list of street names. The same string remains eligible in another occurrence.
"""

from __future__ import annotations

import unicodedata

# Same closed set as known-name PREFIXED classification. Not a stemmer.
HEBREW_PREFIX_LETTERS = frozenset("הובכלמש")
MAX_HEBREW_PREFIX_LETTERS = 4
# Definite ה and complementizer ש are unsafe to peel by themselves.
_UNSAFE_PREFIX_ONLY = frozenset("הש")
GENERIC_ROLE_ONLY = frozenset({"הנשיא", "הנשיאה", "הוא", "היא", "האיש", "האישה"})
_STREET_WORD = "רחוב"


def is_place_name_occurrence(body_text: str, start: int, end: int) -> bool:
    """True when this span continues a ``רחוב`` phrase or is that phrase."""
    if start < 0 or end > len(body_text) or start >= end:
        return False
    if _preceded_by_street_word(body_text, start):
        return True
    return _starts_with_street_phrase(body_text[start:end])


def prefix_is_safe(prefix: str) -> bool:
    """A peel of only ``ה`` and ``ש`` is not a safe name split."""
    if prefix == "":
        return True
    if len(prefix) > MAX_HEBREW_PREFIX_LETTERS:
        return False
    if any(character not in HEBREW_PREFIX_LETTERS for character in prefix):
        return False
    return any(character not in _UNSAFE_PREFIX_ONLY for character in prefix)


def _is_name_token_char(character: str) -> bool:
    return unicodedata.category(character)[0] in {"L", "N"}


def _preceded_by_street_word(body_text: str, start: int) -> bool:
    index = start
    while index > 0 and body_text[index - 1] in " \t":
        index -= 1
    if index == start or index < len(_STREET_WORD):
        return False
    if body_text[index - len(_STREET_WORD) : index] != _STREET_WORD:
        return False
    return _proclitic_boundary(body_text, index - len(_STREET_WORD))


def _starts_with_street_phrase(text: str) -> bool:
    index = 0
    while (
        index < len(text)
        and index < MAX_HEBREW_PREFIX_LETTERS
        and text[index] in HEBREW_PREFIX_LETTERS
    ):
        index += 1
    if index < len(text) and text[index] in HEBREW_PREFIX_LETTERS:
        return False
    if not text.startswith(_STREET_WORD, index):
        return False
    after = index + len(_STREET_WORD)
    return after == len(text) or text[after] in " \t"


def _proclitic_boundary(body_text: str, street_start: int) -> bool:
    index = street_start
    count = 0
    while (
        index > 0
        and body_text[index - 1] in HEBREW_PREFIX_LETTERS
        and count < MAX_HEBREW_PREFIX_LETTERS
    ):
        index -= 1
        count += 1
    if index > 0 and body_text[index - 1] in HEBREW_PREFIX_LETTERS:
        return False
    if index > 0 and _is_name_token_char(body_text[index - 1]):
        return False
    return True
