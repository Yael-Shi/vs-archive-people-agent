"""Build and publish mention-proposals-v1 for one discovery selection.

Detection finishes for every selected body before any file is published. A
provider or input failure before that publication leaves no proposal file.
The diagnostic sidecar is written only after the proposal link succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vs_archive_people.artifacts.io import (
    DISCOVERY_SELECTION_SCHEMA,
    MENTION_PROPOSAL_DIAGNOSTICS_SCHEMA,
    MENTION_PROPOSALS_SCHEMA,
    ArtifactError,
    RunIdMismatchError,
    require_object,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import (
    stamp_generated_artifact,
    verify_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import validate_artifact
from vs_archive_people.evidence.text_bodies import (
    displayed_text_sha256,
    require_document_snapshot,
    select_text_bodies,
)
from vs_archive_people.find_mentions.align import align_proposals
from vs_archive_people.find_mentions.consolidate import consolidate, from_semantic
from vs_archive_people.find_mentions.errors import DiagnosticsPublicationFailed
from vs_archive_people.find_mentions.gemini import prompt_sha256
from vs_archive_people.find_mentions.known_names import (
    known_name_mentions,
    suppress_contained,
)
from vs_archive_people.find_mentions.proposals import SemanticDetector
from vs_archive_people.find_mentions.registry import official_detectors
from vs_archive_people.find_mentions.segment import segment_body
from vs_archive_people.mention_proposals.constraints import (
    mentions_in_source_order,
    require_unique_outer_spans,
)
from vs_archive_people.mention_proposals.geometry import (
    evidence_line_snippet,
    require_at_most_one_line_break,
    require_int,
)
from vs_archive_people.resolution.snapshot_index import build_index


@dataclass(frozen=True)
class PublishedMentions:
    proposal_sha256: str
    proposal_logical_sha256: str
    diagnostics_sha256: str
    diagnostics_logical_sha256: str
    mention_count: int


def build_mention_findings(
    snapshot: dict,
    discovery: dict,
    identity_snapshot: dict,
    *,
    run_id: str,
    document_snapshot_file_sha256: str,
    discovery_file_sha256: str,
    identity_snapshot_file_sha256: str,
    detector: SemanticDetector,
    created_at: str | None = None,
) -> tuple[dict, dict]:
    """Detect the whole selection and return a stamped proposal plus diagnostic rows.

    The diagnostic rows are not yet a sidecar. They have no proposal file hash.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ArtifactError("run_id is required")
    document = require_document_snapshot(snapshot)
    selection = _require_discovery(
        discovery,
        run_id=run_id,
        document_snapshot_file_sha256=document_snapshot_file_sha256,
    )
    exact_names = list(build_index(identity_snapshot).exact_names)
    items_by_id = {item["archive_item_id"]: item for item in document["items"]}
    mentions: list[dict] = []
    notes: list[dict] = []
    for item_id, evidence_source, text in _selected_bodies(selection, items_by_id):
        body_mentions, body_notes = _detect_body(
            text,
            exact_names,
            detector,
            archive_item_id=item_id,
            evidence_source=evidence_source,
        )
        mentions.extend(body_mentions)
        notes.extend(body_notes)
    ordered = mentions_in_source_order(mentions)
    require_unique_outer_spans(ordered)
    proposal = {
        "schema_version": MENTION_PROPOSALS_SCHEMA,
        "run_id": run_id,
        "document_snapshot_file_sha256": document_snapshot_file_sha256,
        "discovery_logical_sha256": selection["logical_content_sha256"],
        "discovery_file_sha256": discovery_file_sha256,
        "identity_snapshot_file_sha256": identity_snapshot_file_sha256,
        "production_write_performed": False,
        "detectors": official_detectors(prompt_sha256()),
        "mentions": ordered,
    }
    stamp_generated_artifact(proposal, created_at=created_at)
    validate_artifact(proposal)
    return proposal, _grouped_notes(notes)


