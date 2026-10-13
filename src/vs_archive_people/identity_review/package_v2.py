"""Build a v2 identity-review queue from resolution-report-v2.

Every resolution becomes one pending review row, including a deterministic
match to an existing production person. ``identity_is_production`` records
that match. It does not record human approval, and this module does not
authorize an apply or write production data.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import (
    IDENTITY_REVIEW_PACKAGE_V2_SCHEMA,
    RESOLUTION_REPORT_SCHEMA,
    RESOLUTION_REPORT_V2_SCHEMA,
    ArtifactError,
    RunIdMismatchError,
    UnsupportedSchemaVersionError,
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

COPIED_FIELDS = (
    "mention_key",
    "mention_fingerprint",
    "archive_item_id",
    "evidence_source",
    "source_text_sha256",
    "exact_mention",
    "start_offset",
    "end_offset",
    "evidence_snippet",
    "occurrence_index",
    "name_surface",
    "name_start_offset",
    "name_end_offset",
    "title_surface",
    "title_start_offset",
    "title_end_offset",
    "surface_kind",
    "detector_ids",
    "known_name_match_quality",
    "classification",
    "matched_person_ids",
    "matched_person_name",
    "matched_via",
    "matched_alias_id",
    "stable_keys",
    "identity_is_production",
)

_REVIEW_REASONS = {
    CLASS_DETERMINISTIC: "deterministic_existing_person_pending_review",
    CLASS_AMBIGUOUS: "ambiguous_existing_person",
    CLASS_NEW: "new_person_candidate",
}

_LIST_FIELDS = ("detector_ids", "matched_person_ids", "stable_keys")


def _require_sha256(value: str | None, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in value)
    ):
        raise ArtifactError(f"{label} must be a SHA-256 hex digest")


def _require_consistency(row: dict) -> None:
    """Refuse a verified row whose match fields disagree with its class."""
    if row.get("review_status") != "PENDING":
        raise ArtifactError("resolution review_status must be PENDING")
    classification = row.get("classification")
    identity = row.get("identity_is_production")
    person_ids = row.get("matched_person_ids")
    if not isinstance(person_ids, list):
        raise ArtifactError("matched_person_ids must be a list")
    name = row.get("matched_person_name")
    via = row.get("matched_via")
    alias_id = row.get("matched_alias_id")
    unique_fields_clear = name is None and via is None and alias_id is None
    if classification == CLASS_DETERMINISTIC:
        if identity is not True:
            raise ArtifactError(
                "DETERMINISTIC_EXISTING_PERSON requires identity_is_production true"
            )
        if len(person_ids) != 1:
            raise ArtifactError(
                "DETERMINISTIC_EXISTING_PERSON requires exactly one matched person"
            )
        if not isinstance(name, str) or not name:
            raise ArtifactError(
                "DETERMINISTIC_EXISTING_PERSON requires a matched person name"
            )
        if via == "person_alias":
            if isinstance(alias_id, bool) or not isinstance(alias_id, int):
                raise ArtifactError(
                    "a person_alias match requires an integer matched_alias_id"
                )
        elif via == "canonical_name":
            if alias_id is not None:
                raise ArtifactError(
                    "a canonical_name match requires matched_alias_id null"
                )
        else:
            raise ArtifactError(
                "DETERMINISTIC_EXISTING_PERSON requires matched_via "
                "canonical_name or person_alias"
            )
        return
    if classification == CLASS_AMBIGUOUS:
        if identity is not False:
            raise ArtifactError("AMBIGUOUS requires identity_is_production false")
        if len(person_ids) < 2:
            raise ArtifactError("AMBIGUOUS requires at least two matched people")
        if not unique_fields_clear:
            raise ArtifactError(
                "AMBIGUOUS requires null matched name, via, and alias id"
            )
        return
    if classification == CLASS_NEW:
        if identity is not False:
            raise ArtifactError(
                "NEW_PERSON_CANDIDATE requires identity_is_production false"
            )
        if person_ids:
            raise ArtifactError("NEW_PERSON_CANDIDATE requires no matched people")
        if not unique_fields_clear:
            raise ArtifactError(
                "NEW_PERSON_CANDIDATE requires null matched name, via, and alias id"
            )
        return
    raise ArtifactError(f"unsupported v2 classification {classification!r}")


def _review_row(resolution: dict) -> dict:
    _require_consistency(resolution)
    row = {field: resolution[field] for field in COPIED_FIELDS}
    for field in _LIST_FIELDS:
        row[field] = list(resolution[field])
    row["review_status"] = "PENDING"
    row["review_reason"] = _REVIEW_REASONS[resolution["classification"]]
    return row


def build_review_package_v2(
    resolution_report: dict,
    *,
    run_id: str,
    resolution_report_file_sha256: str,
    created_at: str | None = None,
) -> dict:
    """Publish a pending identity-review-package-v2 for one verified report."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ArtifactError("run_id is required")
    _require_sha256(resolution_report_file_sha256, "resolution_report_file_sha256")
    document = require_object(resolution_report, "resolution report")
    schema_version = document.get("schema_version")
    if schema_version != RESOLUTION_REPORT_V2_SCHEMA:
        if schema_version == RESOLUTION_REPORT_SCHEMA:
            raise UnsupportedSchemaVersionError(
                "review-v2 reads resolution-report-v2; "
                "resolution-report-v1 requires review-package"
            )
        raise UnsupportedSchemaVersionError(
            f"review-v2 requires schema_version {RESOLUTION_REPORT_V2_SCHEMA}"
        )
    report = verify_generated_artifact(
        document,
        RESOLUTION_REPORT_V2_SCHEMA,
        "resolution report",
    )
    if report.get("run_id") != run_id:
        raise RunIdMismatchError(
            f"resolution report run_id {report.get('run_id')!r} does not match {run_id!r}"
        )
    resolutions = report.get("resolutions")
    if not isinstance(resolutions, list):
        raise ArtifactError("resolution report resolutions must be a list")
    rows = [require_object(item, "resolution") for item in resolutions]
    keys = [row.get("mention_key") for row in rows]
    if any(not isinstance(key, str) or not key for key in keys):
        raise ArtifactError("every resolution needs a mention_key")
    if len(keys) != len(set(keys)):
        raise ArtifactError("resolution report contains a duplicate mention_key")
    fingerprints = [row.get("mention_fingerprint") for row in rows]
    if any(not isinstance(value, str) or not value for value in fingerprints):
        raise ArtifactError("every resolution needs a mention_fingerprint")
    if len(fingerprints) != len(set(fingerprints)):
        raise ArtifactError(
            "resolution report contains a duplicate mention_fingerprint"
        )
    ordered = sorted(rows, key=lambda row: int(str(row["mention_key"])[1:]))
    review_rows = [_review_row(row) for row in ordered]
    package = {
        "schema_version": IDENTITY_REVIEW_PACKAGE_V2_SCHEMA,
        "run_id": report["run_id"],
        "resolution_report_logical_sha256": report["logical_content_sha256"],
        "resolution_report_file_sha256": resolution_report_file_sha256,
        "kind": "IDENTITY_REVIEW_NOT_APPLY",
        "production_write_performed": False,
        "photo_person_created": False,
        "biography_fields_included": False,
        "human_approval_performed": False,
        "apply_authorized": False,
        "review_rows": review_rows,
    }
    stamp_generated_artifact(package, created_at=created_at)
    validate_artifact(package)
    return package
