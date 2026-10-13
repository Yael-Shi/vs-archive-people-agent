"""Line-oriented segmentation that preserves exact source offsets.

Segments are slices of the original body. The body is not normalized, and
newline code points are not members of segment text. ``break_after`` is the
exact gap before the next segment, usually ``\\n``. Alignment may join two
segments only across that single line break and must keep the break in the
source span. A line longer than ``MAX_SEGMENT_CODE_POINTS`` is split on a
space when one sits in the latter half of the window; otherwise it is split
on the code-point boundary. Either split is an exact source boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_SEGMENT_CODE_POINTS = 800


@dataclass(frozen=True)
class Segment:
    segment_id: str
    text: str
    start_offset: int
    end_offset: int
    break_after: str = ""


def segment_body(body_text: str) -> list[Segment]:
    """Return ordered segments ``S0001``, ``S0002``, ... for one displayed body."""
    if not isinstance(body_text, str):
        raise TypeError("body_text must be a str")
    pieces: list[tuple[int, int]] = []
    line_start = 0
    for index, character in enumerate(body_text):
        if character != "\n":
            continue
        pieces.extend(_split_span(body_text, line_start, index))
        line_start = index + 1
    pieces.extend(_split_span(body_text, line_start, len(body_text)))
    segments: list[Segment] = []
    for number, (start, end) in enumerate(pieces, start=1):
        following = pieces[number] if number < len(pieces) else None
        break_after = "" if following is None else body_text[end : following[0]]
        segments.append(
            Segment(
                segment_id=f"S{number:04d}",
                text=body_text[start:end],
                start_offset=start,
                end_offset=end,
                break_after=break_after,
            )
        )
    return segments


def _split_span(body_text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + MAX_SEGMENT_CODE_POINTS, end)
        split_at = window_end
        if window_end < end:
            space_at = body_text.rfind(" ", cursor, window_end)
            midpoint = cursor + (window_end - cursor) // 2
            if space_at >= midpoint:
                split_at = space_at
        if split_at <= cursor:
            split_at = window_end
        spans.append((cursor, split_at))
        cursor = split_at
    return spans
