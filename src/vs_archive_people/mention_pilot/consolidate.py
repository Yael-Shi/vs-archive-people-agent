"""Merge detector spans that occupy the same exact source slice.

A titled Gemini mention also absorbs a span whose offsets are exactly its
name slice. Other overlapping slices stay as separate candidates.
"""

from __future__ import annotations

from vs_archive_people.mention_pilot.align import AlignedMention
from vs_archive_people.mention_pilot.errors import PilotSplitConflictError
from vs_archive_people.mention_pilot.known_names import (
    EMBEDDED,
    EXACT_BOUNDARY,
    PREFIXED,
    KnownSpan,
)

DETECTOR_ORDER = ("GEMINI", "KNOWN_NAME")
# Prefer the more cautious label if two known spans share one slice.
_QUALITY_RANK = {EMBEDDED: 2, PREFIXED: 1, EXACT_BOUNDARY: 0}


def consolidate(
    body_text: str,
    gemini_mentions: list[AlignedMention],
    known_spans: list[KnownSpan],
) -> list[dict]:
    grouped: dict[tuple[int, int], dict] = {}
    for mention in gemini_mentions:
        row = _row(
            grouped,
            body_text,
            mention.start_offset,
            mention.end_offset,
            mention.segment_id,
        )
        row["detectors"].add("GEMINI")
        row["gemini_kinds"].add(mention.gemini_kind)
        _apply_split(row, mention)
    for span in known_spans:
        row = _row(
            grouped, body_text, span.start_offset, span.end_offset, span.segment_id
        )
        row["detectors"].add("KNOWN_NAME")
        row["known_qualities"].add(span.match_quality)
    _absorb_exact_titled_cores(grouped)
    candidates: list[dict] = []
    for start, end in sorted(grouped):
        row = grouped[(start, end)]
        detectors = [name for name in DETECTOR_ORDER if name in row["detectors"]]
        kinds = {kind for kind in row["gemini_kinds"] if kind is not None}
        gemini_kind = next(iter(kinds)) if len(kinds) == 1 else None
        exact = body_text[start:end]
        if exact != row["exact_mention"]:
            raise RuntimeError("consolidated span does not match the source body")
        (
            name_surface,
            name_start,
            name_end,
            title_surface,
            title_start,
            title_end,
            end_segment_id,
        ) = _split_fields(row, start, end, exact)
        if body_text[name_start:name_end] != name_surface:
            raise RuntimeError("consolidated name slice does not match the source body")
        if title_surface is None:
            if title_start is not None or title_end is not None:
                raise RuntimeError("a missing title still has offsets")
        elif body_text[title_start:title_end] != title_surface:
            raise RuntimeError(
                "consolidated title slice does not match the source body"
            )
        candidates.append(
            {
                "segment_id": row["segment_id"],
                "end_segment_id": end_segment_id,
                "exact_mention": exact,
                "start_offset": start,
                "end_offset": end,
                "name_surface": name_surface,
                "name_start_offset": name_start,
                "name_end_offset": name_end,
                "title_surface": title_surface,
                "title_start_offset": title_start,
                "title_end_offset": title_end,
                "detectors": detectors,
                "comparison": _comparison(detectors),
                "gemini_kind": gemini_kind,
                "known_name_match_quality": _known_quality(
                    row["known_qualities"], detectors
                ),
            }
        )
    return candidates


def _row(
    grouped: dict[tuple[int, int], dict],
    body_text: str,
    start: int,
    end: int,
    segment_id: str | None,
) -> dict:
    key = (start, end)
    exact = body_text[start:end]
    existing = grouped.get(key)
    if existing is None:
        existing = {
            "exact_mention": exact,
            "segment_id": segment_id,
            "detectors": set(),
            "gemini_kinds": set(),
            "known_qualities": set(),
            "split": None,
        }
        grouped[key] = existing
        return existing
    if existing["exact_mention"] != exact:
        raise RuntimeError("the same offsets produced two source slices")
    if existing["segment_id"] is None:
        existing["segment_id"] = segment_id
    return existing


def _absorb_exact_titled_cores(grouped: dict[tuple[int, int], dict]) -> None:
    """Fold an exact core-name span into the one titled mention that owns it."""
    claims: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for key, row in grouped.items():
        name_key = _titled_name_key(row)
        if name_key is None or name_key == key:
            continue
        claims.setdefault(name_key, []).append(key)
    for name_key, outer_keys in claims.items():
        if len(outer_keys) != 1:
            continue
        inner = grouped.get(name_key)
        if inner is None or _row_has_title(inner):
            continue
        outer = grouped[outer_keys[0]]
        outer["detectors"].update(inner["detectors"])
        outer["known_qualities"].update(inner["known_qualities"])
        del grouped[name_key]


def _titled_name_key(row: dict) -> tuple[int, int] | None:
    split = row["split"]
    if not isinstance(split, tuple) or split[4] is None:
        return None
    return split[2], split[3]


def _row_has_title(row: dict) -> bool:
    return _titled_name_key(row) is not None


def _apply_split(row: dict, mention: AlignedMention) -> None:
    split = (
        mention.end_segment_id,
        mention.name_surface,
        mention.name_start_offset,
        mention.name_end_offset,
        mention.title_surface,
        mention.title_start_offset,
        mention.title_end_offset,
    )
    current = row["split"]
    if current is None or current == split:
        row["split"] = split
        return
    raise PilotSplitConflictError(
        "the same source span has two different exact splits and was not published"
    )


def _split_fields(
    row: dict, start: int, end: int, exact: str
) -> tuple[str, int, int, str | None, int | None, int | None, str | None]:
    split = row["split"]
    if split is None:
        return exact, start, end, None, None, None, None
    end_segment_id, name, name_start, name_end, title, title_start, title_end = split
    return name, name_start, name_end, title, title_start, title_end, end_segment_id


def _known_quality(qualities: set[str], detectors: list[str]) -> str | None:
    if "KNOWN_NAME" not in detectors:
        return None
    if not qualities:
        raise RuntimeError("a known-name span is missing match quality")
    return max(qualities, key=lambda quality: _QUALITY_RANK[quality])


def _comparison(detectors: list[str]) -> str:
    if detectors == ["GEMINI", "KNOWN_NAME"]:
        return "BOTH"
    if detectors == ["GEMINI"]:
        return "GEMINI"
    if detectors == ["KNOWN_NAME"]:
        return "KNOWN_NAME"
    raise RuntimeError(f"unexpected detector set: {detectors}")
