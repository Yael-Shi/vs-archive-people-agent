"""Build a human identity-review package from a resolution report.

Review rows are ambiguous matches, new-person candidates, inferred alias
proposals, and explicit identity-changing actions. Deterministic matches are
listed separately as auto-resolution proposals. Nothing here is approved for
production, and biography fields are not copied.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import (
    IDENTITY_REVIEW_PACKAGE_SCHEMA,
    RESOLUTION_REPORT_SCHEMA,
    ArtifactError,
    require_object,
)
from vs_archive_people.artifacts.provenance import (
    stamp_generated_artifact,
    verify_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import validate_artifact
from vs_archive_people.resolution.match import (
    CLASS_AMBIGUOUS,
    CLASS_DETERMINISTIC,
    CLASS_NEW,
)

_FORBIDDEN_KEYS = {
    "biography",
    "existing_biography",
    "proposed_biography",
    "photo_person",
    "photo_person_id",
    "photo_content_id",
    "PhotoPerson",
}


def _reject_forbidden(value: object, label: str) -> None:
    if isinstance(value, dict):
        found = _FORBIDDEN_KEYS.intersection(value)
        if found:
            raise ArtifactError(f"{label} contains forbidden fields: {sorted(found)}")
        for key, item in value.items():
            _reject_forbidden(item, f"{label}.{key}")
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden(item, label)


def _needs_review(row: dict) -> bool:
    return (
        row["classification"] in {CLASS_AMBIGUOUS, CLASS_NEW}
        or row.get("inferred_alias_proposal") is True
        or row.get("proposed_identity_action") is not None
    )


def _review_reason(row: dict) -> str:
    reasons = []
    if row["classification"] == CLASS_AMBIGUOUS:
        reasons.append("ambiguous_existing_person")
    if row["classification"] == CLASS_NEW:
        reasons.append("new_person_candidate")
    if row.get("inferred_alias_proposal") is True:
        reasons.append("inferred_alias")
    if row.get("proposed_identity_action"):
        reasons.append(str(row["proposed_identity_action"]).lower())
    return "|".join(reasons)


def _public_row(row: dict) -> dict:
    return {
        "mention_key": row["mention_key"],
        "archive_item_id": row["archive_item_id"],
        "exact_mention": row["exact_mention"],
        "evidence_source": row["evidence_source"],
        "source_text_sha256": row["source_text_sha256"],
        "evidence_snippet": row["evidence_snippet"],
        "start_offset": row["start_offset"],
        "end_offset": row["end_offset"],
        "occurrence_index": row["occurrence_index"],
        "mention_fingerprint": row["mention_fingerprint"],
        "classification": row["classification"],
        "matched_person_ids": list(row.get("matched_person_ids") or []),
        "matched_person_name": row.get("matched_person_name"),
        "matched_via": row.get("matched_via"),
        "stable_keys": list(row.get("stable_keys") or []),
        "confidence": row.get("confidence"),
        "confidence_used_for_classification": False,
        "inferred_alias_proposal": row.get("inferred_alias_proposal") is True,
        "proposed_identity_action": row.get("proposed_identity_action"),
        "local_candidate_overlap": row.get("local_candidate_overlap") is True,
        "review_status": "PENDING",
    }


def build_review_package(
    resolution_report: dict,
    *,
    resolution_report_file_sha256: str,
    created_at: str | None = None,
) -> dict:
    report = verify_generated_artifact(
        require_object(resolution_report, "resolution report"),
        RESOLUTION_REPORT_SCHEMA,
        "resolution report",
    )
    _reject_forbidden(report, "resolution report")
    resolutions = report.get("resolutions")
    if not isinstance(resolutions, list):
        raise ArtifactError("resolution report resolutions must be a list")
    review_rows = []
    auto_rows = []
    seen: set[str] = set()
    seen_fingerprints: set[str] = set()
    for raw in resolutions:
        row = require_object(raw, "resolution")
        key = row.get("mention_key")
        if not isinstance(key, str) or key in seen:
            raise ArtifactError("each resolution needs a unique mention_key")
        seen.add(key)
        fingerprint = row.get("mention_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ArtifactError("each resolution needs a mention_fingerprint")
        if fingerprint in seen_fingerprints:
            raise ArtifactError("identity review repeats a mention_fingerprint")
        seen_fingerprints.add(fingerprint)
        if row.get("confidence_used_for_classification") is not False:
            raise ArtifactError(
                "confidence must not be marked as used for classification"
            )
        public = _public_row(row)
        if row.get("classification") == CLASS_DETERMINISTIC:
            auto_rows.append(
                {
                    **public,
                    "proposal_status": "PROPOSED_NOT_APPLIED",
                }
            )
        if _needs_review(row):
            review_rows.append({**public, "review_reason": _review_reason(row)})
    package = {
        "schema_version": IDENTITY_REVIEW_PACKAGE_SCHEMA,
        "run_id": report["run_id"],
        "resolution_report_logical_sha256": report["logical_content_sha256"],
        "resolution_report_file_sha256": resolution_report_file_sha256,
        "kind": "IDENTITY_REVIEW_NOT_APPLY",
        "production_write_performed": False,
        "photo_person_created": False,
        "biography_fields_included": False,
        "review_rows": sorted(review_rows, key=lambda item: item["mention_key"]),
        "auto_resolution_proposals": sorted(
            auto_rows, key=lambda item: item["mention_key"]
        ),
    }
    stamp_generated_artifact(package, created_at=created_at)
    _reject_forbidden(package, "identity review package")
    validate_artifact(package)
    return package
