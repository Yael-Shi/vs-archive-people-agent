"""Run one experimental mention pilot over a single displayed body."""

from __future__ import annotations

from typing import Protocol

from vs_archive_people.artifacts.io import ArtifactError, sha256_bytes
from vs_archive_people.evidence.text_bodies import (
    require_document_snapshot,
    select_text_bodies,
)
from vs_archive_people.mention_pilot.align import align_proposals
from vs_archive_people.mention_pilot.context import is_place_name_occurrence
from vs_archive_people.mention_pilot.consolidate import consolidate
from vs_archive_people.mention_pilot.gemini import MODEL_NAME, ProviderDetection
from vs_archive_people.mention_pilot.known_names import find_known_name_spans
from vs_archive_people.mention_pilot.segment import segment_body

PILOT_KIND = "GEMINI_MENTION_PILOT_NOT_ARTIFACT"


class MentionDetector(Protocol):
    def detect(self, segments) -> ProviderDetection: ...


def displayed_body_for_item(
    snapshot: dict,
    archive_item_id: int,
    evidence_source: str | None,
) -> dict:
    """Select one displayed body. Refuse when the role choice is ambiguous."""
    document = require_document_snapshot(snapshot)
    if isinstance(archive_item_id, bool) or not isinstance(archive_item_id, int):
        raise ArtifactError("archive_item_id must be an integer")
    item = next(
        (
            row
            for row in document["items"]
            if row.get("archive_item_id") == archive_item_id
        ),
        None,
    )
    if item is None:
        raise ArtifactError(f"archive_item_id {archive_item_id} is not in the snapshot")
    bodies = select_text_bodies(item)
    if evidence_source is not None:
        matches = [body for body in bodies if _role_matches(body, evidence_source)]
        if len(matches) != 1:
            raise ArtifactError(
                f"evidence source {evidence_source!r} does not select one displayed body"
            )
        selected = matches[0]
        source_label = evidence_source
    elif len(bodies) == 1:
        selected = bodies[0]
        source_label = selected["text_role"]
    else:
        raise ArtifactError(
            "the archive item has more than one displayed body; pass --evidence-source"
        )
    return {
        "text": selected["text"],
        "evidence_source": source_label,
        "text_roles": list(selected["text_roles"]),
    }


def run_mention_pilot(
    *,
    body_text: str,
    archive_item_id: int,
    evidence_source: str,
    text_roles: list[str],
    identity_snapshot: dict,
    detector: MentionDetector,
) -> dict:
    """Detect spans and verify every published offset against ``body_text``."""
    segments = segment_body(body_text)
    detection = detector.detect(segments)
    aligned, unaligned, suppressed = align_proposals(
        body_text, segments, detection.proposals
    )
    known = [
        span
        for span in find_known_name_spans(body_text, identity_snapshot, segments)
        if not is_place_name_occurrence(body_text, span.start_offset, span.end_offset)
        and not _inside_suppressed(span.start_offset, span.end_offset, suppressed)
    ]
    candidates = consolidate(body_text, aligned, known)
    for candidate in candidates:
        start = candidate["start_offset"]
        end = candidate["end_offset"]
        if body_text[start:end] != candidate["exact_mention"]:
            raise ArtifactError("pilot candidate offset does not match exact_mention")
        name_start = candidate["name_start_offset"]
        name_end = candidate["name_end_offset"]
        if body_text[name_start:name_end] != candidate["name_surface"]:
            raise ArtifactError("pilot name offset does not match name_surface")
        if not (start <= name_start <= name_end <= end):
            raise ArtifactError("pilot name slice is outside the full mention")
        title = candidate["title_surface"]
        if title is None:
            continue
        title_start = candidate["title_start_offset"]
        title_end = candidate["title_end_offset"]
        if body_text[title_start:title_end] != title:
            raise ArtifactError("pilot title offset does not match title_surface")
        if not (start <= title_start <= title_end <= end):
            raise ArtifactError("pilot title slice is outside the full mention")
    return {
        "kind": PILOT_KIND,
        "model": MODEL_NAME,
        "archive_item_id": archive_item_id,
        "evidence_source": evidence_source,
        "text_roles": text_roles,
        "body_sha256": sha256_bytes(body_text.encode("utf-8")),
        "candidates": candidates,
        "unaligned_gemini_candidates": [
            {
                "segment_id": row.segment_id,
                "surface": row.surface,
                "gemini_kind": row.gemini_kind,
                "occurrence": row.occurrence,
                "reason": row.reason,
            }
            for row in unaligned
        ],
        "provider_calls": list(detection.calls),
    }


def _inside_suppressed(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(
        range_start <= start and end <= range_end for range_start, range_end in ranges
    )


def _role_matches(body: dict, evidence_source: str) -> bool:
    return evidence_source == body["text_role"] or evidence_source in body["text_roles"]
