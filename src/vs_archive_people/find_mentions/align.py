"""Map semantic proposals onto exact source offsets.

A surface may occur more than once in its window. Occurrences are the
non-overlapping left-to-right matches of that exact string. When more than one
exists, an occurrence hint (1-based) is required. This module does not choose
the first match.

A proposal may name the next segment when ``break_after`` is a single ``\\n``.
The span is then a slice of the original body, including that break. Title and
name surfaces are accepted only as unique exact substrings of that slice.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from vs_archive_people.find_mentions.context import (
    GENERIC_ROLE_ONLY,
    HEBREW_PREFIX_LETTERS,
    MAX_HEBREW_PREFIX_LETTERS,
    is_place_name_occurrence,
    prefix_is_safe,
)
from vs_archive_people.find_mentions.proposals import SemanticProposal
from vs_archive_people.find_mentions.segment import Segment
from vs_archive_people.find_mentions.spans import occurrence_spans

REJECTED = "rejected"
SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class AlignedSurface:
    segment_id: str
    exact_mention: str
    start_offset: int
    end_offset: int
    name_surface: str
    name_start_offset: int
    name_end_offset: int
    title_surface: str | None
    title_start_offset: int | None
    title_end_offset: int | None
    surface_kind: str | None


@dataclass(frozen=True)
class DiagnosticNote:
    bucket: str
    surface: str
    reason: str
    segment_id: str | None
    start_offset: int | None = None
    end_offset: int | None = None


@dataclass(frozen=True)
class _Split:
    name_surface: str
    name_start: int
    name_end: int
    title_surface: str | None
    title_start: int | None
    title_end: int | None


def align_proposals(
    body_text: str,
    segments: list[Segment],
    proposals: list[SemanticProposal],
) -> tuple[list[AlignedSurface], list[DiagnosticNote], list[tuple[int, int]]]:
    """Return aligned surfaces, diagnostic notes, and non-person ranges.

    The third list holds source ranges that suppress contained known names.
    """
    by_id = {segment.segment_id: segment for segment in segments}
    ordered = list(segments)
    aligned: list[AlignedSurface] = []
    notes: list[DiagnosticNote] = []
    suppressed: list[tuple[int, int]] = []
    for proposal in proposals:
        segment = by_id.get(proposal.segment_id)
        if segment is None:
            notes.append(_note(proposal, REJECTED, "UNKNOWN_SEGMENT"))
            continue
        if proposal.surface == "":
            notes.append(_note(proposal, REJECTED, "EMPTY_SURFACE"))
            continue
        window = _window(ordered, segment, proposal.end_segment_id)
        if isinstance(window, str):
            notes.append(_note(proposal, REJECTED, window))
            continue
        window_start, window_end = window
        window_text = body_text[window_start:window_end]
        spans = occurrence_spans(window_text, proposal.surface)
        if not spans:
            notes.append(_note(proposal, REJECTED, "SURFACE_NOT_IN_SEGMENT"))
            continue
        if len(spans) > 1 and proposal.occurrence is None:
            notes.append(_note(proposal, REJECTED, "AMBIGUOUS_REPEATED_SURFACE"))
            continue
        if proposal.occurrence is None:
            local_start, local_end = spans[0]
        elif proposal.occurrence < 1 or proposal.occurrence > len(spans):
            notes.append(_note(proposal, REJECTED, "OCCURRENCE_OUT_OF_RANGE"))
            continue
        else:
            local_start, local_end = spans[proposal.occurrence - 1]
        start = window_start + local_start
        end = window_start + local_end
        exact = body_text[start:end]
        if exact != proposal.surface or exact != window_text[local_start:local_end]:
            notes.append(_note(proposal, REJECTED, "OFFSET_INVARIANT_FAILED"))
            continue
        if proposal.referent == "NON_PERSON" or is_place_name_occurrence(
            body_text, start, end
        ):
            suppressed.append((start, end))
            notes.append(
                _note(
                    proposal,
                    SUPPRESSED,
                    "PLACE_OR_INSTITUTION_CONTEXT",
                    start,
                    end,
                )
            )
            continue
        split = _split_for_proposal(exact, proposal)
        if isinstance(split, str):
            bucket = SUPPRESSED if split == "TITLE_OR_ROLE_WITHOUT_NAME" else REJECTED
            notes.append(_note(proposal, bucket, split, start, end))
            continue
        name_start = start + split.name_start
        name_end = start + split.name_end
        if is_place_name_occurrence(body_text, name_start, name_end):
            suppressed.append((start, end))
            notes.append(
                _note(
                    proposal,
                    SUPPRESSED,
                    "PLACE_OR_INSTITUTION_CONTEXT",
                    start,
                    end,
                )
            )
            continue
        title_start = None if split.title_start is None else start + split.title_start
        title_end = None if split.title_end is None else start + split.title_end
        aligned.append(
            AlignedSurface(
                segment_id=segment.segment_id,
                exact_mention=exact,
                start_offset=start,
                end_offset=end,
                name_surface=split.name_surface,
                name_start_offset=name_start,
                name_end_offset=name_end,
                title_surface=split.title_surface,
                title_start_offset=title_start,
                title_end_offset=title_end,
                surface_kind=proposal.kind,
            )
        )
    return aligned, notes, suppressed


def _window(
    segments: list[Segment], segment: Segment, end_segment_id: str | None
) -> tuple[int, int] | str:
    if end_segment_id is None:
        return segment.start_offset, segment.end_offset
    index = next(
        (position for position, item in enumerate(segments) if item is segment),
        None,
    )
    if index is None or index + 1 >= len(segments):
        return "END_SEGMENT_NOT_NEXT_LINE"
    following = segments[index + 1]
    if following.segment_id != end_segment_id or segment.break_after != "\n":
        return "END_SEGMENT_NOT_NEXT_LINE"
    return segment.start_offset, following.end_offset


def _split_for_proposal(full: str, proposal: SemanticProposal) -> _Split | str:
    if proposal.kind == "TITLE_AND_NAME":
        return _required_title_split(
            full, proposal.title_surface, proposal.name_surface
        )
    # An omitted name slice means the full surface is the name. A supplied
    # slice is accepted only when it is a unique exact substring with safe
    # residue. An invalid supplied slice is not rewritten as the full name.
    return _resolve_split(full, proposal.name_surface, proposal.title_surface)


def _required_title_split(
    full: str, title: str | None, name: str | None
) -> _Split | str:
    """Accept only an exact detector split. Do not infer a title locally."""
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


def _note(
    proposal: SemanticProposal,
    bucket: str,
    reason: str,
    start: int | None = None,
    end: int | None = None,
) -> DiagnosticNote:
    return DiagnosticNote(
        bucket=bucket,
        surface=proposal.surface,
        reason=reason,
        segment_id=proposal.segment_id,
        start_offset=start,
        end_offset=end,
    )
