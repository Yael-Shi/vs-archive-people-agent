"""Freeze evidence-package-v2 from mention-proposals-v1.

The proposal artifact is detection geometry. This module is the trust
boundary. It verifies the generated proposal, its discovery and document
parents, and every source slice against the unmodified displayed body.
It does not read a diagnostic sidecar, an identity snapshot, or a provider
configuration, and it does not assign identity.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import (
    DISCOVERY_SELECTION_SCHEMA,
    EVIDENCE_PACKAGE_V2_SCHEMA,
    MENTION_PROPOSALS_SCHEMA,
    ArtifactError,
    InputHashChangedError,
    RunIdMismatchError,
    UnsupportedSchemaVersionError,
    require_object,
)
from vs_archive_people.artifacts.provenance import (
    stamp_generated_artifact,
    verify_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import validate_artifact
from vs_archive_people.evidence.mentions import (
    assign_mention_keys,
    assign_occurrence_indexes,
    mention_fingerprint,
    validate_mention_evidence,
)
from vs_archive_people.evidence.text_bodies import (
    displayed_text_sha256,
    metadata_title,
    require_document_snapshot,
    select_text_bodies,
)
from vs_archive_people.mention_proposals.constraints import (
    require_declared_detectors_and_known_name_quality,
    require_surface_kind,
    require_unique_outer_spans,
)
from vs_archive_people.mention_proposals.geometry import (
    require_at_most_one_line_break,
    require_int,
    require_name_inside_outer,
    require_split_residue,
    require_title_inside_outer,
    require_title_pair,
    slice_span,
)


def recheck_mention_source(mention: dict, body: str, declared: set[str]) -> None:
    """Re-slice ``body`` and apply the official geometry rules to that slice.

    Equality is exact. The displayed body is not normalized. Error text does
    not include the body.
    """
    start = mention["start_offset"]
    end = mention["end_offset"]
    exact = slice_span(body, start, end, "outer mention")
    if exact != mention["exact_mention"]:
        raise ArtifactError(
            "exact_mention does not match the displayed body at the supplied offsets"
        )
    name_start = mention["name_start_offset"]
    name_end = mention["name_end_offset"]
    require_name_inside_outer(start, end, name_start, name_end)
    name = slice_span(body, name_start, name_end, "name")
    if name != mention["name_surface"]:
        raise ArtifactError(
            "name_surface does not match the displayed body at the name offsets"
        )
    title = require_title_pair(
        mention["title_start_offset"], mention["title_end_offset"]
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
        sliced_title = slice_span(body, title_start, title_end, "title")
        if sliced_title != mention["title_surface"]:
            raise ArtifactError(
                "title_surface does not match the displayed body at the title offsets"
            )
        relative_title_start = title_start - start
        relative_title_end = title_end - start
    require_at_most_one_line_break(exact)
    require_split_residue(
        exact,
        name_start - start,
        name_end - start,
        relative_title_start,
        relative_title_end,
    )
    require_surface_kind(mention.get("surface_kind"), title is not None)
    require_declared_detectors_and_known_name_quality(mention, declared)
    validate_mention_evidence(
        mention["exact_mention"], mention["evidence_snippet"], body
    )


def build_evidence_package_v2(
    snapshot: dict,
    discovery: dict,
    proposals: dict,
    *,
    run_id: str,
    document_snapshot_file_sha256: str,
    discovery_file_sha256: str,
    mention_proposals_file_sha256: str,
    created_at: str | None = None,
) -> dict:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ArtifactError("run_id is required")
    document = require_document_snapshot(snapshot)
    selection = _verify_discovery(
        discovery,
        run_id=run_id,
        document_snapshot_file_sha256=document_snapshot_file_sha256,
    )
    proposal = _verify_proposals(
        proposals,
        run_id=run_id,
        document_snapshot_file_sha256=document_snapshot_file_sha256,
        discovery_logical_sha256=selection["logical_content_sha256"],
        discovery_file_sha256=discovery_file_sha256,
    )
    selected_rows = selection["selected_items"]
    items_by_id = {item["archive_item_id"]: item for item in document["items"]}
    selected_ids = [
        require_int(row.get("archive_item_id"), "selected archive_item_id")
        for row in selected_rows
        if isinstance(row, dict)
    ]
    if len(selected_ids) != len(selected_rows) or len(selected_ids) != len(
        set(selected_ids)
    ):
        raise ArtifactError("discovery selection contains an invalid archive_item_id")
    missing = [item_id for item_id in selected_ids if item_id not in items_by_id]
    if missing:
        raise ArtifactError(
            f"discovery selected unknown archive_item_id values: {missing}"
        )

    packaged_items = []
    bodies_by_item: dict[int, list[dict]] = {}
    for item_id in selected_ids:
        item = items_by_id[item_id]
        bodies = select_text_bodies(item)
        bodies_by_item[item_id] = bodies
        expected_hash = _selected_hash(selected_rows, item_id)
        actual_hash = displayed_text_sha256(item)
        if actual_hash != expected_hash:
            raise ArtifactError(
                f"displayed text hash changed for archive_item_id {item_id}"
            )
        packaged_items.append(
            {
                "archive_item_id": item_id,
                "item_type": item["item_type"],
                "displayed_text_sha256": actual_hash,
                "output_metadata_only": {"title": metadata_title(item)},
                "texts": bodies,
            }
        )

    declared = {detector["id"] for detector in proposal["detectors"]}
    checked: list[dict] = []
    for raw in proposal["mentions"]:
        mention = require_object(raw, "mention proposal")
        item_id = require_int(mention.get("archive_item_id"), "archive_item_id")
        if item_id not in items_by_id:
            raise ArtifactError(f"mention archive_item_id {item_id} is unknown")
        if item_id not in bodies_by_item:
            raise ArtifactError(
                f"mention archive_item_id {item_id} is outside the discovery selection"
            )
        evidence_source = mention.get("evidence_source")
        if not isinstance(evidence_source, str) or not evidence_source.strip():
            raise ArtifactError("evidence_source must be a non-empty string")
        body = _body_for_source(bodies_by_item[item_id], evidence_source)
        recheck_mention_source(mention, body["text"], declared)
        checked.append(
            _evidence_row(
                mention,
                source_text_sha256=body["text_sha256"],
            )
        )

    require_unique_outer_spans(checked)
    assign_occurrence_indexes(checked)
    fingerprints = [row["mention_fingerprint"] for row in checked]
    if len(fingerprints) != len(set(fingerprints)):
        raise ArtifactError("duplicate mention_fingerprint")
    mentions = assign_mention_keys(checked)
    packaged_items.sort(key=lambda row: row["archive_item_id"])
    package = {
        "schema_version": EVIDENCE_PACKAGE_V2_SCHEMA,
        "run_id": run_id,
        "document_snapshot_file_sha256": document_snapshot_file_sha256,
        "discovery_logical_sha256": selection["logical_content_sha256"],
        "discovery_file_sha256": discovery_file_sha256,
        "mention_proposals_logical_sha256": proposal["logical_content_sha256"],
        "mention_proposals_file_sha256": mention_proposals_file_sha256,
        "production_write_performed": False,
        "items": packaged_items,
        "mentions": mentions,
    }
    stamp_generated_artifact(package, created_at=created_at)
    validate_artifact(package)
    return package


def _evidence_row(mention: dict, *, source_text_sha256: str) -> dict:
    """Copy verified geometry. Detector registry and identity fields stay out."""
    item_id = mention["archive_item_id"]
    evidence_source = mention["evidence_source"]
    start_offset = mention["start_offset"]
    end_offset = mention["end_offset"]
    return {
        "archive_item_id": item_id,
        "evidence_source": evidence_source,
        "source_text_sha256": source_text_sha256,
        "exact_mention": mention["exact_mention"],
        "start_offset": start_offset,
        "end_offset": end_offset,
        "evidence_snippet": mention["evidence_snippet"],
        "name_surface": mention["name_surface"],
        "name_start_offset": mention["name_start_offset"],
        "name_end_offset": mention["name_end_offset"],
        "title_surface": mention["title_surface"],
        "title_start_offset": mention["title_start_offset"],
        "title_end_offset": mention["title_end_offset"],
        "surface_kind": mention["surface_kind"],
        "detector_ids": list(mention["detector_ids"]),
        "known_name_match_quality": mention["known_name_match_quality"],
        "mention_fingerprint": mention_fingerprint(
            archive_item_id=item_id,
            evidence_source=evidence_source,
            source_text_sha256=source_text_sha256,
            start_offset=start_offset,
            end_offset=end_offset,
        ),
    }


def _verify_discovery(
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
    selected_rows = selection.get("selected_items")
    if not isinstance(selected_rows, list):
        raise ArtifactError("discovery selected_items must be a list")
    return selection


def _verify_proposals(
    proposals: dict,
    *,
    run_id: str,
    document_snapshot_file_sha256: str,
    discovery_logical_sha256: str,
    discovery_file_sha256: str,
) -> dict:
    document = require_object(proposals, "mention proposals")
    if document.get("schema_version") != MENTION_PROPOSALS_SCHEMA:
        raise UnsupportedSchemaVersionError(
            f"mention proposals schema_version must be {MENTION_PROPOSALS_SCHEMA}"
        )
    proposal = verify_generated_artifact(
        document, MENTION_PROPOSALS_SCHEMA, "mention proposals"
    )
    if proposal.get("run_id") != run_id:
        raise RunIdMismatchError(
            f"mention proposals run_id {proposal.get('run_id')!r} does not match {run_id!r}"
        )
    if proposal.get("document_snapshot_file_sha256") != document_snapshot_file_sha256:
        raise InputHashChangedError(
            "mention proposals document_snapshot_file_sha256 does not match "
            "this document snapshot file"
        )
    if proposal.get("discovery_logical_sha256") != discovery_logical_sha256:
        raise InputHashChangedError(
            "mention proposals discovery_logical_sha256 does not match "
            "the verified discovery selection"
        )
    if proposal.get("discovery_file_sha256") != discovery_file_sha256:
        raise InputHashChangedError(
            "mention proposals discovery_file_sha256 does not match "
            "this discovery selection file"
        )
    return proposal


def _selected_hash(rows: list[dict], item_id: int) -> str:
    for row in rows:
        if isinstance(row, dict) and row.get("archive_item_id") == item_id:
            digest = row.get("displayed_text_sha256")
            if not isinstance(digest, str) or not digest:
                raise ArtifactError(
                    f"discovery selection is missing displayed_text_sha256 for {item_id}"
                )
            return digest
    raise ArtifactError(f"discovery selection is missing archive_item_id {item_id}")


def _body_for_source(bodies: list[dict], evidence_source: str) -> dict:
    for body in bodies:
        if evidence_source in body["text_roles"]:
            return body
    raise ArtifactError(
        f"evidence_source {evidence_source!r} is not a displayed text role"
    )