def publish_mention_findings(
    snapshot: dict,
    discovery: dict,
    identity_snapshot: dict,
    *,
    run_id: str,
    document_snapshot_file_sha256: str,
    discovery_file_sha256: str,
    identity_snapshot_file_sha256: str,
    detector: SemanticDetector,
    proposal_output: Path,
    diagnostics_output: Path,
    created_at: str | None = None,
) -> PublishedMentions:
    """Publish the proposal, then try to publish the diagnostic sidecar.

    A failure before the proposal link publishes nothing. A sidecar failure
    leaves the proposal in place.
    """
    proposal, grouped = build_mention_findings(
        snapshot,
        discovery,
        identity_snapshot,
        run_id=run_id,
        document_snapshot_file_sha256=document_snapshot_file_sha256,
        discovery_file_sha256=discovery_file_sha256,
        identity_snapshot_file_sha256=identity_snapshot_file_sha256,
        detector=detector,
        created_at=created_at,
    )
    proposal_sha256 = write_json_atomic(proposal_output, proposal)
    try:
        diagnostics = diagnostics_artifact(
            run_id=run_id,
            mention_proposals_logical_sha256=proposal["logical_content_sha256"],
            mention_proposals_file_sha256=proposal_sha256,
            rejected=grouped["rejected"],
            suppressed=grouped["suppressed"],
            conflicts=grouped["conflicts"],
            created_at=created_at,
        )
        diagnostics_sha256 = write_json_atomic(diagnostics_output, diagnostics)
    except ArtifactError as exc:
        raise DiagnosticsPublicationFailed(
            str(exc),
            proposal_output=str(proposal_output),
            proposal_sha256=proposal_sha256,
            proposal_logical_sha256=proposal["logical_content_sha256"],
        ) from None
    return PublishedMentions(
        proposal_sha256=proposal_sha256,
        proposal_logical_sha256=proposal["logical_content_sha256"],
        diagnostics_sha256=diagnostics_sha256,
        diagnostics_logical_sha256=diagnostics["logical_content_sha256"],
        mention_count=len(proposal["mentions"]),
    )


def diagnostics_artifact(
    *,
    run_id: str,
    mention_proposals_logical_sha256: str,
    mention_proposals_file_sha256: str,
    rejected: list[dict],
    suppressed: list[dict],
    conflicts: list[dict],
    created_at: str | None = None,
) -> dict:
    document = {
        "schema_version": MENTION_PROPOSAL_DIAGNOSTICS_SCHEMA,
        "run_id": run_id,
        "mention_proposals_logical_sha256": mention_proposals_logical_sha256,
        "mention_proposals_file_sha256": mention_proposals_file_sha256,
        "rejected": rejected,
        "suppressed": suppressed,
        "conflicts": conflicts,
    }
    stamp_generated_artifact(document, created_at=created_at)
    validate_artifact(document)
    return document


def _detect_body(
    text: str,
    exact_names: list[str],
    detector: SemanticDetector,
    *,
    archive_item_id: int,
    evidence_source: str,
) -> tuple[list[dict], list[dict]]:
    segments = segment_body(text)
    proposals = detector.detect(segments)
    aligned, semantic_notes, suppressed_ranges = align_proposals(
        text, segments, proposals
    )
    known, known_notes = known_name_mentions(text, exact_names, segments)
    known, contained_notes = suppress_contained(known, suppressed_ranges)
    prepared = [from_semantic(surface) for surface in aligned]
    prepared.extend(known)
    published, conflict_notes = consolidate(text, prepared)
    notes = [
        _render_note(note, archive_item_id, evidence_source)
        for note in (
            *semantic_notes,
            *known_notes,
            *contained_notes,
            *conflict_notes,
        )
    ]
    mentions = [
        _mention_row(text, mention, archive_item_id, evidence_source)
        for mention in published
    ]
    return mentions, notes


