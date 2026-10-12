"""Source slices for a mention proposal.

Offsets are zero-based Python code points into the unmodified displayed body.
This module does not normalize, casefold, or rewrite that body before slicing.
Title text is free text. There is no title vocabulary.
"""

from __future__ import annotations

import unicodedata

from vs_archive_people.artifacts.io import ArtifactError

HEBREW_PREFIX_LETTERS = frozenset("הובכלמש")
MAX_HEBREW_PREFIX_LETTERS = 4
_UNSAFE_PREFIX_ONLY = frozenset("הש")
SURFACE_KINDS = frozenset(
    {
        "FULL_NAME",
        "PARTIAL_NAME",
        "TITLE_AND_NAME",
        "NICKNAME",
        "UNCERTAIN_PERSON_NAME",
    }
)


def require_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactError(f"{label} must be an integer")
    if value < 0:
        raise ArtifactError(f"{label} must be zero or greater")
    return value


def require_at_most_one_line_break(exact_mention: str) -> None:
    """Allow a same-line span or one span across a single adjacent break.

    ``\\r\\n`` counts as that one break because it contains one ``\\n``. The
    source characters are not rewritten. Two ``\\n`` characters, including a
    blank line, are rejected.
    """
    if exact_mention.count("\n") > 1:
        raise ArtifactError("outer mention may cross at most one line break")


def slice_span(body: str, start: int, end: int, label: str) -> str:
    """Return ``body[start:end]`` when the span is inside the body and non-empty."""
    if not 0 <= start < end <= len(body):
        raise ArtifactError(f"{label} offsets are outside the displayed body")
    return body[start:end]


def evidence_line_snippet(body: str, start: int, end: int) -> str:
    """Exact source lines that cover ``[start, end)``.

    A span on one line excludes the line terminator. A span that crosses a
    line break includes that break, including ``\\r\\n``, and excludes a
    terminator after the last covered line.
    """
    line_start = body.rfind("\n", 0, start) + 1
    newline = body.find("\n", end)
    if newline < 0:
        line_end = len(body)
    elif newline > 0 and body[newline - 1] == "\r" and newline - 1 >= end:
        line_end = newline - 1
    else:
        line_end = newline
    return body[line_start:line_end]


def require_name_inside_outer(
    outer_start: int, outer_end: int, name_start: int, name_end: int
) -> None:
    if not outer_start <= name_start < name_end <= outer_end:
        raise ArtifactError("name span is outside the outer mention")


def require_title_pair(
    title_start: int | None, title_end: int | None
) -> tuple[int, int] | None:
    if (title_start is None) != (title_end is None):
        raise ArtifactError("title offsets must both be null or both be integers")
    if title_start is None or title_end is None:
        return None
    return title_start, title_end


def require_title_inside_outer(
    outer_start: int,
    outer_end: int,
    name_start: int,
    name_end: int,
    title_start: int,
    title_end: int,
) -> None:
    if not outer_start <= title_start < title_end <= outer_end:
        raise ArtifactError("title span is outside the outer mention")
    if title_start < name_end and name_start < title_end:
        raise ArtifactError("title span overlaps the name span")


def require_split_residue(
    outer: str,
    name_start: int,
    name_end: int,
    title_start: int | None,
    title_end: int | None,
) -> None:
    """Apply the title and name residue rules to offsets inside ``outer``.

    Whitespace, the original line break, and a safe Hebrew proclitic may sit
    outside the slices. Unicode punctuation and separators may sit between a
    title slice and a name slice. Letters or digits in that gap are rejected.
    """
    if title_start is None or title_end is None:
        if not _residue_is_safe(outer, name_start, name_end, None, None):
            raise ArtifactError("name split leaves unsafe residue")
        return
    if not _title_split_residue_is_safe(
        outer, name_start, name_end, title_start, title_end
    ):
        raise ArtifactError("title and name leave unsafe residue")


def _prefix_is_safe(prefix: str) -> bool:
    """A peel of only ``ה`` or ``ש`` is not a safe name split."""
    if prefix == "":
        return True
    if len(prefix) > MAX_HEBREW_PREFIX_LETTERS:
        return False
    if any(character not in HEBREW_PREFIX_LETTERS for character in prefix):
        return False
    return any(character not in _UNSAFE_PREFIX_ONLY for character in prefix)


def _is_title_name_separator(character: str) -> bool:
    if character in " \t\r\n":
        return True
    category = unicodedata.category(character)
    return category.startswith(("P", "Z"))


def _title_split_residue_is_safe(
    outer: str,
    name_start: int,
    name_end: int,
    title_start: int,
    title_end: int,
) -> bool:
    if title_end <= name_start:
        gap = range(title_end, name_start)
    elif name_end <= title_start:
        gap = range(name_end, title_start)
    else:
        return False
    masked = list(outer)
    for index in gap:
        if _is_title_name_separator(masked[index]):
            masked[index] = " "
    return _residue_is_safe(
        "".join(masked), name_start, name_end, title_start, title_end
    )


def _residue_is_safe(
    outer: str,
    name_start: int,
    name_end: int,
    title_start: int | None,
    title_end: int | None,
) -> bool:
    covered = [False] * len(outer)
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
        and outer[run_start - 1] in HEBREW_PREFIX_LETTERS
    ):
        run_start -= 1
    prefix = outer[run_start:run_end]
    if len(prefix) > MAX_HEBREW_PREFIX_LETTERS or not _prefix_is_safe(prefix):
        return False
    if prefix and run_start > 0 and not covered[run_start - 1]:
        if unicodedata.category(outer[run_start - 1])[0] in {"L", "N"}:
            return False
    for index, character in enumerate(outer):
        if covered[index] or run_start <= index < run_end:
            continue
        if character in " \t\r\n" or unicodedata.category(character) == "Zs":
            continue
        return False
    return True
