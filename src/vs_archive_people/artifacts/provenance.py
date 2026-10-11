"""Provenance stamped on artifacts this package generates.

Logical identity excludes publication metadata and audit-only file hashes:

- ``artifact_created_at``
- ``producer``
- ``logical_content_sha256``
- any top-level field whose name ends in ``_file_sha256``

Fields that end in ``_logical_sha256`` stay inside the logical body. They are
the semantic dependency on a generated parent. A parent file hash is recorded
for audit and does not change the child logical hash.

Checkpoint ``input_file_hashes`` and ``output_file_hashes`` do not use that
suffix. They are part of the checkpoint's logical content because a checkpoint
is the exact-file record of a run step.
"""

from __future__ import annotations

from datetime import datetime

from vs_archive_people import __version__
from vs_archive_people.artifacts.io import ArtifactError, sha256_json

PRODUCER_NAME = "vs-archive-people"
PROVENANCE_FIELDS = frozenset(
    {"artifact_created_at", "producer", "logical_content_sha256"}
)
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def excluded_from_logical_hash(key: str) -> bool:
    """Publication metadata and audit-only parent file hashes."""
    return key in PROVENANCE_FIELDS or key.endswith("_file_sha256")


def utc_now_iso() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def require_utc_timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ArtifactError("artifact_created_at must be a string")
    try:
        parsed = datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise ArtifactError(
            "artifact_created_at must be a real UTC time YYYY-MM-DDTHH:MM:SSZ"
        ) from exc
    if parsed.strftime(_TIMESTAMP_FORMAT) != value:
        raise ArtifactError(
            "artifact_created_at must be a real UTC time YYYY-MM-DDTHH:MM:SSZ"
        )
    return value


def logical_body(document: dict) -> dict:
    return {
        key: value
        for key, value in document.items()
        if not excluded_from_logical_hash(key)
    }


def stamp_generated_artifact(document: dict, *, created_at: str | None = None) -> dict:
    """Attach provenance and the logical content hash. Returns the same dict."""
    when = utc_now_iso() if created_at is None else created_at
    document["artifact_created_at"] = require_utc_timestamp(when)
    document["producer"] = {"name": PRODUCER_NAME, "version": __version__}
    document["logical_content_sha256"] = sha256_json(logical_body(document))
    return document


def verify_generated_artifact(document: dict, expected_schema: str, label: str) -> dict:
    """Validate a generated artifact and require its stored logical hash to match."""
    from vs_archive_people.artifacts.io import LogicalHashMismatchError, require_schema

    require_schema(document, expected_schema, label)
    require_utc_timestamp(document.get("artifact_created_at"))
    stored = document.get("logical_content_sha256")
    actual = sha256_json(logical_body(document))
    if stored != actual:
        raise LogicalHashMismatchError(
            f"{label} logical_content_sha256 does not match its logical content"
        )
    return document
