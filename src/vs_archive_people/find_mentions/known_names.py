"""Exact known-name spans for the official finder.

Accepted ``Person.name`` and ``PersonAlias.name`` strings are searched as
literal substrings of the unmodified body. Pending rows are excluded by the
same index as resolution. There is no fuzzy match, no casefold, no NFKC, and
no title stripping. The result has no person id.

``EXACT_BOUNDARY`` is published as that exact slice. A safe ``PREFIXED`` match
publishes the proclitic plus the catalog string as the outer span and the
catalog string as ``name_surface``. A peel of only ``ה`` or ``ש`` is refused.
``EMBEDDED`` is diagnostic only.
"""

from __future__ import annotations

import unicodedata

from vs_archive_people.artifacts.io import ArtifactError
from vs_archive_people.find_mentions.align import DiagnosticNote
from vs_archive_people.find_mentions.context import (
    HEBREW_PREFIX_LETTERS,
    MAX_HEBREW_PREFIX_LETTERS,
    is_place_name_occurrence,
    prefix_is_safe,
)
from vs_archive_people.find_mentions.registry import KNOWN_NAME_DETECTOR_ID
from vs_archive_people.find_mentions.segment import Segment
from vs_archive_people.find_mentions.spans import occurrence_spans
from vs_archive_people.mention_proposals.geometry import require_split_residue

SUPPRESSED = "suppressed"
REJECTED = "rejected"
EXACT_BOUNDARY = "EXACT_BOUNDARY"
PREFIXED = "PREFIXED"
EMBEDDED = "EMBEDDED"


class PreparedMention:
    """One detector's view of a span, before consolidation."""

    def __init__(
        self,
        *,
        segment_id: str | None,
        exact_mention: str,
        start_offset: int,
        end_offset: int,
        name_surface: str,
        name_start_offset: int,
        name_end_offset: int,
        title_surface: str | None,
        title_start_offset: int | None,
        title_end_offset: int | None,
        surface_kind: str | None,
        detector_id: str,
        known_name_match_quality: str | None,
        detector_ids: tuple[str, ...] | None = None,
    ) -> None:
        self.segment_id = segment_id
        self.exact_mention = exact_mention
        self.start_offset = start_offset
        self.end_offset = end_offset
        self.name_surface = name_surface
        self.name_start_offset = name_start_offset
        self.name_end_offset = name_end_offset
        self.title_surface = title_surface
        self.title_start_offset = title_start_offset
        self.title_end_offset = title_end_offset
        self.surface_kind = surface_kind
        self.detector_id = detector_id
        self.known_name_match_quality = known_name_match_quality
        self.detector_ids = detector_ids or (detector_id,)


def known_name_mentions(
    body_text: str,
    exact_names: list[str],
    segments: list[Segment],
) -> tuple[list[PreparedMention], list[DiagnosticNote]]:
    """Return publishable known-name spans and diagnostic notes."""
    prepared: list[PreparedMention] = []
    notes: list[DiagnosticNote] = []
    for name in exact_names:
        for start, end in occurrence_spans(body_text, name):
            quality = classify_known_match(body_text, start, end)
            segment_id = _containing_segment_id(segments, start, end)
            if quality == EMBEDDED:
                notes.append(
                    _span_note(
                        SUPPRESSED,
                        "EMBEDDED_KNOWN_NAME",
                        body_text[start:end],
                        segment_id,
                        start,
                        end,
                    )
                )
                continue
            if is_place_name_occurrence(body_text, start, end):
                notes.append(
                    _span_note(
                        SUPPRESSED,
                        "PLACE_OR_INSTITUTION_CONTEXT",
                        body_text[start:end],
                        segment_id,
                        start,
                        end,
                    )
                )
                continue
            if quality == EXACT_BOUNDARY:
                prepared.append(
                    _prepared(
                        body_text,
                        start,
                        end,
                        start,
                        end,
                        EXACT_BOUNDARY,
                        segment_id,
                    )
                )
                continue
            if quality != PREFIXED:
                continue
            prefixed = _safe_prefixed_span(body_text, start, end)
            if isinstance(prefixed, str):
                notes.append(
                    _span_note(
                        REJECTED,
                        prefixed,
                        body_text[start:end],
                        segment_id,
                        start,
                        end,
                    )
                )
                continue
            outer_start, outer_end = prefixed
            if is_place_name_occurrence(body_text, outer_start, outer_end):
                notes.append(
                    _span_note(
                        SUPPRESSED,
                        "PLACE_OR_INSTITUTION_CONTEXT",
                        body_text[outer_start:outer_end],
                        segment_id,
                        outer_start,
                        outer_end,
                    )
                )
                continue
            prepared.append(
                _prepared(
                    body_text,
                    outer_start,
                    outer_end,
                    start,
                    end,
                    PREFIXED,
                    _containing_segment_id(segments, outer_start, outer_end)
                    or segment_id,
                )
            )
    return prepared, notes


