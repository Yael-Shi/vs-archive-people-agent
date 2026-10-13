"""Classify evidence-package-v2 mentions against a production identity index.

The lookup string is the frozen ``name_surface``. ``exact_mention`` stays on
the row as review context. This module does not strip titles or Hebrew
proclitics, and it does not read detector provenance as an identity rule.

``NOT_A_PERSON`` is not a v2 outcome. Non-person candidates are already
absent from published evidence. Local same-run candidates are not an input:
they never count as production identity, and review v2 is not implemented.
Nothing here creates or updates a Person, alias, merge, or canonical name.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import (
    EVIDENCE_PACKAGE_SCHEMA,
    EVIDENCE_PACKAGE_V2_SCHEMA,
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
from vs_archive_people.resolution.snapshot_index import IdentityIndex

CARRIED_FIELDS = (
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
)


def _require_sha256(value: str | None, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in value)
    ):
        raise ArtifactError(f"{label} must be a SHA-256 hex digest")


def _lookup_surface(mention: dict) -> str:
    """Return the frozen name slice. Do not parse the outer mention."""
    surface = mention.get("name_surface")
    if not isinstance(surface, str) or not surface:
        raise ArtifactError("name_surface is required")
    return surface


def _classify_one(mention: dict, index: IdentityIndex) -> dict:
    surface = _lookup_surface(mention)
    hits = index.lookup(surface)
    person_ids = [person_id for person_id, _via in hits]
    if len(person_ids) == 1:
        classification = CLASS_DETERMINISTIC
        matched_via = hits[0][1]
    elif len(person_ids) > 1:
        classification = CLASS_AMBIGUOUS
        matched_via = None
    else:
        classification = CLASS_NEW
        matched_via = None

    matched_alias_id = None
    matched_name = None
    stable_keys: list[str] = []
    if len(person_ids) == 1:
        person = index.people[person_ids[0]]
        matched_name = person.canonical_name
        stable_keys = list(person.stable_keys)
        if matched_via == "person_alias":
            matched_alias_id = index.alias_id(person.person_id, surface)
    elif len(person_ids) > 1:
        stable_keys = sorted(
            {
                key
                for person_id in person_ids
                for key in index.people[person_id].stable_keys
            }
        )
    row = {field: mention[field] for field in CARRIED_FIELDS}
    row["detector_ids"] = list(mention["detector_ids"])
    row.update(
        {
            "classification": classification,
            "matched_person_ids": person_ids,
            "matched_person_name": matched_name,
            "matched_via": matched_via,
            "matched_alias_id": matched_alias_id,
            "stable_keys": stable_keys,
            "review_status": "PENDING",
            "identity_is_production": classification == CLASS_DETERMINISTIC,
        }
    )
    return row


def build_resolution_report_v2(
    evidence_package: dict,
    index: IdentityIndex,
    *,
    run_id: str,
    evidence_package_file_sha256: str,
    identity_snapshot_file_sha256: str,
    created_at: str | None = None,
) -> dict:
    """Publish a read-only resolution-report-v2 for one verified evidence package."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ArtifactError("run_id is required")
    _require_sha256(evidence_package_file_sha256, "evidence_package_file_sha256")
    _require_sha256(identity_snapshot_file_sha256, "identity_snapshot_file_sha256")
    document = require_object(evidence_package, "evidence package")
    schema_version = document.get("schema_version")
    if schema_version != EVIDENCE_PACKAGE_V2_SCHEMA:
        if schema_version == EVIDENCE_PACKAGE_SCHEMA:
            raise UnsupportedSchemaVersionError(
                "resolve-v2 reads evidence-package-v2; "
                "evidence-package-v1 requires resolve"
            )
        raise UnsupportedSchemaVersionError(
            f"resolve-v2 requires schema_version {EVIDENCE_PACKAGE_V2_SCHEMA}"
        )
    package = verify_generated_artifact(
        document,
        EVIDENCE_PACKAGE_V2_SCHEMA,
        "evidence package",
    )
    if package.get("run_id") != run_id:
        raise RunIdMismatchError(
            f"evidence package run_id {package.get('run_id')!r} does not match {run_id!r}"
        )
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
    resolutions = [_classify_one(mention, index) for mention in ordered]
    if len({row["mention_key"] for row in resolutions}) != len(resolutions):
        raise ArtifactError("resolution report would repeat a mention_key")
    report = {
        "schema_version": RESOLUTION_REPORT_V2_SCHEMA,
        "run_id": package["run_id"],
        "evidence_package_logical_sha256": package["logical_content_sha256"],
        "evidence_package_file_sha256": evidence_package_file_sha256,
        "identity_snapshot_file_sha256": identity_snapshot_file_sha256,
        "production_write_performed": False,
        "photo_person_rows_ignored": index.photo_person_rows_ignored,
        "photo_person_created": False,
        "resolutions": resolutions,
    }
    stamp_generated_artifact(report, created_at=created_at)
    validate_artifact(report)
    return report
