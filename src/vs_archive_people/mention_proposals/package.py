"""Build mention-proposals-v1 from human geometry.

The geometry file supplies offsets only. Exact slices and the evidence
snippet are copied from the displayed body. This command does not call a
provider, does not read an identity snapshot, and does not assign identity.
"""

from __future__ import annotations

from typing import Any

from vs_archive_people.artifacts.io import (
    DISCOVERY_SELECTION_SCHEMA,
    MENTION_PROPOSALS_SCHEMA,
    ArtifactError,
    RunIdMismatchError,
    require_object,
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
from vs_archive_people.mention_proposals.constraints import (
    mentions_in_source_order,
    require_unique_outer_spans,
)
from vs_archive_people.mention_proposals.geometry import (
    SURFACE_KINDS,
    evidence_line_snippet,
    require_at_most_one_line_break,
    require_int,
    require_name_inside_outer,
    require_split_residue,
    require_title_inside_outer,
    require_title_pair,
    slice_span,
)

HUMAN_DETECTOR = {
    "id": "human-authored",
    "role": "human",
    "implementation": "manual",
    "version": "1",
}
HUMAN_DETECTOR_ID = HUMAN_DETECTOR["id"]

_GEOMETRY_FIELDS = frozenset(
    {
        "archive_item_id",
        "evidence_source",
        "start_offset",
        "end_offset",
        "name_start_offset",
        "name_end_offset",
        "title_start_offset",
        "title_end_offset",
        "surface_kind",
    }
)
_REQUIRED_GEOMETRY = _GEOMETRY_FIELDS - {"surface_kind"}


def package_mention_proposals(
    snapshot: dict,
    discovery: dict,
    geometry: dict,
    *,
    run_id: str,
    document_snapshot_file_sha256: str,
    discovery_file_sha256: str,
    created_at: str | None = None,
) -> dict:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ArtifactError("run_id is required")
    document = require_document_snapshot(snapshot)
    selection = _require_discovery(
        discovery,
        run_id=run_id,
        document_snapshot_file_sha256=document_snapshot_file_sha256,
    )
    rows = _geometry_rows(geometry)
    items_by_id = {item["archive_item_id"]: item for item in document["items"]}
    _require_selected_items(selection, items_by_id)
    mentions = mentions_in_source_order(
        [_mention_from_geometry(row, items_by_id, selection) for row in rows]
    )
    require_unique_outer_spans(mentions)
    packaged = {
        "schema_version": MENTION_PROPOSALS_SCHEMA,
        "run_id": run_id,
        "document_snapshot_file_sha256": document_snapshot_file_sha256,
        "discovery_logical_sha256": selection["logical_content_sha256"],
        "discovery_file_sha256": discovery_file_sha256,
        "identity_snapshot_file_sha256": None,
        "production_write_performed": False,
        "detectors": [dict(HUMAN_DETECTOR)],
        "mentions": mentions,
    }
    stamp_generated_artifact(packaged, created_at=created_at)
    validate_artifact(packaged)
    return packaged


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


def _require_selected_items(selection: dict, items_by_id: dict[int, dict]) -> None:
    selected_rows = selection.get("selected_items")
    if not isinstance(selected_rows, list):
        raise ArtifactError("discovery selected_items must be a list")
    selected_ids = []
    for row in selected_rows:
        if not isinstance(row, dict):
            raise ArtifactError(
                "discovery selection contains an invalid archive_item_id"
            )
        selected_ids.append(
            require_int(row.get("archive_item_id"), "selected archive_item_id")
        )
    if len(selected_ids) != len(set(selected_ids)):
        raise ArtifactError("discovery selection contains an invalid archive_item_id")
    missing = [item_id for item_id in selected_ids if item_id not in items_by_id]
    if missing:
        raise ArtifactError(
            f"discovery selected unknown archive_item_id values: {missing}"
        )


def _geometry_rows(geometry: object) -> list[dict]:
    document = require_object(geometry, "mention geometry")
    extra = set(document) - {"mentions"}
    if extra:
        name = sorted(extra)[0]
        raise ArtifactError(f"forbidden geometry property {name!r}")
    rows = document.get("mentions")
    if not isinstance(rows, list):
        raise ArtifactError("mention geometry mentions must be a list")
    return [require_object(row, "mention geometry row") for row in rows]


def _mention_from_geometry(
    row: dict, items_by_id: dict[int, dict], selection: dict
) -> dict:
    extra = set(row) - _GEOMETRY_FIELDS
    if extra:
        name = sorted(extra)[0]
        raise ArtifactError(f"forbidden geometry property {name!r}")
    missing = _REQUIRED_GEOMETRY - set(row)
    if missing:
        name = sorted(missing)[0]
        raise ArtifactError(f"mention geometry is missing {name}")
    item_id = require_int(row.get("archive_item_id"), "archive_item_id")
    evidence_source = row.get("evidence_source")
    if not isinstance(evidence_source, str) or not evidence_source.strip():
        raise ArtifactError("evidence_source must be a non-empty string")
    _require_item_selected(selection, items_by_id, item_id)
    body = _body_for_source(select_text_bodies(items_by_id[item_id]), evidence_source)
    text = body["text"]
    start = require_int(row.get("start_offset"), "start_offset")
    end = require_int(row.get("end_offset"), "end_offset")
    name_start = require_int(row.get("name_start_offset"), "name_start_offset")
    name_end = require_int(row.get("name_end_offset"), "name_end_offset")
    title_start = _optional_offset(row.get("title_start_offset"), "title_start_offset")
    title_end = _optional_offset(row.get("title_end_offset"), "title_end_offset")
    exact_mention = slice_span(text, start, end, "outer mention")
    require_at_most_one_line_break(exact_mention)
    slice_span(text, name_start, name_end, "name")
    require_name_inside_outer(start, end, name_start, name_end)
    title_pair = require_title_pair(title_start, title_end)
    title_surface = None
    if title_pair is not None:
        title_start, title_end = title_pair
        require_title_inside_outer(
            start, end, name_start, name_end, title_start, title_end
        )
        title_surface = slice_span(text, title_start, title_end, "title")
    require_split_residue(
        exact_mention,
        name_start - start,
        name_end - start,
        None if title_start is None else title_start - start,
        None if title_end is None else title_end - start,
    )
    surface_kind = row["surface_kind"] if "surface_kind" in row else None
    if surface_kind is not None and surface_kind not in SURFACE_KINDS:
        raise ArtifactError(
            "surface_kind must be FULL_NAME, PARTIAL_NAME, TITLE_AND_NAME, "
            "NICKNAME, UNCERTAIN_PERSON_NAME, or null"
        )
    return {
        "archive_item_id": item_id,
        "evidence_source": evidence_source,
        "exact_mention": exact_mention,
        "start_offset": start,
        "end_offset": end,
        "evidence_snippet": evidence_line_snippet(text, start, end),
        "name_surface": text[name_start:name_end],
        "name_start_offset": name_start,
        "name_end_offset": name_end,
        "title_surface": title_surface,
        "title_start_offset": title_start,
        "title_end_offset": title_end,
        "surface_kind": surface_kind,
        "detector_ids": [HUMAN_DETECTOR_ID],
        "known_name_match_quality": None,
    }


def _optional_offset(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return require_int(value, label)


def _require_item_selected(
    selection: dict, items_by_id: dict[int, dict], item_id: int
) -> None:
    selected_rows = selection["selected_items"]
    selected = next(
        (
            row
            for row in selected_rows
            if isinstance(row, dict) and row.get("archive_item_id") == item_id
        ),
        None,
    )
    if selected is None:
        raise ArtifactError(
            f"mention archive_item_id {item_id} is outside the discovery selection"
        )
    if item_id not in items_by_id:
        raise ArtifactError(
            f"mention archive_item_id {item_id} is outside the discovery selection"
        )
    expected = selected.get("displayed_text_sha256")
    actual = displayed_text_sha256(items_by_id[item_id])
    if actual != expected:
        raise ArtifactError(
            f"displayed text hash changed for archive_item_id {item_id}"
        )


def _body_for_source(bodies: list[dict], evidence_source: str) -> dict:
    for body in bodies:
        if evidence_source in body["text_roles"]:
            return body
    raise ArtifactError(
        f"evidence_source {evidence_source!r} is not a displayed text role"
    )
