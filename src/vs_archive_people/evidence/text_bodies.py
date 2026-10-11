"""Displayed-text selection.

Titles, authors, public notes, and timestamps are metadata. Only declared
displayed bodies are evidence. Identical raw strings that appear in more than
one role are one body with every role recorded. Strings that differ by
whitespace stay separate.
"""

from __future__ import annotations

from typing import Any

from vs_archive_people.artifacts.io import (
    DOCUMENT_TEXT_SNAPSHOT_SCHEMA,
    ArtifactError,
    require_object,
    require_schema,
    sha256_json,
)

OCR_ROLES = (
    (
        "ocr_document.displayed_transcription.text",
        ("ocr_document", "displayed_transcription", "text"),
    ),
    (
        "ocr_document.displayed_source_text.text",
        ("ocr_document", "displayed_source_text", "text"),
    ),
    (
        "ocr_document.displayed_hebrew_translation.text",
        ("ocr_document", "displayed_hebrew_translation", "text"),
    ),
)
MANUAL_ROLES = (("manual_text.body", ("manual_text", "body")),)


def _nested_raw(item: dict, path: tuple[str, ...]) -> str | None:
    value: Any = item
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if not isinstance(value, str):
        return None
    return value


def _roles_for(item: dict) -> tuple[tuple[str, tuple[str, ...]], ...]:
    item_type = item.get("item_type")
    if item_type == "OCR_DOCUMENT":
        return OCR_ROLES
    if item_type == "MANUAL_TEXT":
        return MANUAL_ROLES
    raise ArtifactError(
        f"unsupported item_type {item_type!r} for archive_item_id "
        f"{item.get('archive_item_id')}"
    )


def select_text_bodies(item: dict) -> list[dict]:
    """Return displayed bodies. Empty or whitespace-only strings are omitted."""
    require_object(item, "document item")
    bodies: list[dict] = []
    for role, path in _roles_for(item):
        raw_text = _nested_raw(item, path)
        if raw_text is None or not raw_text.strip():
            continue
        matching = next(
            (body for body in bodies if body["text"] == raw_text),
            None,
        )
        if matching is not None:
            matching["text_roles"].append(role)
            matching["text_role"] = "|".join(matching["text_roles"])
            continue
        bodies.append(
            {
                "text_role": role,
                "text_roles": [role],
                "text": raw_text,
                "text_sha256": sha256_json(raw_text),
            }
        )
    return bodies


def displayed_text_sha256(item: dict) -> str:
    """Hash declared displayed bodies. Timestamps and titles are not inputs."""
    payload = [
        {"text": body["text"], "text_role": body["text_role"]}
        for body in select_text_bodies(item)
    ]
    return sha256_json(payload)


def metadata_title(item: dict) -> str:
    title = item.get("title")
    return title if isinstance(title, str) else ""


def require_document_snapshot(document: dict) -> dict:
    snapshot = require_object(document, "document text snapshot")
    require_schema(snapshot, DOCUMENT_TEXT_SNAPSHOT_SCHEMA, "document text snapshot")
    items = snapshot.get("items")
    if not isinstance(items, list):
        raise ArtifactError("document text snapshot items must be a list")
    seen: set[int] = set()
    for item in items:
        row = require_object(item, "document item")
        item_id = row.get("archive_item_id")
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            raise ArtifactError("archive_item_id must be an integer")
        if item_id in seen:
            raise ArtifactError(f"duplicate archive_item_id {item_id}")
        seen.add(item_id)
        select_text_bodies(row)
    return snapshot
