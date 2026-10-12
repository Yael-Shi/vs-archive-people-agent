"""Exact known-name spans from an identity snapshot.

Accepted ``Person.name`` and ``PersonAlias.name`` strings are searched as
literal substrings of the unmodified body. Pending rows are excluded by the
same index rules as resolution. There is no fuzzy match, no casefold, no
NFKC, and no title stripping. The result is source spans only: no person id.

Each span also carries a deterministic match quality. A neighbor is a name
token character when its Unicode category is a letter (``L*``) or number
(``N*``). Combining marks (``M*``) and format characters (``Cf``) are skipped
when looking for that neighbor; they do not themselves separate tokens.
Anything else, including punctuation, symbols, and whitespace, is a boundary.
The start and end of the body are boundaries.

``EXACT_BOUNDARY`` means neither side continues a name token.
``PREFIXED`` means the right side is a boundary and the left side is one to
four characters from the closed Hebrew proclitic set ``ה ו ב כ ל מ ש``, and
nothing else. Longer left runs, or any other left-side letter or number, are
``EMBEDDED``. A name-token character on the right is always ``EMBEDDED``.
Embedded and prefixed matches are kept.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from vs_archive_people.mention_pilot.align import occurrence_spans
from vs_archive_people.mention_pilot.segment import Segment
from vs_archive_people.resolution.snapshot_index import build_index

EXACT_BOUNDARY = "EXACT_BOUNDARY"
PREFIXED = "PREFIXED"
EMBEDDED = "EMBEDDED"
# One-letter Hebrew proclitics. Stacks of these letters only, up to four.
HEBREW_PREFIX_LETTERS = frozenset("הובכלמש")
MAX_HEBREW_PREFIX_LETTERS = 4


@dataclass(frozen=True)
class KnownSpan:
    exact_mention: str
    start_offset: int
    end_offset: int
    segment_id: str | None
    match_quality: str


def classify_known_match(body_text: str, start: int, end: int) -> str:
    """Classify one literal match. Does not use identity or language detection."""
    if _right_is_name_token(body_text, end):
        return EMBEDDED
    left = _significant_left_run(body_text, start)
    if left == "":
        return EXACT_BOUNDARY
    if 1 <= len(left) <= MAX_HEBREW_PREFIX_LETTERS and all(
        character in HEBREW_PREFIX_LETTERS for character in left
    ):
        return PREFIXED
    return EMBEDDED


def find_known_name_spans(
    body_text: str,
    identity_snapshot: dict,
    segments: list[Segment],
) -> list[KnownSpan]:
    index = build_index(identity_snapshot)
    spans: list[KnownSpan] = []
    for name in index.exact_names:
        for start, end in occurrence_spans(body_text, name):
            spans.append(
                KnownSpan(
                    exact_mention=body_text[start:end],
                    start_offset=start,
                    end_offset=end,
                    segment_id=_containing_segment_id(segments, start, end),
                    match_quality=classify_known_match(body_text, start, end),
                )
            )
    spans.sort(
        key=lambda span: (span.start_offset, span.end_offset, span.exact_mention)
    )
    return spans


def _containing_segment_id(segments: list[Segment], start: int, end: int) -> str | None:
    for segment in segments:
        if segment.start_offset <= start and end <= segment.end_offset:
            return segment.segment_id
    return None


def _ignorable(character: str) -> bool:
    category = unicodedata.category(character)
    return category.startswith("M") or category == "Cf"


def _is_name_token_char(character: str) -> bool:
    return unicodedata.category(character)[0] in {"L", "N"}


def _significant_left_run(body_text: str, start: int) -> str:
    found: list[str] = []
    index = start
    while index > 0:
        index -= 1
        character = body_text[index]
        if _ignorable(character):
            continue
        if not _is_name_token_char(character):
            break
        found.append(character)
    found.reverse()
    return "".join(found)


def _right_is_name_token(body_text: str, end: int) -> bool:
    index = end
    while index < len(body_text):
        character = body_text[index]
        if _ignorable(character):
            index += 1
            continue
        return _is_name_token_char(character)
    return False
