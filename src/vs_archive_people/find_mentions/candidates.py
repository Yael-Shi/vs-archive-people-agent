"""Semantic candidate schema and parsing.

The detector proposes ``segment_id`` and ``surface``. It does not propose offsets.
"""

from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from vs_archive_people.find_mentions.errors import FindMentionsProviderError
from vs_archive_people.find_mentions.proposals import SemanticProposal

GEMINI_KINDS = (
    "FULL_NAME",
    "PARTIAL_NAME",
    "TITLE_AND_NAME",
    "NICKNAME",
    "UNCERTAIN_PERSON_NAME",
)
_NON_TITLE_KINDS = tuple(kind for kind in GEMINI_KINDS if kind != "TITLE_AND_NAME")


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


_OPTIONAL_CANDIDATE_FIELDS = {
    "occurrence": _nullable({"type": "integer", "minimum": 1}),
    "end_segment_id": _nullable({"type": "string"}),
    "referent": _nullable({"enum": ["PERSON", "NON_PERSON"]}),
}

TITLE_AND_NAME_CANDIDATE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["segment_id", "surface", "kind", "name_surface", "title_surface"],
    "properties": {
        "segment_id": {"type": "string"},
        "surface": {"type": "string"},
        "kind": {"enum": ["TITLE_AND_NAME"]},
        "name_surface": {"type": "string"},
        "title_surface": {"type": "string"},
        **_OPTIONAL_CANDIDATE_FIELDS,
    },
}

NON_TITLE_CANDIDATE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["segment_id", "surface"],
    "properties": {
        "segment_id": {"type": "string"},
        "surface": {"type": "string"},
        "kind": _nullable({"enum": list(_NON_TITLE_KINDS)}),
        "name_surface": _nullable({"type": "string"}),
        "title_surface": _nullable({"type": "string"}),
        **_OPTIONAL_CANDIDATE_FIELDS,
    },
}

CANDIDATE_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "anyOf": [
                    TITLE_AND_NAME_CANDIDATE_SCHEMA,
                    NON_TITLE_CANDIDATE_SCHEMA,
                ]
            },
        }
    },
}

_VALIDATOR = Draft202012Validator(CANDIDATE_RESPONSE_SCHEMA)


def parse_candidate_payload(payload: object) -> list[SemanticProposal]:
    """Validate a provider JSON object and return proposals. No offsets."""
    try:
        _VALIDATOR.validate(payload)
    except ValidationError:
        raise FindMentionsProviderError(
            "provider candidate JSON failed schema validation"
        ) from None
    if not isinstance(payload, dict):
        raise FindMentionsProviderError(
            "provider candidate JSON failed schema validation"
        )
    proposals: list[SemanticProposal] = []
    for row in payload["candidates"]:
        kind = row.get("kind")
        occurrence = row.get("occurrence")
        proposals.append(
            SemanticProposal(
                segment_id=row["segment_id"],
                surface=row["surface"],
                kind=kind if isinstance(kind, str) else None,
                occurrence=occurrence if isinstance(occurrence, int) else None,
                name_surface=_optional_text(row, "name_surface"),
                title_surface=_optional_text(row, "title_surface"),
                end_segment_id=_optional_text(row, "end_segment_id"),
                referent=_optional_text(row, "referent"),
            )
        )
    return proposals


def _optional_text(row: dict, key: str) -> str | None:
    if key not in row or row[key] is None:
        return None
    value = row[key]
    if not isinstance(value, str):
        return None
    if key != "name_surface" and value == "":
        return None
    return value
