"""Validate artifacts with Draft 2020-12 schemas shipped in this package.

Schema documents are loaded through ``importlib.resources``. Callers do not
need the repository checkout or a particular working directory.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource

from vs_archive_people.artifacts.io import ArtifactError

SCHEMA_FILES = {
    "document-text-snapshot-v1": "document_text_snapshot.v1.json",
    "discovery-selection-v1": "discovery_selection.v1.json",
    "evidence-package-v1": "evidence_package.v1.json",
    "identity-snapshot-v1": "identity_snapshot.v1.json",
    "resolution-report-v1": "resolution_report.v1.json",
    "identity-review-package-v1": "identity_review_package.v1.json",
    "run-checkpoint-v1": "run_checkpoint.v1.json",
    "mention-proposals-v1": "mention_proposals.v1.json",
    "mention-proposal-diagnostics-v1": "mention_proposal_diagnostics.v1.json",
}

# This constant owns checkpoint progression. The JSON Schema enum only
# validates which step strings may be serialized.
RUN_STEPS = (
    "discover",
    "freeze-evidence",
    "resolve",
    "review",
)


def schema_resources():
    """Package resources for the schema documents. This is not a filesystem path."""
    from importlib.resources import files

    return files("vs_archive_people.schemas")


def require_valid_schema(document: dict, name: str) -> None:
    try:
        Draft202012Validator.check_schema(document)
    except SchemaError as exc:
        raise ArtifactError(
            f"schema file {name} is not a valid Draft 2020-12 schema: {exc.message}"
        ) from exc


def _schema_version_const(document: dict) -> str | None:
    properties = document.get("properties")
    if not isinstance(properties, dict):
        return None
    declaration = properties.get("schema_version")
    if not isinstance(declaration, dict):
        return None
    const = declaration.get("const")
    if not isinstance(const, str):
        return None
    return const


def index_schema_documents(files: dict[str, dict]) -> dict[str, dict]:
    """Index schema documents by ``$id`` and require the artifact-file map.

    ``SCHEMA_FILES`` is the authoritative filename for each artifact
    ``schema_version``. A shared schema such as ``producer.v1.json`` has no
    ``schema_version``. Duplicate ``$id`` values are refused.
    """
    by_id: dict[str, dict] = {}
    id_owner: dict[str, str] = {}
    for filename in sorted(files):
        document = files[filename]
        if not isinstance(document, dict):
            raise ArtifactError(f"schema file {filename} must be a JSON object")
        schema_id = document.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise ArtifactError(f"schema file {filename} is missing $id")
        previous = id_owner.get(schema_id)
        if previous is not None:
            raise ArtifactError(
                f"duplicate schema $id {schema_id!r} in {previous} and {filename}"
            )
        by_id[schema_id] = document
        id_owner[schema_id] = filename

    declared: dict[str, list[str]] = {}
    for filename in sorted(files):
        const = _schema_version_const(files[filename])
        if const is not None:
            declared.setdefault(const, []).append(filename)

    for schema_version, filename in SCHEMA_FILES.items():
        if filename not in files:
            raise ArtifactError(
                "mapped schema file "
                f"{filename} for schema_version {schema_version!r} is missing"
            )
        owners = declared.get(schema_version, [])
        if owners != [filename]:
            raise ArtifactError(
                f"schema_version {schema_version!r} must be declared by {filename}, "
                f"found {owners}"
            )
    return by_id


def _read_packaged_schemas() -> dict[str, dict]:
    files: dict[str, dict] = {}
    for resource in schema_resources().iterdir():
        if not resource.name.endswith(".json"):
            continue
        if resource.name in files:
            raise ArtifactError(f"duplicate schema filename {resource.name}")
        try:
            document = json.loads(resource.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ArtifactError(
                f"schema file {resource.name} is not JSON: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise ArtifactError(f"schema file {resource.name} must be a JSON object")
        require_valid_schema(document, resource.name)
        files[resource.name] = document
    return files


@lru_cache(maxsize=None)
def _schema_index() -> tuple[dict[str, dict], dict[str, dict]]:
    files = _read_packaged_schemas()
    return index_schema_documents(files), files


@lru_cache(maxsize=None)
def _registry() -> Registry:
    registry: Registry = Registry()
    by_id, _files = _schema_index()
    for schema_id, document in by_id.items():
        registry = registry.with_resource(schema_id, Resource.from_contents(document))
    return registry


@lru_cache(maxsize=None)
def load_schema(schema_version: str) -> dict:
    filename = SCHEMA_FILES.get(schema_version)
    if filename is None:
        raise ArtifactError(f"unknown schema_version {schema_version!r}")
    _by_id, files = _schema_index()
    document = files.get(filename)
    if document is None:
        raise ArtifactError(
            f"mapped schema file {filename} for schema_version {schema_version!r} is missing"
        )
    if _schema_version_const(document) != schema_version:
        raise ArtifactError(
            f"schema file {filename} does not declare schema_version {schema_version!r}"
        )
    return document


def published_schema_versions() -> dict[str, str]:
    """Version map stored on a checkpoint, taken from the checkpoint schema."""
    block = load_schema("run-checkpoint-v1")["properties"]["schema_versions"]
    versions = {}
    for name in block["required"]:
        versions[name] = block["properties"][name]["const"]
    return versions


def step_chain() -> tuple[str, ...]:
    """Return the run progression owned by ``RUN_STEPS``."""
    return RUN_STEPS


def validate_artifact(document: dict) -> None:
    if not isinstance(document, dict):
        raise ArtifactError("artifact must be a JSON object")
    version = document.get("schema_version")
    if not isinstance(version, str) or version not in SCHEMA_FILES:
        raise ArtifactError(f"unknown schema_version {version!r}")
    schema = load_schema(version)
    validator = Draft202012Validator(schema, registry=_registry())
    errors = sorted(validator.iter_errors(document), key=lambda item: item.json_path)
    if errors:
        first: ValidationError = errors[0]
        raise ArtifactError(f"{first.json_path}: {first.message}")
    if version == "mention-proposals-v1":
        from vs_archive_people.mention_proposals.constraints import (
            require_mention_proposal_constraints,
        )

        require_mention_proposal_constraints(document)


def iter_schema_documents() -> list[tuple[str, dict[str, Any]]]:
    by_id, _files = _schema_index()
    return sorted(by_id.items())