def _mention_row(
    text: str, mention, archive_item_id: int, evidence_source: str
) -> dict:
    start = mention.start_offset
    end = mention.end_offset
    exact = text[start:end]
    if exact != mention.exact_mention:
        raise ArtifactError("aligned mention does not match the displayed body")
    name = text[mention.name_start_offset : mention.name_end_offset]
    if name != mention.name_surface:
        raise ArtifactError("aligned name slice does not match the displayed body")
    title = mention.title_surface
    if title is not None:
        sliced = text[mention.title_start_offset : mention.title_end_offset]
        if sliced != title:
            raise ArtifactError("aligned title slice does not match the displayed body")
    require_at_most_one_line_break(exact)
    return {
        "archive_item_id": archive_item_id,
        "evidence_source": evidence_source,
        "exact_mention": exact,
        "start_offset": start,
        "end_offset": end,
        "evidence_snippet": evidence_line_snippet(text, start, end),
        "name_surface": name,
        "name_start_offset": mention.name_start_offset,
        "name_end_offset": mention.name_end_offset,
        "title_surface": title,
        "title_start_offset": mention.title_start_offset,
        "title_end_offset": mention.title_end_offset,
        "surface_kind": mention.surface_kind,
        "detector_ids": list(mention.detector_ids),
        "known_name_match_quality": mention.known_name_match_quality,
    }


def _render_note(note, archive_item_id: int, evidence_source: str) -> dict:
    row = {
        "bucket": note.bucket,
        "archive_item_id": archive_item_id,
        "evidence_source": evidence_source,
        "surface": note.surface,
        "reason": note.reason,
        "segment_id": note.segment_id,
    }
    if note.start_offset is not None and note.end_offset is not None:
        row["start_offset"] = note.start_offset
        row["end_offset"] = note.end_offset
    return row


def _grouped_notes(notes: list[dict]) -> dict[str, list[dict]]:
    grouped = {"rejected": [], "suppressed": [], "conflicts": []}
    for note in notes:
        bucket = note["bucket"]
        if bucket not in grouped:
            raise RuntimeError(f"unexpected diagnostic bucket: {bucket}")
        row = {key: value for key, value in note.items() if key != "bucket"}
        grouped[bucket].append(row)
    for bucket, rows in grouped.items():
        grouped[bucket] = sorted(rows, key=_note_sort_key)
    return grouped


def _note_sort_key(row: dict) -> tuple:
    return (
        row["archive_item_id"],
        row["evidence_source"],
        row.get("start_offset", -1),
        row.get("end_offset", -1),
        row["reason"],
        row["surface"],
        row["segment_id"] or "",
    )


def _selected_bodies(selection: dict, items_by_id: dict[int, dict]):
    selected_rows = selection.get("selected_items")
    if not isinstance(selected_rows, list):
        raise ArtifactError("discovery selected_items must be a list")
    seen: list[int] = []
    bodies = []
    for row in selected_rows:
        if not isinstance(row, dict):
            raise ArtifactError(
                "discovery selection contains an invalid archive_item_id"
            )
        item_id = require_int(row.get("archive_item_id"), "selected archive_item_id")
        if item_id in seen:
            raise ArtifactError(
                "discovery selection contains an invalid archive_item_id"
            )
        seen.append(item_id)
        item = items_by_id.get(item_id)
        if item is None:
            raise ArtifactError(
                f"discovery selected unknown archive_item_id values: [{item_id}]"
            )
        expected = row.get("displayed_text_sha256")
        actual = displayed_text_sha256(item)
        if actual != expected:
            raise ArtifactError(
                f"displayed text hash changed for archive_item_id {item_id}"
            )
        for body in select_text_bodies(item):
            roles = body.get("text_roles") or []
            if not roles:
                raise ArtifactError("displayed body is missing a text role")
            bodies.append((item_id, roles[0], body["text"]))
    return bodies


def _require_discovery(
    discovery: dict, *, run_id: str, document_snapshot_file_sha256: str
) -> dict:
    selection = verify_generated_artifact(
        require_object(discovery, "discovery selection"),
        DISCOVERY_SELECTION_SCHEMA,
        "discovery selection",
    )
    if selection.get("run_id") != run_id:
        raise RunIdMismatchError(
            f"discovery run_id {selection.get('run_id')!r} does not match {run_id!r}"
        )
    if selection.get("document_snapshot_file_sha256") != document_snapshot_file_sha256:
        raise ArtifactError(
            "discovery selection is not bound to this document snapshot file"
        )
    return selection
