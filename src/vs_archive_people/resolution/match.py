"""Classify each mention against a production identity index.

Deterministic means the normalized mention equals exactly one production
Person.name or PersonAlias.name. Shared forms are ambiguous. Unknown person
forms are new-person candidates. NOT_A_PERSON is accepted only when the mention
record says so.

Confidence is copied onto the row and is not read by the classifier.
Local or pending candidates never count as production identity.
MERGE and CANONICAL_NAME_CHANGE are recorded only when the mention proposal
already carries that action. This module does not invent them.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import (
    EVIDENCE_PACKAGE_SCHEMA,
    RESOLUTION_REPORT_SCHEMA,
    ArtifactError,
    require_object,
)
from vs_archive_people.artifacts.provenance import (
    stamp_generated_artifact,
    verify_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import validate_artifact
from vs_archive_people.resolution.snapshot_index import IdentityIndex, match_key

CLASS_DETERMINISTIC = "DETERMINISTIC_EXISTING_PERSON"
CLASS_AMBIGUOUS = "AMBIGUOUS"
CLASS_NEW = "NEW_PERSON_CANDIDATE"
CLASS_NOT_PERSON = "NOT_A_PERSON"


def _local_names(local_candidates: list[dict] | None) -> set[str]:
    if not local_candidates:
        return set()
    names: set[str] = set()
    for row in local_candidates:
        candidate = require_object(row, "local candidate")
        status = str(candidate.get("review_status", "PENDING")).upper()
        if status != "PENDING":
            raise ArtifactError("local candidates must stay PENDING")
        name = candidate.get("name") or candidate.get("proposed_canonical_name")
        if not isinstance(name, str) or not name.strip():
            raise ArtifactError("local candidate name is required")
        names.add(match_key(name))
    return names


def _classify_one(mention: dict, index: IdentityIndex, local_names: set[str]) -> dict:
    exact = mention["exact_mention"]
    mention_class = mention.get("mention_class", "PERSON")
    local_overlap = match_key(exact) in local_names
    action = mention.get("proposed_identity_action")
    propose_alias = bool(mention.get("propose_alias"))
    hits = [] if mention_class == "NOT_A_PERSON" else index.lookup(exact)
    person_ids = [person_id for person_id, _via in hits]
    if mention_class == "NOT_A_PERSON":
        classification = CLASS_NOT_PERSON
        matched_via = None
    elif len(person_ids) == 1:
        classification = CLASS_DETERMINISTIC
        matched_via = hits[0][1]
    elif len(person_ids) > 1:
        classification = CLASS_AMBIGUOUS
        matched_via = None
    else:
        classification = CLASS_NEW
        matched_via = None

    already_known = len(person_ids) == 1 and matched_via in {
        "canonical_name",
        "person_alias",
    }
    inferred_alias = bool(
        propose_alias and mention_class != "NOT_A_PERSON" and not already_known
    )
    matched_alias_id = None
    matched_name = None
    stable_keys: list[str] = []
    if len(person_ids) == 1:
        person = index.people[person_ids[0]]
        matched_name = person.canonical_name
        stable_keys = list(person.stable_keys)
        if matched_via == "person_alias":
            matched_alias_id = index.alias_id(person.person_id, exact)
    elif len(person_ids) > 1:
        stable_keys = sorted(
            {
                key
                for person_id in person_ids
                for key in index.people[person_id].stable_keys
            }
        )
    return {
        "mention_key": mention["mention_key"],
        "archive_item_id": mention["archive_item_id"],
        "exact_mention": exact,
        "evidence_source": mention["evidence_source"],
        "source_text_sha256": mention["source_text_sha256"],
        "evidence_snippet": mention["evidence_snippet"],
        "start_offset": mention["start_offset"],
        "end_offset": mention["end_offset"],
        "occurrence_index": mention["occurrence_index"],
        "mention_fingerprint": mention["mention_fingerprint"],
        "classification": classification,
        "matched_person_ids": person_ids,
        "matched_person_name": matched_name,
        "matched_via": matched_via,
        "matched_alias_id": matched_alias_id,
        "stable_keys": stable_keys,
        "confidence": mention.get("confidence"),
        "confidence_used_for_classification": False,
        "inferred_alias_proposal": inferred_alias,
        "proposed_identity_action": action,
        "local_candidate_overlap": local_overlap,
        "review_status": "PENDING",
        "identity_is_production": classification == CLASS_DETERMINISTIC,
    }


def _require_sha256(value: str | None, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in value)
    ):
        raise ArtifactError(f"{label} must be a SHA-256 hex digest")


def _require_local_candidate_provenance(
    local_candidates: list[dict] | None,
    local_candidates_file_sha256: str | None,
) -> None:
    """A supplied candidate list, including an empty one, must name its file."""
    if (local_candidates is None) != (local_candidates_file_sha256 is None):
        raise ArtifactError(
            "local_candidates and local_candidates_file_sha256 must both be "
            "absent or both be present"
        )
    if local_candidates_file_sha256 is not None:
        _require_sha256(local_candidates_file_sha256, "local_candidates_file_sha256")


def build_resolution_report(
    evidence_package: dict,
    index: IdentityIndex,
    *,
    evidence_package_file_sha256: str,
    identity_snapshot_file_sha256: str,
    local_candidates: list[dict] | None = None,
    local_candidates_file_sha256: str | None = None,
    created_at: str | None = None,
) -> dict:
    package = verify_generated_artifact(
        require_object(evidence_package, "evidence package"),
        EVIDENCE_PACKAGE_SCHEMA,
        "evidence package",
    )
    if package.get("document_snapshot_file_sha256") is None:
        raise ArtifactError(
            "evidence package is missing its document snapshot file hash"
        )
    _require_local_candidate_provenance(local_candidates, local_candidates_file_sha256)
    mentions = package.get("mentions")
    if not isinstance(mentions, list):
        raise ArtifactError("evidence package mentions must be a list")
    keys = [row.get("mention_key") for row in mentions if isinstance(row, dict)]
    if len(keys) != len(mentions) or any(
        not isinstance(key, str) or not key for key in keys
    ):
        raise ArtifactError("every mention needs a mention_key")
    if len(keys) != len(set(keys)):
        raise ArtifactError("evidence package contains a duplicate mention_key")
    fingerprints = [
        row.get("mention_fingerprint") for row in mentions if isinstance(row, dict)
    ]
    if len(fingerprints) != len(mentions) or any(
        not isinstance(value, str) or not value for value in fingerprints
    ):
        raise ArtifactError("every mention needs a mention_fingerprint")
    if len(fingerprints) != len(set(fingerprints)):
        raise ArtifactError("evidence package contains a duplicate mention_fingerprint")
    ordered = sorted(
        (require_object(mention, "mention") for mention in mentions),
        key=lambda row: int(str(row["mention_key"])[1:]),
    )
    local = _local_names(local_candidates)
    resolutions = [_classify_one(mention, index, local) for mention in ordered]
    if len({row["mention_key"] for row in resolutions}) != len(resolutions):
        raise ArtifactError("resolution report would repeat a mention_key")
    report = {
        "schema_version": RESOLUTION_REPORT_SCHEMA,
        "run_id": package["run_id"],
        "evidence_package_logical_sha256": package["logical_content_sha256"],
        "evidence_package_file_sha256": evidence_package_file_sha256,
        "identity_snapshot_file_sha256": identity_snapshot_file_sha256,
        "local_candidates_file_sha256": local_candidates_file_sha256,
        "production_write_performed": False,
        "confidence_approves_identity": False,
        "local_candidates_are_production_identity": False,
        "photo_person_rows_ignored": index.photo_person_rows_ignored,
        "photo_person_created": False,
        "resolutions": resolutions,
    }
    stamp_generated_artifact(report, created_at=created_at)
    validate_artifact(report)
    return report