def suppress_contained(
    prepared: list[PreparedMention],
    ranges: list[tuple[int, int]],
) -> tuple[list[PreparedMention], list[DiagnosticNote]]:
    """Drop known-name spans fully inside a non-person range."""
    kept: list[PreparedMention] = []
    notes: list[DiagnosticNote] = []
    for mention in prepared:
        name_start = mention.name_start_offset
        name_end = mention.name_end_offset
        if _inside(name_start, name_end, ranges) or _inside(
            mention.start_offset, mention.end_offset, ranges
        ):
            notes.append(
                _span_note(
                    SUPPRESSED,
                    "PLACE_OR_INSTITUTION_CONTEXT",
                    mention.exact_mention,
                    mention.segment_id,
                    mention.start_offset,
                    mention.end_offset,
                )
            )
            continue
        kept.append(mention)
    return kept, notes


def _safe_prefixed_span(body_text: str, start: int, end: int) -> tuple[int, int] | str:
    prefix = _adjacent_prefix(body_text, start)
    significant = _significant_left_run(body_text, start)
    if prefix == "" or prefix != significant or not prefix_is_safe(prefix):
        return "UNSAFE_NAME_SPLIT"
    if len(prefix) > MAX_HEBREW_PREFIX_LETTERS:
        return "UNSAFE_NAME_SPLIT"
    outer_start = start - len(prefix)
    outer = body_text[outer_start:end]
    name_start = len(prefix)
    name_end = name_start + (end - start)
    try:
        require_split_residue(outer, name_start, name_end, None, None)
    except ArtifactError:
        return "UNSAFE_NAME_SPLIT"
    if outer.count("\n") > 1:
        return "OFFSET_INVARIANT_FAILED"
    return outer_start, end


def _prepared(
    body_text: str,
    outer_start: int,
    outer_end: int,
    name_start: int,
    name_end: int,
    quality: str,
    segment_id: str | None,
) -> PreparedMention:
    return PreparedMention(
        segment_id=segment_id,
        exact_mention=body_text[outer_start:outer_end],
        start_offset=outer_start,
        end_offset=outer_end,
        name_surface=body_text[name_start:name_end],
        name_start_offset=name_start,
        name_end_offset=name_end,
        title_surface=None,
        title_start_offset=None,
        title_end_offset=None,
        surface_kind=None,
        detector_id=KNOWN_NAME_DETECTOR_ID,
        known_name_match_quality=quality,
    )


def _span_note(
    bucket: str,
    reason: str,
    surface: str,
    segment_id: str | None,
    start: int,
    end: int,
) -> DiagnosticNote:
    return DiagnosticNote(
        bucket=bucket,
        surface=surface,
        reason=reason,
        segment_id=segment_id,
        start_offset=start,
        end_offset=end,
    )


def _inside(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(
        range_start <= start and end <= range_end for range_start, range_end in ranges
    )


def _containing_segment_id(segments: list[Segment], start: int, end: int) -> str | None:
    for segment in segments:
        if segment.start_offset <= start and end <= segment.end_offset:
            return segment.segment_id
    return None


def _adjacent_prefix(body_text: str, start: int) -> str:
    found: list[str] = []
    index = start
    while index > 0 and body_text[index - 1] in HEBREW_PREFIX_LETTERS:
        if len(found) == MAX_HEBREW_PREFIX_LETTERS:
            found.append(body_text[index - 1])
            break
        found.append(body_text[index - 1])
        index -= 1
    found.reverse()
    return "".join(found)


def _ignorable(character: str) -> bool:
    category = unicodedata.category(character)
    return category.startswith("M") or category == "Cf"


def _is_name_token_char(character: str) -> bool:
    return unicodedata.category(character)[0] in {"L", "N"}


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


def _right_is_name_token(body_text: str, end: int) -> bool:
    index = end
    while index < len(body_text):
        character = body_text[index]
        if _ignorable(character):
            index += 1
            continue
        return _is_name_token_char(character)
    return False


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
