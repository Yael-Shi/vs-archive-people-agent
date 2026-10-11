"""Run checkpoints.

A run starts at discover and then moves one step at a time through
freeze-evidence, resolve, and review. Every input hash already stored for the
run must be supplied again unchanged. schema_versions stay the versions
recorded when the run started. Writing the checkpoint file is the caller's
job and must go through atomic publication of a new path.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import (
    RUN_CHECKPOINT_SCHEMA,
    ArtifactError,
    InputHashChangedError,
    MissingBoundInputError,
    UnsupportedSchemaVersionError,
    require_object,
)
from vs_archive_people.artifacts.provenance import (
    stamp_generated_artifact,
    verify_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import (
    published_schema_versions,
    step_chain,
    validate_artifact,
)

__all__ = [
    "InputHashChangedError",
    "MissingBoundInputError",
    "UnsupportedSchemaVersionError",
]


def _hash_map(value: object, label: str) -> dict[str, str]:
    if value is None:
        return {}
    raw = require_object(value, label)
    cleaned: dict[str, str] = {}
    for key, digest in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ArtifactError(f"{label} keys must be non-empty strings")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in digest)
        ):
            raise ArtifactError(f"{label} {key} must be a SHA-256 hex digest")
        cleaned[key] = digest.lower()
    return cleaned


def _require_history(completed: object) -> list[str]:
    chain = step_chain()
    if not isinstance(completed, list) or any(
        not isinstance(item, str) for item in completed
    ):
        raise ArtifactError("completed_steps must be a list of strings")
    if len(completed) != len(set(completed)):
        raise ArtifactError("completed_steps contains a duplicate")
    if not completed:
        raise ArtifactError("completed_steps is empty")
    if list(completed) != list(chain[: len(completed)]):
        raise ArtifactError("completed_steps must follow " + " -> ".join(chain))
    return completed


def require_resumable_checkpoint(document: dict) -> dict[str, str]:
    """Return the recorded schema map when this package can continue the run.

    A checkpoint keeps the schema versions it started with. This package does
    not rewrite them and does not migrate an older map. A map that differs
    from the installed contracts is ``UNSUPPORTED_SCHEMA_VERSION``.
    """
    version = document.get("schema_version")
    if version != RUN_CHECKPOINT_SCHEMA:
        if isinstance(version, str) and version.startswith("run-checkpoint-"):
            raise UnsupportedSchemaVersionError(
                "UNSUPPORTED_SCHEMA_VERSION: this package cannot resume "
                f"checkpoint schema_version {version!r}"
            )
        raise ArtifactError(
            f"checkpoint schema_version must be {RUN_CHECKPOINT_SCHEMA}"
        )
    recorded = document.get("schema_versions")
    if not isinstance(recorded, dict) or any(
        not isinstance(key, str) for key in recorded
    ):
        raise ArtifactError("checkpoint schema_versions must be an object")
    if any(not isinstance(value, str) for value in recorded.values()):
        raise ArtifactError("checkpoint schema_versions values must be strings")
    expected = published_schema_versions()
    if recorded != expected:
        raise UnsupportedSchemaVersionError(
            "UNSUPPORTED_SCHEMA_VERSION: checkpoint schema_versions are not the "
            "contracts this package can resume"
        )
    return dict(recorded)


def assert_inputs_unchanged(checkpoint: dict, input_hashes: dict[str, str]) -> None:
    document = require_object(checkpoint, "checkpoint")
    require_resumable_checkpoint(document)
    verify_generated_artifact(document, RUN_CHECKPOINT_SCHEMA, "checkpoint")
    recorded = _hash_map(document.get("input_file_hashes"), "input_file_hashes")
    proposed = _hash_map(input_hashes, "input hashes")
    missing = sorted(set(recorded) - set(proposed))
    if missing:
        raise MissingBoundInputError(
            "previously bound inputs were not provided: " + ", ".join(missing)
        )
    for name, digest in recorded.items():
        if proposed[name] != digest:
            raise InputHashChangedError(
                f"input artifact {name} changed for run {document.get('run_id')}"
            )


def initial_checkpoint(
    *,
    run_id: str,
    step: str,
    input_hashes: dict[str, str],
    output_hashes: dict[str, str],
    processed_items: list[dict] | None = None,
    created_at: str | None = None,
) -> dict:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ArtifactError("run_id is required")
    chain = step_chain()
    if step != chain[0]:
        raise ArtifactError(f"a run must start at {chain[0]}")
    checkpoint = {
        "schema_version": RUN_CHECKPOINT_SCHEMA,
        "run_id": run_id,
        "schema_versions": published_schema_versions(),
        "input_file_hashes": _hash_map(input_hashes, "input hashes"),
        "completed_steps": [step],
        "output_file_hashes": _hash_map(output_hashes, "output hashes"),
        "processed_items": _merge_processed([], list(processed_items or [])),
    }
    stamp_generated_artifact(checkpoint, created_at=created_at)
    validate_artifact(checkpoint)
    return checkpoint


def append_step(
    prior: dict,
    *,
    step: str,
    input_hashes: dict[str, str],
    output_hashes: dict[str, str],
    processed_items: list[dict] | None = None,
    created_at: str | None = None,
) -> dict:
    document = require_object(prior, "checkpoint")
    schema_versions = require_resumable_checkpoint(document)
    verify_generated_artifact(document, RUN_CHECKPOINT_SCHEMA, "checkpoint")
    completed = _require_history(document.get("completed_steps"))
    chain = step_chain()
    if len(completed) >= len(chain):
        raise ArtifactError("run steps are already complete")
    expected = chain[len(completed)]
    if step != expected:
        if step in completed:
            raise ArtifactError(f"step {step} is already recorded")
        raise ArtifactError(f"next step must be {expected}")
    assert_inputs_unchanged(document, input_hashes)
    merged_inputs = _hash_map(document.get("input_file_hashes"), "input_file_hashes")
    merged_inputs.update(_hash_map(input_hashes, "input hashes"))
    merged_outputs = _hash_map(document.get("output_file_hashes"), "output_file_hashes")
    for name, digest in _hash_map(output_hashes, "output hashes").items():
        if name in merged_outputs and merged_outputs[name] != digest:
            raise ArtifactError(
                f"output artifact {name} is already bound to another hash"
            )
        merged_outputs[name] = digest
    items = list(document.get("processed_items") or [])
    if processed_items is not None:
        items = _merge_processed(items, processed_items)
    checkpoint = {
        "schema_version": RUN_CHECKPOINT_SCHEMA,
        "run_id": document["run_id"],
        "schema_versions": schema_versions,
        "input_file_hashes": merged_inputs,
        "completed_steps": [*completed, step],
        "output_file_hashes": merged_outputs,
        "processed_items": items,
    }
    stamp_generated_artifact(checkpoint, created_at=created_at)
    validate_artifact(checkpoint)
    return checkpoint


def _merge_processed(existing: list[dict], updates: list[dict]) -> list[dict]:
    by_id: dict[int, str] = {}
    ordered: list[int] = []
    for row in [*existing, *updates]:
        item = require_object(row, "processed item")
        item_id = item.get("archive_item_id")
        digest = item.get("displayed_text_sha256")
        if isinstance(item_id, bool) or not isinstance(item_id, int):
            raise ArtifactError("processed archive_item_id must be an integer")
        if not isinstance(digest, str) or not digest:
            raise ArtifactError("processed displayed_text_sha256 is required")
        if item_id not in by_id:
            ordered.append(item_id)
        elif by_id[item_id] != digest:
            raise ArtifactError(
                f"processed archive_item_id {item_id} is bound to two displayed-text hashes"
            )
        by_id[item_id] = digest
    return [
        {"archive_item_id": item_id, "displayed_text_sha256": by_id[item_id]}
        for item_id in sorted(ordered)
    ]


def processed_items_from_evidence(package: dict) -> list[dict]:
    rows = [
        {
            "archive_item_id": item["archive_item_id"],
            "displayed_text_sha256": item["displayed_text_sha256"],
        }
        for item in package.get("items", [])
    ]
    return _merge_processed([], rows)
