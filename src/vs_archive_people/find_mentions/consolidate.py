"""Consolidate detector spans for one displayed body.

Same outer offsets become one mention. ``detector_ids`` is the union. Two
different exact splits of that span publish neither representation. Exactly
one titled outer absorbs an inner span at its name offsets. Several titled
outers keep every valid span.
"""

from __future__ import annotations

from vs_archive_people.find_mentions.align import AlignedSurface, DiagnosticNote
from vs_archive_people.find_mentions.known_names import PreparedMention
from vs_archive_people.find_mentions.registry import (
    KNOWN_NAME_DETECTOR_ID,
    SEMANTIC_DETECTOR_ID,
    canonical_detector_ids,
)
from vs_archive_people.find_mentions.known_names import EXACT_BOUNDARY, PREFIXED

CONFLICTS = "conflicts"
_QUALITY_RANK = {PREFIXED: 1, EXACT_BOUNDARY: 0}


def from_semantic(surface: AlignedSurface) -> PreparedMention:
    return PreparedMention(
        segment_id=surface.segment_id,
        exact_mention=surface.exact_mention,
        start_offset=surface.start_offset,
        end_offset=surface.end_offset,
        name_surface=surface.name_surface,
        name_start_offset=surface.name_start_offset,
        name_end_offset=surface.name_end_offset,
        title_surface=surface.title_surface,
        title_start_offset=surface.title_start_offset,
        title_end_offset=surface.title_end_offset,
        surface_kind=surface.surface_kind,
        detector_id=SEMANTIC_DETECTOR_ID,
        known_name_match_quality=None,
    )


def consolidate(
    body_text: str,
    mentions: list[PreparedMention],
) -> tuple[list[PreparedMention], list[DiagnosticNote]]:
    """Return published spans and conflict notes. Overlaps that are not exact stay."""
    grouped: dict[tuple[int, int], dict] = {}
    conflicts: list[DiagnosticNote] = []
    for mention in mentions:
        key = (mention.start_offset, mention.end_offset)
        row = grouped.get(key)
        if row is None:
            grouped[key] = _new_row(mention)
            continue
        if row["conflict"]:
            continue
        if _split_key(mention) != row["split"]:
            row["conflict"] = True
            conflicts.append(
                DiagnosticNote(
                    bucket=CONFLICTS,
                    surface=row["exact_mention"],
                    reason="SPLIT_CONFLICT",
                    segment_id=row["segment_id"],
                    start_offset=key[0],
                    end_offset=key[1],
                )
            )
            continue
        _union(row, mention)
    _absorb(grouped, conflicts)
    published: list[PreparedMention] = []
    for key in sorted(grouped):
        row = grouped[key]
        if row["conflict"] or row["absorbed"]:
            continue
        start, end = key
        exact = body_text[start:end]
        if exact != row["exact_mention"]:
            raise RuntimeError("consolidated span does not match the source body")
        published.append(_to_mention(row, start, end, exact))
    return published, conflicts


def _new_row(mention: PreparedMention) -> dict:
    return {
        "exact_mention": mention.exact_mention,
        "segment_id": mention.segment_id,
        "split": _split_key(mention),
        "name_surface": mention.name_surface,
        "title_surface": mention.title_surface,
        "detectors": {mention.detector_id},
        "kinds": {mention.surface_kind}
        if mention.detector_id == SEMANTIC_DETECTOR_ID
        else set(),
        "qualities": _qualities(mention),
        "conflict": False,
        "absorbed": False,
    }


def _union(row: dict, mention: PreparedMention) -> None:
    row["detectors"].add(mention.detector_id)
    if mention.detector_id == SEMANTIC_DETECTOR_ID:
        row["kinds"].add(mention.surface_kind)
    row["qualities"].update(_qualities(mention))
    if row["segment_id"] is None:
        row["segment_id"] = mention.segment_id


def _absorb(
    grouped: dict[tuple[int, int], dict], conflicts: list[DiagnosticNote]
) -> None:
    claims: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for key, row in grouped.items():
        if row["conflict"]:
            continue
        name_key = _titled_name_key(row, key)
        if name_key is None:
            continue
        claims.setdefault(name_key, []).append(key)
    for name_key, outer_keys in claims.items():
        inner = grouped.get(name_key)
        if inner is None or inner["conflict"] or inner["absorbed"]:
            continue
        distinct = [key for key in outer_keys if key != name_key]
        if len(distinct) == 1 and not _has_title(inner):
            outer = grouped[distinct[0]]
            outer["detectors"].update(inner["detectors"])
            outer["qualities"].update(inner["qualities"])
            inner["absorbed"] = True
            continue
        if len(distinct) > 1:
            conflicts.append(
                DiagnosticNote(
                    bucket=CONFLICTS,
                    surface=inner["exact_mention"],
                    reason="ABSORB_AMBIGUOUS",
                    segment_id=inner["segment_id"],
                    start_offset=name_key[0],
                    end_offset=name_key[1],
                )
            )


def _titled_name_key(row: dict, key: tuple[int, int]) -> tuple[int, int] | None:
    name_start, name_end, title_start, _title_end = row["split"]
    if title_start is None:
        return None
    name_key = (name_start, name_end)
    if name_key == key:
        return None
    return name_key


def _has_title(row: dict) -> bool:
    return row["split"][2] is not None


def _split_key(
    mention: PreparedMention,
) -> tuple[int, int, int | None, int | None]:
    return (
        mention.name_start_offset,
        mention.name_end_offset,
        mention.title_start_offset,
        mention.title_end_offset,
    )


def _qualities(mention: PreparedMention) -> set[str]:
    if mention.known_name_match_quality is None:
        return set()
    return {mention.known_name_match_quality}


def _surface_kind(kinds: set[str | None]) -> str | None:
    labels = {kind for kind in kinds if kind is not None}
    if len(labels) == 1:
        return next(iter(labels))
    return None


def _quality(qualities: set[str], detectors: set[str]) -> str | None:
    if KNOWN_NAME_DETECTOR_ID not in detectors:
        return None
    if not qualities:
        raise RuntimeError("a known-name span is missing match quality")
    return max(qualities, key=lambda quality: _QUALITY_RANK[quality])


def _to_mention(row: dict, start: int, end: int, exact: str) -> PreparedMention:
    name_start, name_end, title_start, title_end = row["split"]
    title = row["title_surface"]
    return PreparedMention(
        segment_id=row["segment_id"],
        exact_mention=exact,
        start_offset=start,
        end_offset=end,
        name_surface=row["name_surface"],
        name_start_offset=name_start,
        name_end_offset=name_end,
        title_surface=title,
        title_start_offset=title_start,
        title_end_offset=title_end,
        surface_kind=_surface_kind(row["kinds"]),
        detector_id=canonical_detector_ids(row["detectors"])[0],
        known_name_match_quality=_quality(row["qualities"], row["detectors"]),
        detector_ids=tuple(canonical_detector_ids(row["detectors"])),
    )
