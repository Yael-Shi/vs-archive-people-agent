"""Cross-field checks for mention-proposals-v1.

JSON Schema rejects undeclared fields and closed enums. These checks cover
references and slice shape that a schema document does not express. They do
not read a displayed body and they do not call a provider.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import ArtifactError
from vs_archive_people.mention_proposals.geometry import (
    require_at_most_one_line_break,
    require_name_inside_outer,
    require_split_residue,
    require_title_inside_outer,
    require_title_pair,
)

KNOWN_NAME_DETECTOR_ID = "known-name"
_KINDS_WITHOUT_TITLE = frozenset(
    {
        "FULL_NAME",
        "PARTIAL_NAME",
        "NICKNAME",
        "UNCERTAIN_PERSON_NAME",
    }
)


def outer_span_key(mention: dict) -> tuple[int, str, int, int]:
    return (
        mention["archive_item_id"],
        mention["evidence_source"],
        mention["start_offset"],
        mention["end_offset"],
    )


def mentions_in_source_order(mentions: list[dict]) -> list[dict]:
    """Order rows by item, evidence source, and outer offsets.

    Equal outer spans are invalid. This sort does not change the global
    logical-hash rule; producers apply it before stamping.
    """
    return sorted(mentions, key=outer_span_key)


def require_unique_outer_spans(mentions: list[dict]) -> None:
    seen: set[tuple[int, str, int, int]] = set()
    for mention in mentions:
        key = outer_span_key(mention)
        if key in seen:
            raise ArtifactError(
                "duplicate outer span for one archive item and evidence source"
            )
        seen.add(key)


def require_mention_proposal_constraints(document: dict) -> None:
    detectors = document["detectors"]
    seen: list[str] = []
    for detector in detectors:
        detector_id = detector["id"]
        if detector_id in seen:
            raise ArtifactError(f"duplicate detector id {detector_id!r}")
        seen.append(detector_id)
    declared = set(seen)
    mentions = document["mentions"]
    for mention in mentions:
        _require_mention(mention, declared)
    require_unique_outer_spans(mentions)


def _require_mention(mention: dict, declared: set[str]) -> None:
    for detector_id in mention["detector_ids"]:
        if detector_id not in declared:
            raise ArtifactError(
                f"detector id {detector_id!r} is not declared in detectors"
            )
    quality = mention["known_name_match_quality"]
    contributed = KNOWN_NAME_DETECTOR_ID in mention["detector_ids"]
    if contributed and quality is None:
        raise ArtifactError(
            "known_name_match_quality is required when known-name contributed"
        )
    if quality is not None and not contributed:
        raise ArtifactError(
            "known_name_match_quality is allowed only when known-name contributed"
        )
    start = mention["start_offset"]
    end = mention["end_offset"]
    name_start = mention["name_start_offset"]
    name_end = mention["name_end_offset"]
    if not start < end:
        raise ArtifactError("outer span must be non-empty")
    require_name_inside_outer(start, end, name_start, name_end)
    title = require_title_pair(
        mention["title_start_offset"], mention["title_end_offset"]
    )
    outer = mention["exact_mention"]
    if len(outer) != end - start:
        raise ArtifactError("exact_mention length does not match the outer offsets")
    relative_name_start = name_start - start
    relative_name_end = name_end - start
    if outer[relative_name_start:relative_name_end] != mention["name_surface"]:
        raise ArtifactError(
            "name_surface does not match exact_mention at the name offsets"
        )
    relative_title_start = None
    relative_title_end = None
    if title is None:
        if mention["title_surface"] is not None:
            raise ArtifactError(
                "title_surface must be null when title offsets are null"
            )
    else:
        title_start, title_end = title
        require_title_inside_outer(
            start, end, name_start, name_end, title_start, title_end
        )
        if not isinstance(mention["title_surface"], str):
            raise ArtifactError(
                "title_surface must be a non-empty string when title offsets are set"
            )
        relative_title_start = title_start - start
        relative_title_end = title_end - start
        if outer[relative_title_start:relative_title_end] != mention["title_surface"]:
            raise ArtifactError(
                "title_surface does not match exact_mention at the title offsets"
            )
    require_at_most_one_line_break(outer)
    _require_surface_kind(mention.get("surface_kind"), title is not None)
    require_split_residue(
        outer,
        relative_name_start,
        relative_name_end,
        relative_title_start,
        relative_title_end,
    )


def _require_surface_kind(surface_kind: object, title_present: bool) -> None:
    if surface_kind == "TITLE_AND_NAME" and not title_present:
        raise ArtifactError("TITLE_AND_NAME requires a title slice")
    if title_present and surface_kind in _KINDS_WITHOUT_TITLE:
        raise ArtifactError(f"surface_kind {surface_kind} contradicts a title slice")
