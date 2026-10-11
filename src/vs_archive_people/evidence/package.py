"""Freeze an immutable evidence package from a document snapshot and proposals.

Mention finding is not done here. A proposal file supplies the mentions.
This module checks each snippet against the displayed body and assigns
mention_key values. It does not resolve identity.
"""

from __future__ import annotations

from typing import Any

from vs_archive_people.artifacts.io import (
    DISCOVERY_SELECTION_SCHEMA,
    EVIDENCE_PACKAGE_SCHEMA,
    ArtifactError,
    RunIdMismatchError,
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
    validate_source_surface,
)
from vs_archive_people.evidence.text_bodies import (
    displayed_text_sha256,
    metadata_title,
    require_document_snapshot,
    select_text_bodies,
)

_IDENTITY_ACTIONS = {None, "MERGE", "CANONICAL_NAME_CHANGE"}
_MENTION_CLASSES = {"PERSON", "NOT_A_PERSON"}


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactError(f"{label} must be an integer")
    return value


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


def _optional_confidence(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ArtifactError("confidence must be an integer from 0 through 100")
    return value


def _proposal_fields(raw: dict) -> dict:
    row = require_object(raw, "mention proposal")
    item_id = _require_int(row.get("archive_item_id"), "archive_item_id")
    evidence_source = row.get("evidence_source")
    if not isinstance(evidence_source, str) or not evidence_source.strip():
        raise ArtifactError("evidence_source must be a non-empty string")
    start_offset = _require_int(row.get("start_offset"), "start_offset")
    end_offset = _require_int(row.get("end_offset"), "end_offset")
    mention_class = row.get("mention_class", "PERSON")
    if mention_class not in _MENTION_CLASSES:
        raise ArtifactError("mention_class must be PERSON or NOT_A_PERSON")
    action = row.get("proposed_identity_action")
    if action not in _IDENTITY_ACTIONS:
        raise ArtifactError(
            "proposed_identity_action must be MERGE, CANONICAL_NAME_CHANGE, or null"
        )
    propose_alias = row.get("propose_alias", False)
    if not isinstance(propose_alias, bool):
        raise ArtifactError("propose_alias must be a boolean")
    row.pop("mention_key", None)
    row.pop("mention_fingerprint", None)
    exact_mention = row.get("exact_mention")
    evidence_snippet = row.get("evidence_snippet")
    if not isinstance(exact_mention, str) or not isinstance(evidence_snippet, str):
        raise ArtifactError("exact_mention and evidence_snippet must be strings")
    return {
        "archive_item_id": item_id,
        "evidence_source": evidence_source,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "exact_mention": exact_mention,
        "evidence_snippet": evidence_snippet,
        "mention_class": mention_class,
        "propose_alias": propose_alias,
        "proposed_identity_action": action,
        "confidence": _optional_confidence(row.get("confidence")),
        "review_status": "PENDING",
    }


def _body_for_source(bodies: list[dict], evidence_source: str) -> dict:
    for body in bodies:
        if evidence_source in body["text_roles"]:
            return body
    raise ArtifactError(
        f"evidence_source {evidence_source!r} is not a displayed text role"
    )


def build_evidence_package(
    snapshot: dict,
    discovery: dict,
    proposals: list[dict],
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
    selected_ids = [
        _require_int(row.get("archive_item_id"), "selected archive_item_id")
        for row in selected_rows
        if isinstance(row, dict)
    ]
    if len(selected_ids) != len(selected_rows) or len(selected_ids) != len(
        set(selected_ids)
    ):
        raise ArtifactError("discovery selection contains an invalid archive_item_id")
    items_by_id = {item["archive_item_id"]: item for item in document["items"]}
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

    checked: list[dict] = []
    for raw in proposals:
        proposal = _proposal_fields(raw)
        item_id = proposal["archive_item_id"]
        if item_id not in bodies_by_item:
            raise ArtifactError(
                f"mention archive_item_id {item_id} is outside the discovery selection"
            )
        body = _body_for_source(bodies_by_item[item_id], proposal["evidence_source"])
        validate_mention_evidence(
            proposal["exact_mention"],
            proposal["evidence_snippet"],
            body["text"],
        )
        validate_source_surface(
            proposal["exact_mention"],
            body["text"],
            proposal["start_offset"],
            proposal["end_offset"],
        )
        proposal["source_text_sha256"] = body["text_sha256"]
        proposal["text_role"] = proposal["evidence_source"]
        proposal.pop("mention_fingerprint", None)
        proposal["mention_fingerprint"] = mention_fingerprint(
            archive_item_id=item_id,
            evidence_source=proposal["evidence_source"],
            source_text_sha256=body["text_sha256"],
            start_offset=proposal["start_offset"],
            end_offset=proposal["end_offset"],
        )
        checked.append(proposal)

    assign_occurrence_indexes(checked)

    fingerprints = [row["mention_fingerprint"] for row in checked]
    if len(fingerprints) != len(set(fingerprints)):
        raise ArtifactError("duplicate mention_fingerprint")
    mentions = assign_mention_keys(checked)
    packaged_items.sort(key=lambda row: row["archive_item_id"])
    package = {
        "schema_version": EVIDENCE_PACKAGE_SCHEMA,
        "run_id": run_id,
        "document_snapshot_file_sha256": document_snapshot_file_sha256,
        "discovery_logical_sha256": selection["logical_content_sha256"],
        "discovery_file_sha256": discovery_file_sha256,
        "mention_proposals_file_sha256": mention_proposals_file_sha256,
        "production_write_performed": False,
        "items": packaged_items,
        "mentions": mentions,
    }
    stamp_generated_artifact(package, created_at=created_at)
    validate_artifact(package)
    return package
