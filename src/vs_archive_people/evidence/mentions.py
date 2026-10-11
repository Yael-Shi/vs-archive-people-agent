"""Mention evidence checks and central mention_key assignment.

Whitespace runs, including line breaks, collapse to one space for comparison.
Words, letters, and punctuation must still occur in that order. mention_key
values are assigned here from stable sort order. Callers cannot supply them.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import ArtifactError, sha256_json

MENTION_KEY_PREFIX = "M"


def mention_fingerprint(
    *,
    archive_item_id: int,
    evidence_source: str,
    source_text_sha256: str,
    start_offset: int,
    end_offset: int,
) -> str:
    """Durable occurrence id. ``mention_key`` is only a run-local label.

    The digest is SHA-256 of canonical JSON for the source position. It does
    not include ``occurrence_index`` or the proposal's spelling of the mention.
    ``source_text_sha256`` is stored lowercase.
    """
    return sha256_json(
        {
            "archive_item_id": archive_item_id,
            "end_offset": end_offset,
            "evidence_source": evidence_source,
            "source_text_sha256": source_text_sha256.lower(),
            "start_offset": start_offset,
        }
    )


def validate_source_surface(
    exact_mention: str,
    body_text: str,
    start_offset: int,
    end_offset: int,
) -> None:
    """Require the offsets to slice the exact mention from the raw body."""
    if isinstance(start_offset, bool) or not isinstance(start_offset, int):
        raise ArtifactError("start_offset must be an integer")
    if isinstance(end_offset, bool) or not isinstance(end_offset, int):
        raise ArtifactError("end_offset must be an integer")
    if not 0 <= start_offset < end_offset <= len(body_text):
        raise ArtifactError("mention offsets are outside the displayed body")
    if body_text[start_offset:end_offset] != exact_mention:
        raise ArtifactError(
            "exact_mention does not match the displayed body at the supplied offsets"
        )


def normalize_ws(value: str) -> str:
    return " ".join(str(value or "").split())


def snippet_in_body(evidence_snippet: str, body_text: str) -> bool:
    snippet = normalize_ws(evidence_snippet)
    if not snippet:
        return False
    return snippet in normalize_ws(body_text)


def mention_in_snippet(exact_mention: str, evidence_snippet: str) -> bool:
    mention = normalize_ws(exact_mention)
    if not mention:
        return False
    return mention in normalize_ws(evidence_snippet)


def validate_mention_evidence(
    exact_mention: str,
    evidence_snippet: str,
    body_text: str,
) -> None:
    if not isinstance(exact_mention, str) or not exact_mention.strip():
        raise ArtifactError("exact_mention must be a non-empty string")
    if not isinstance(evidence_snippet, str) or not evidence_snippet.strip():
        raise ArtifactError("evidence_snippet must be a non-empty string")
    if not snippet_in_body(evidence_snippet, body_text):
        raise ArtifactError(
            "evidence_snippet is not a contiguous excerpt of the displayed body "
            "after whitespace-only normalization"
        )
    if not mention_in_snippet(exact_mention, evidence_snippet):
        raise ArtifactError(
            "exact_mention is not present in evidence_snippet after whitespace "
            "normalization"
        )


def mention_key_for(index: int) -> str:
    if index < 1:
        raise ArtifactError("mention_key index must start at 1")
    return f"{MENTION_KEY_PREFIX}{index:04d}"


def assign_occurrence_indexes(proposals: list[dict]) -> None:
    """Number mentions from source order inside each item and text role.

    Caller numbering is ignored. An earlier span gets a lower index.
    """
    groups: dict[tuple[int, str], list[dict]] = {}
    for row in proposals:
        key = (int(row["archive_item_id"]), str(row["evidence_source"]))
        groups.setdefault(key, []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda item: (int(item["start_offset"]), int(item["end_offset"])))
        for index, row in enumerate(rows):
            row["occurrence_index"] = index


def assign_mention_keys(proposals: list[dict]) -> list[dict]:
    """Return copies with run-local keys. Input order does not decide the key."""
    ordered = sorted(
        proposals,
        key=lambda row: (
            int(row["archive_item_id"]),
            str(row["evidence_source"]),
            int(row["start_offset"]),
            int(row["end_offset"]),
        ),
    )
    assigned: list[dict] = []
    seen_order: set[tuple[int, str, int, int]] = set()
    for index, row in enumerate(ordered, start=1):
        order_key = (
            int(row["archive_item_id"]),
            str(row["evidence_source"]),
            int(row["start_offset"]),
            int(row["end_offset"]),
        )
        if order_key in seen_order:
            raise ArtifactError(
                "duplicate source span for one archive item and text role"
            )
        seen_order.add(order_key)
        assigned_row = dict(row)
        assigned_row.pop("mention_key", None)
        assigned_row["mention_key"] = mention_key_for(index)
        assigned.append(assigned_row)
    keys = [row["mention_key"] for row in assigned]
    if len(keys) != len(set(keys)):
        raise ArtifactError("mention_key assignment produced a duplicate")
    return assigned
