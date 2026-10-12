"""Map Gemini proposals onto exact source offsets.

A surface may occur more than once in its window. Occurrences are the
non-overlapping left-to-right matches of that exact string. When more than one
exists, an ``occurrence`` hint (1-based) is required and is checked. This
module does not choose the first match, and it does not invent an offset.

A proposal may name the next segment when ``break_after`` is a single ``\\n``.
The published span is then a slice of the original body, including that break.
Title and name surfaces are accepted only as unique exact substrings of that
slice. A grammatical prefix is kept in the full slice and omitted from the
name slice only when the proposal's name is that exact remainder.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from vs_archive_people.mention_pilot.context import (
    GENERIC_ROLE_ONLY,
    HEBREW_PREFIX_LETTERS,
    MAX_HEBREW_PREFIX_LETTERS,
    is_place_name_occurrence,
    prefix_is_safe,
)
from vs_archive_people.mention_pilot.schema import GeminiProposal
from vs_archive_people.mention_pilot.segment import Segment


@dataclass(frozen=True)
class AlignedMention:
    segment_id: str
    end_segment_id: str | None
    exact_mention: str
    start_offset: int
    end_offset: int
    name_surface: str
    name_start_offset: int
    name_end_offset: int
    title_surface: str | None
    title_start_offset: int | None
    title_end_offset: int | None
    gemini_kind: str | None


@dataclass(frozen=True)
class UnalignedMention:
    segment_id: str
    surface: str
    gemini_kind: str | None
    occurrence: int | None
    reason: str


@dataclass(frozen=True)
class _Split:
    name_surface: str
    name_start: int
    name_end: int
    title_surface: str | None
    title_start: int | None
    title_end: int | None


def occurrence_spans(text: str, surface: str) -> list[tuple[int, int]]:
    """Non-overlapping left-to-right matches. An empty surface matches nothing."""
    if surface == "":
        return []
    found: list[tuple[int, int]] = []
    cursor = 0
    while True:
        index = text.find(surface, cursor)
        if index < 0:
            return found
        found.append((index, index + len(surface)))
        cursor = index + len(surface)


def align_proposals(
    body_text: str,
    segments: list[Segment],
    proposals: list[GeminiProposal],
) -> tuple[list[AlignedMention], list[UnalignedMention], list[tuple[int, int]]]:
    """Return aligned mentions, rejected proposals, and spans that suppress names.

    The third list holds source ranges of place or institution occurrences.
    A known-name span inside one of those ranges is not a person candidate.
    """
    by_id = {segment.segment_id: segment for segment in segments}
    ordered = list(segments)
    aligned: list[AlignedMention] = []
    unaligned: list[UnalignedMention] = []
    suppressed: list[tuple[int, int]] = []
    for proposal in proposals:
        segment = by_id.get(proposal.segment_id)
        if segment is None:
            unaligned.append(_unaligned(proposal, "UNKNOWN_SEGMENT"))
            continue
        if proposal.surface == "":
            unaligned.append(_unaligned(proposal, "EMPTY_SURFACE"))
            continue
        window = _window(ordered, segment, proposal.end_segment_id)
        if isinstance(window, str):
            unaligned.append(_unaligned(proposal, window))
            continue
        window_start, window_end, end_segment_id = window
        window_text = body_text[window_start:window_end]
        spans = occurrence_spans(window_text, proposal.surface)
        if not spans:
            unaligned.append(_unaligned(proposal, "SURFACE_NOT_IN_SEGMENT"))
            continue
        if len(spans) > 1 and proposal.occurrence is None:
            unaligned.append(_unaligned(proposal, "AMBIGUOUS_REPEATED_SURFACE"))
            continue
        if proposal.occurrence is None:
            local_start, local_end = spans[0]
        elif proposal.occurrence < 1 or proposal.occurrence > len(spans):
            unaligned.append(_unaligned(proposal, "OCCURRENCE_OUT_OF_RANGE"))
            continue
        else:
            local_start, local_end = spans[proposal.occurrence - 1]
        start = window_start + local_start
        end = window_start + local_end
        exact = body_text[start:end]
        if exact != proposal.surface or exact != window_text[local_start:local_end]:
            unaligned.append(_unaligned(proposal, "OFFSET_INVARIANT_FAILED"))
            continue
        if proposal.referent == "NON_PERSON" or is_place_name_occurrence(
            body_text, start, end
        ):
            suppressed.append((start, end))
            unaligned.append(_unaligned(proposal, "PLACE_OR_INSTITUTION_CONTEXT"))
            continue
        split = _split_for_proposal(exact, proposal)
        if isinstance(split, str):
            unaligned.append(_unaligned(proposal, split))
            continue
        name_start = start + split.name_start
        name_end = start + split.name_end
        if is_place_name_occurrence(body_text, name_start, name_end):
            suppressed.append((start, end))
            unaligned.append(_unaligned(proposal, "PLACE_OR_INSTITUTION_CONTEXT"))
            continue
        title_start = None if split.title_start is None else start + split.title_start
        title_end = None if split.title_end is None else start + split.title_end
        aligned.append(
            AlignedMention(
                segment_id=segment.segment_id,
                end_segment_id=end_segment_id,
                exact_mention=exact,
                start_offset=start,
                end_offset=end,
                name_surface=split.name_surface,
                name_start_offset=name_start,
                name_end_offset=name_end,
                title_surface=split.title_surface,
                title_start_offset=title_start,
                title_end_offset=title_end,
                gemini_kind=proposal.kind,
            )
        )
    return aligned, unaligned, suppressed


def _window(
    segments: list[Segment], segment: Segment, end_segment_id: str | None
) -> tuple[int, int, str | None] | str:
    if end_segment_id is None:
        return segment.start_offset, segment.end_offset, None
    index = next(
        (position for position, item in enumerate(segments) if item is segment),
        None,
    )
    if index is None or index + 1 >= len(segments):
        return "END_SEGMENT_NOT_NEXT_LINE"
    following = segments[index + 1]
    if following.segment_id != end_segment_id or segment.break_after != "\n":
        return "END_SEGMENT_NOT_NEXT_LINE"
    return segment.start_offset, following.end_offset, following.segment_id


def _split_for_proposal(full: str, proposal: GeminiProposal) -> _Split | str:
    if proposal.kind == "TITLE_AND_NAME":
        return _required_title_split(
            full, proposal.title_surface, proposal.name_surface
        )
    supplied = _resolve_split(full, proposal.name_surface, proposal.title_surface)
    if isinstance(supplied, str) and supplied != "TITLE_OR_ROLE_WITHOUT_NAME":
        return _Split(full, 0, len(full), None, None, None)
    return supplied


def _required_title_split(
    full: str, title: str | None, name: str | None
) -> _Split | str:
    """Accept only an exact Gemini split. Do not infer a title locally."""
    if title is None or title.strip() == "" or name is None or name.strip() == "":
        return "TITLE_SPLIT_UNRESOLVED"
    supplied = _locate_split(full, name, title)
    if not isinstance(supplied, _Split) or not supplied.title_surface:
        return "TITLE_SPLIT_UNRESOLVED"
    if not _title_split_residue_is_safe(full, supplied):
        return "TITLE_SPLIT_UNRESOLVED"
    return supplied


def _resolve_split(full: str, name: str | None, title: str | None) -> _Split | str:
    located = _locate_split(full, name, title)
    if isinstance(located, str):
        return located
    if not _residue_is_safe(
        full,
        located.name_start,
        located.name_end,
        located.title_start,
        located.title_end,
    ):
        return "UNSAFE_NAME_SPLIT"
    return located


def _locate_split(full: str, name: str | None, title: str | None) -> _Split | str:
    if name is None:
        name = full
    if title == "":
        title = None
    if name == "" or name in GENERIC_ROLE_ONLY:
        return "TITLE_OR_ROLE_WITHOUT_NAME"
    name_spans = occurrence_spans(full, name)
    if not name_spans:
        return "NAME_SURFACE_NOT_IN_FULL"
    if len(name_spans) != 1:
        return "AMBIGUOUS_NAME_SURFACE"
    name_start, name_end = name_spans[0]
    title_start = None
    title_end = None
    if title is not None:
        title_spans = occurrence_spans(full, title)
        if not title_spans:
            return "TITLE_SURFACE_NOT_IN_FULL"
        if len(title_spans) != 1:
            return "AMBIGUOUS_TITLE_SURFACE"
        title_start, title_end = title_spans[0]
        if _overlaps(name_start, name_end, title_start, title_end):
            return "TITLE_NAME_OVERLAP"
    return _Split(
        name_surface=name,
        name_start=name_start,
        name_end=name_end,
        title_surface=title,
        title_start=title_start,
        title_end=title_end,
    )


def _title_split_residue_is_safe(full: str, split: _Split) -> bool:
    """Allow punctuation only in the gap between the exact title and name."""
    if split.title_start is None or split.title_end is None:
        return False
    if split.title_end <= split.name_start:
        gap = range(split.title_end, split.name_start)
    elif split.name_end <= split.title_start:
        gap = range(split.name_end, split.title_start)
    else:
        return False
    masked = list(full)
    for index in gap:
        if _is_title_name_separator(masked[index]):
            masked[index] = " "
    return _residue_is_safe(
        "".join(masked),
        split.name_start,
        split.name_end,
        split.title_start,
        split.title_end,
    )


def _is_title_name_separator(character: str) -> bool:
    if character in " \t\r\n":
        return True
    category = unicodedata.category(character)
    return category.startswith(("P", "Z"))


def _residue_is_safe(
    full: str,
    name_start: int,
    name_end: int,
    title_start: int | None,
    title_end: int | None,
) -> bool:
    covered = [False] * len(full)
    for index in range(name_start, name_end):
        covered[index] = True
    if title_start is not None and title_end is not None:
        for index in range(title_start, title_end):
            covered[index] = True
    run_end = name_start
    run_start = name_start
    while (
        run_start > 0
        and not covered[run_start - 1]
        and full[run_start - 1] in HEBREW_PREFIX_LETTERS
    ):
        run_start -= 1
    prefix = full[run_start:run_end]
    if len(prefix) > MAX_HEBREW_PREFIX_LETTERS or not prefix_is_safe(prefix):
        return False
    if prefix and run_start > 0 and not covered[run_start - 1]:
        if unicodedata.category(full[run_start - 1])[0] in {"L", "N"}:
            return False
    for index, character in enumerate(full):
        if covered[index] or run_start <= index < run_end:
            continue
        if character in " \t\r\n" or unicodedata.category(character) == "Zs":
            continue
        return False
    return True


def _overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


def _unaligned(proposal: GeminiProposal, reason: str) -> UnalignedMention:
    return UnalignedMention(
        segment_id=proposal.segment_id,
        surface=proposal.surface,
        gemini_kind=proposal.kind,
        occurrence=proposal.occurrence,
        reason=reason,
    )
