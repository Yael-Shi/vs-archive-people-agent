"""Select archive items whose displayed text is unseen or changed.

Selection uses archive_item_id and the displayed-text hash recorded on a prior
checkpoint. updated_at and created_at are ignored.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import (
    DISCOVERY_SELECTION_SCHEMA,
    ArtifactError,
    require_object,
)
from vs_archive_people.evidence.text_bodies import (
    displayed_text_sha256,
    require_document_snapshot,
)
from vs_archive_people.runs.checkpoint import require_completed_prior_checkpoint


def processed_item_hashes(checkpoint: dict | None) -> dict[int, str]:
    if checkpoint is None:
        return {}
    document = require_completed_prior_checkpoint(
        require_object(checkpoint, "checkpoint")
    )
    rows = document.get("processed_items")
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise ArtifactError("checkpoint processed_items must be a list")
    found: dict[int, str] = {}
    for row in rows:
        item = require_object(row, "processed item")
        item_id = item.get("archive_item_id")
        digest = item.get("displayed_text_sha256")
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            raise ArtifactError("processed archive_item_id must be an integer")
        if not isinstance(digest, str) or not digest:
            raise ArtifactError("processed displayed_text_sha256 is required")
        if item_id in found and found[item_id] != digest:
            raise ArtifactError(
                f"checkpoint has conflicting hashes for archive_item_id {item_id}"
            )
        found[item_id] = digest
    return found


def select_items(snapshot: dict, prior_checkpoint: dict | None = None) -> dict:
    document = require_document_snapshot(snapshot)
    previous = processed_item_hashes(prior_checkpoint)
    selected = []
    for item in sorted(document["items"], key=lambda row: row["archive_item_id"]):
        item_id = item["archive_item_id"]
        digest = displayed_text_sha256(item)
        prior = previous.get(item_id)
        if prior is None:
            reason = "not_previously_processed"
        elif prior != digest:
            reason = "displayed_text_changed"
        else:
            continue
        selected.append(
            {
                "archive_item_id": item_id,
                "displayed_text_sha256": digest,
                "reason": reason,
            }
        )
    return {
        "schema_version": DISCOVERY_SELECTION_SCHEMA,
        "selected_items": selected,
    }
