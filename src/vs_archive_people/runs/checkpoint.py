"""Run checkpoints.

A run starts at discover and then moves one step at a time through
freeze-evidence, resolve, and review. Every input hash already stored for the
run must be supplied again unchanged. schema_versions stay the versions
recorded when the run started. Writing the checkpoint file is the caller's
job and must go through atomic publication of a new path.

``run-checkpoint-v1`` and ``run-checkpoint-v2`` use the same step names. A run
keeps the generation it started with. Completing ``review`` means the identity
review package for that generation was published. It does not mean a person
approved the identities, and it does not authorize an apply.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import (
    RUN_CHECKPOINT_SCHEMA,
    RUN_CHECKPOINT_V2_SCHEMA,
    ArtifactError,
    IncompletePriorCheckpointError,
    InputHashChangedError,
    MissingBoundInputError,
    RunIdMismatchError,
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

_CHECKPOINT_SCHEMAS = frozenset({RUN_CHECKPOINT_SCHEMA, RUN_CHECKPOINT_V2_SCHEMA})

# Logical steps whose recorded artifact chooses the run generation.
STEP_OUTPUT_ROLES = {
    "freeze-evidence": "evidence_package",
    "resolve": "resolution_report",
    "review": "identity_review_package",
}


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


def _require_checkpoint_schema(checkpoint_schema: str) -> str:
    if checkpoint_schema not in _CHECKPOINT_SCHEMAS:
        raise UnsupportedSchemaVersionError(
            f"unsupported checkpoint schema {checkpoint_schema!r}"
        )
    return checkpoint_schema


def require_resumable_checkpoint(
    document: dict, *, checkpoint_schema: str = RUN_CHECKPOINT_SCHEMA
) -> dict[str, str]:
    """Return the recorded schema map when this generation can continue the run.

    A checkpoint keeps the schema versions it started with. This package does
    not rewrite them and does not migrate one generation into the other. A map
    that differs from the installed contract for ``checkpoint_schema`` is
    ``UNSUPPORTED_SCHEMA_VERSION``.
    """
    expected_schema = _require_checkpoint_schema(checkpoint_schema)
    version = document.get("schema_version")
    if version != expected_schema:
        if isinstance(version, str) and (
            version in _CHECKPOINT_SCHEMAS or version.startswith("run-checkpoint-")
        ):
            raise UnsupportedSchemaVersionError(
                f"checkpoint schema_version {version!r} cannot resume as "
                f"{expected_schema!r}"
            )
        raise ArtifactError(f"checkpoint schema_version must be {expected_schema}")
    recorded = document.get("schema_versions")
    if not isinstance(recorded, dict) or any(
        not isinstance(key, str) for key in recorded
    ):
        raise ArtifactError("checkpoint schema_versions must be an object")
    if any(not isinstance(value, str) for value in recorded.values()):
        raise ArtifactError("checkpoint schema_versions values must be strings")
    expected = published_schema_versions(expected_schema)
    if recorded != expected:
        raise UnsupportedSchemaVersionError(
            "UNSUPPORTED_SCHEMA_VERSION: checkpoint schema_versions are not the "
            f"contracts this package can resume for {expected_schema}"
        )
    return dict(recorded)


def require_completed_prior_checkpoint(document: dict) -> dict:
    """Accept a finished v1 or v2 checkpoint as discovery memory for a new run.

    The prior run is not resumed, and its generation is not copied onto the
    new run. An unfinished checkpoint of either generation is
    ``INCOMPLETE_PRIOR_CHECKPOINT``.
    """
    version = document.get("schema_version")
    if version not in _CHECKPOINT_SCHEMAS:
        if isinstance(version, str) and version.startswith("run-checkpoint-"):
            raise UnsupportedSchemaVersionError(
                "UNSUPPORTED_SCHEMA_VERSION: this package cannot use "
                f"checkpoint schema_version {version!r} as discovery memory"
            )
        raise ArtifactError(
            "prior checkpoint schema_version must be "
            f"{RUN_CHECKPOINT_SCHEMA} or {RUN_CHECKPOINT_V2_SCHEMA}"
        )
    require_resumable_checkpoint(document, checkpoint_schema=version)
    verified = verify_generated_artifact(document, version, "checkpoint")
    if list(verified.get("completed_steps")) != list(step_chain()):
        raise IncompletePriorCheckpointError(
            "prior checkpoint is same-run resume state and cannot seed discovery "
            "until review is complete"
        )
    return verified


def _require_step_artifact(
    *,
    step: str,
    run_id: str,
    checkpoint_schema: str,
    output_hashes: dict[str, str],
    bound_artifacts: dict[str, dict] | None,
) -> None:
    """Verify the generation artifact for a step that records one.

    v1 may omit the artifact and keep the historical hash-only record. v2
    requires the verified artifact and its output hash. A supplied artifact
    of the other generation is ``UNSUPPORTED_SCHEMA_VERSION``.
    """
    role = STEP_OUTPUT_ROLES.get(step)
    if role is None:
        return
    expected = published_schema_versions(checkpoint_schema)[role]
    artifact = None if bound_artifacts is None else bound_artifacts.get(role)
    if checkpoint_schema == RUN_CHECKPOINT_V2_SCHEMA:
        if artifact is None:
            raise ArtifactError(f"{step} requires a verified {expected} artifact")
        if role not in output_hashes:
            raise ArtifactError(f"{step} must record output hash {role}")
    if artifact is None:
        return
    document = require_object(artifact, role)
    actual = document.get("schema_version")
    if actual != expected:
        raise UnsupportedSchemaVersionError(
            f"{role} schema_version {actual!r} does not match "
            f"{checkpoint_schema} contract {expected!r}"
        )
    verify_generated_artifact(document, expected, role)
    if document.get("run_id") != run_id:
        raise RunIdMismatchError(
            f"{role} run_id does not match checkpoint run {run_id!r}"
        )


def assert_inputs_unchanged(
    checkpoint: dict,
    input_hashes: dict[str, str],
    *,
    checkpoint_schema: str = RUN_CHECKPOINT_SCHEMA,
) -> None:
    document = require_object(checkpoint, "checkpoint")
    require_resumable_checkpoint(document, checkpoint_schema=checkpoint_schema)
    verify_generated_artifact(document, checkpoint_schema, "checkpoint")
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
    checkpoint_schema: str = RUN_CHECKPOINT_SCHEMA,
) -> dict:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ArtifactError("run_id is required")
    schema = _require_checkpoint_schema(checkpoint_schema)
    chain = step_chain()
    if step != chain[0]:
        raise ArtifactError(f"a run must start at {chain[0]}")
    outputs = _hash_map(output_hashes, "output hashes")
    _require_step_artifact(
        step=step,
        run_id=run_id,
        checkpoint_schema=schema,
        output_hashes=outputs,
        bound_artifacts=None,
    )
    checkpoint = {
        "schema_version": schema,
        "run_id": run_id,
        "schema_versions": published_schema_versions(schema),
        "input_file_hashes": _hash_map(input_hashes, "input hashes"),
        "completed_steps": [step],
        "output_file_hashes": outputs,
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
    checkpoint_schema: str = RUN_CHECKPOINT_SCHEMA,
    bound_artifacts: dict[str, dict] | None = None,
) -> dict:
    document = require_object(prior, "checkpoint")
    schema = _require_checkpoint_schema(checkpoint_schema)
    schema_versions = require_resumable_checkpoint(document, checkpoint_schema=schema)
    verify_generated_artifact(document, schema, "checkpoint")
    completed = _require_history(document.get("completed_steps"))
    chain = step_chain()
    if len(completed) >= len(chain):
        raise ArtifactError("run steps are already complete")
    expected = chain[len(completed)]
    if step != expected:
        if step in completed:
            raise ArtifactError(f"step {step} is already recorded")
        raise ArtifactError(f"next step must be {expected}")
    assert_inputs_unchanged(document, input_hashes, checkpoint_schema=schema)
    outputs = _hash_map(output_hashes, "output hashes")
    _require_step_artifact(
        step=step,
        run_id=document["run_id"],
        checkpoint_schema=schema,
        output_hashes=outputs,
        bound_artifacts=bound_artifacts,
    )
    merged_inputs = _hash_map(document.get("input_file_hashes"), "input_file_hashes")
    merged_inputs.update(_hash_map(input_hashes, "input hashes"))
    merged_outputs = _hash_map(document.get("output_file_hashes"), "output_file_hashes")
    for name, digest in outputs.items():
        if name in merged_outputs and merged_outputs[name] != digest:
            raise ArtifactError(
                f"output artifact {name} is already bound to another hash"
            )
        merged_outputs[name] = digest
    items = list(document.get("processed_items") or [])
    if processed_items is not None:
        items = _merge_processed(items, processed_items)
    checkpoint = {
        "schema_version": schema,
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
