"""Atomic JSON artifacts and content hashes.

Publication refuses an existing path. A complete temporary file in the same
directory is fsynced, then linked onto the destination. ``link`` fails if that
path already exists, so a later publisher cannot replace it. The temporary
name is removed after success or failure. The parent directory is fsynced
after the link.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

DOCUMENT_TEXT_SNAPSHOT_SCHEMA = "document-text-snapshot-v1"
IDENTITY_SNAPSHOT_SCHEMA = "identity-snapshot-v1"
EVIDENCE_PACKAGE_SCHEMA = "evidence-package-v1"
EVIDENCE_PACKAGE_V2_SCHEMA = "evidence-package-v2"
RESOLUTION_REPORT_SCHEMA = "resolution-report-v1"
RESOLUTION_REPORT_V2_SCHEMA = "resolution-report-v2"
IDENTITY_REVIEW_PACKAGE_SCHEMA = "identity-review-package-v1"
IDENTITY_REVIEW_PACKAGE_V2_SCHEMA = "identity-review-package-v2"
RUN_CHECKPOINT_SCHEMA = "run-checkpoint-v1"
RUN_CHECKPOINT_V2_SCHEMA = "run-checkpoint-v2"
DISCOVERY_SELECTION_SCHEMA = "discovery-selection-v1"
MENTION_PROPOSALS_SCHEMA = "mention-proposals-v1"
MENTION_PROPOSAL_DIAGNOSTICS_SCHEMA = "mention-proposal-diagnostics-v1"


class ArtifactError(ValueError):
    """A snapshot, proposal, or artifact failed a contract check."""

    error_code = "MALFORMED_ARTIFACT"


class ArtifactExistsError(ArtifactError):
    """Refusing to overwrite an existing artifact."""

    error_code = "ARTIFACT_EXISTS"


class ArtifactReadError(ArtifactError):
    """An input artifact could not be read from the filesystem."""

    error_code = "ARTIFACT_READ_ERROR"


class InputHashChangedError(ArtifactError):
    """An input already recorded for this run no longer matches."""

    error_code = "INPUT_HASH_CHANGED"


class MissingBoundInputError(ArtifactError):
    """A later step omitted an input hash the run already recorded."""

    error_code = "MISSING_BOUND_INPUT"


class UnsupportedSchemaVersionError(ArtifactError):
    """This package cannot resume the schema versions recorded on a run."""

    error_code = "UNSUPPORTED_SCHEMA_VERSION"


class LogicalHashMismatchError(ArtifactError):
    """A generated artifact's stored logical hash does not match its content."""

    error_code = "LOGICAL_HASH_MISMATCH"


class RunIdMismatchError(ArtifactError):
    """Run-scoped artifacts in one pipeline invocation do not share a run_id."""

    error_code = "RUN_ID_MISMATCH"


class IncompletePriorCheckpointError(ArtifactError):
    """A checkpoint is resumable for its own run but cannot seed a new one."""

    error_code = "INCOMPLETE_PRIOR_CHECKPOINT"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        handle = Path(path).open("rb")
    except FileNotFoundError as exc:
        raise ArtifactReadError(f"artifact file not found: {path}") from exc
    except OSError as exc:
        raise ArtifactReadError(f"unable to read artifact file: {path}") from exc
    with handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(payload: Any) -> str:
    return sha256_bytes(canonical_json_bytes(payload))


def read_json(path: Path) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ArtifactReadError(f"artifact file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ArtifactReadError(f"artifact file is not UTF-8 text: {path}") from exc
    except OSError as exc:
        raise ArtifactReadError(f"unable to read artifact file: {path}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"artifact file is not valid JSON: {path}") from exc


def require_object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ArtifactError(f"{label} must be a JSON object")
    return value


def require_schema(document: dict, expected: str, label: str) -> None:
    if document.get("schema_version") != expected:
        raise ArtifactError(f"{label} schema_version must be {expected}")
    from vs_archive_people.artifacts.schema_validate import validate_artifact

    validate_artifact(document)


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def write_json_atomic(path: Path, payload: Any) -> str:
    """Write JSON exactly once. Return the SHA-256 of the published bytes.

    The no-overwrite guarantee is ``os.link`` into the destination. That call
    fails when the path exists. It does not depend on an earlier ``exists``
    check.
    """
    if isinstance(payload, dict) and "schema_version" in payload:
        from vs_archive_people.artifacts.schema_validate import validate_artifact

        validate_artifact(payload)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ArtifactExistsError(
                f"refusing to overwrite existing artifact: {destination}"
            ) from exc
        _fsync_directory(destination.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return sha256_file(destination)
