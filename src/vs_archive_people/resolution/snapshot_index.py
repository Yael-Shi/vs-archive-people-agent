"""Index a production identity snapshot for exact-name lookup.

Person.name and PersonAlias.name are production names. Bindings are returned
with a match so a review row can show stable keys. They are not names and are
not matched against mention text.

PhotoPerson rows are counted and then discarded. ArchiveItemPerson rows are not
a name index. A row marked review_status=PENDING is local memory and is excluded.
Biography text is never copied into the index.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from vs_archive_people.artifacts.io import (
    IDENTITY_SNAPSHOT_SCHEMA,
    ArtifactError,
    require_object,
    require_schema,
)

PRODUCTION_MODELS = (
    "Person",
    "PersonAlias",
    "PersonRegistryImportBinding",
    "ArchiveItemPerson",
    "PhotoPerson",
)


def match_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())


@dataclass(frozen=True)
class IndexedPerson:
    person_id: int
    canonical_name: str
    stable_keys: tuple[str, ...]


@dataclass
class IdentityIndex:
    people: dict[int, IndexedPerson]
    names: dict[str, dict[int, str]]
    photo_person_rows_ignored: int = 0
    pending_rows_excluded: int = 0
    archive_item_person_rows_ignored: int = 0
    # Original accepted Person.name and PersonAlias.name strings, first-seen
    # order. Not normalized. Not an identity assignment.
    exact_names: list[str] = field(default_factory=list)
    _alias_ids: dict[tuple[int, str], int] = field(default_factory=dict)

    def lookup(self, exact_mention: str) -> list[tuple[int, str]]:
        """Return (person_id, matched_via) pairs for an exact normalized name."""
        key = match_key(exact_mention)
        if not key:
            return []
        hits = self.names.get(key, {})
        return sorted((person_id, via) for person_id, via in hits.items())

    def alias_id(self, person_id: int, exact_mention: str) -> int | None:
        return self._alias_ids.get((person_id, match_key(exact_mention)))


def _rows(snapshot: dict, model_name: str) -> list[dict]:
    models = snapshot.get("models")
    if not isinstance(models, dict) or model_name not in models:
        if model_name in {"ArchiveItemPerson", "PhotoPerson"}:
            return []
        raise ArtifactError(f"identity snapshot is missing models.{model_name}")
    model = require_object(models[model_name], model_name)
    rows = model.get("rows")
    if not isinstance(rows, list):
        raise ArtifactError(f"{model_name} rows must be a list")
    return [require_object(row, f"{model_name} row") for row in rows]


def _is_pending(row: dict) -> bool:
    status = row.get("review_status")
    return isinstance(status, str) and status.upper() == "PENDING"


def _person_id(row: dict, label: str) -> int:
    value = row.get("id") if label == "Person" else row.get("person_id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactError(f"{label} is missing an integer person id")
    return value


def _production_name(value: object, label: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(f"{label} is missing a non-empty name")
    key = match_key(value)
    if not key:
        raise ArtifactError(f"{label} normalizes to an empty name")
    return value, key


def _require_row_id(row: dict, label: str) -> int:
    value = row.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactError(f"{label}.id must be an integer")
    return value


def build_index(snapshot: dict) -> IdentityIndex:
    document = require_object(snapshot, "identity snapshot")
    require_schema(document, IDENTITY_SNAPSHOT_SCHEMA, "identity snapshot")
    index = IdentityIndex(people={}, names={})
    pending_person_ids: set[int] = set()
    seen_person_ids: set[int] = set()
    seen_exact_names: set[str] = set()

    def remember_exact(name: str) -> None:
        if name not in seen_exact_names:
            seen_exact_names.add(name)
            index.exact_names.append(name)

    for row in _rows(document, "Person"):
        person_id = _person_id(row, "Person")
        if person_id in seen_person_ids:
            raise ArtifactError(f"duplicate Person id {person_id}")
        seen_person_ids.add(person_id)
        if _is_pending(row):
            index.pending_rows_excluded += 1
            pending_person_ids.add(person_id)
            continue
        name, key = _production_name(row.get("name"), f"Person {person_id}")
        index.people[person_id] = IndexedPerson(
            person_id=person_id,
            canonical_name=name,
            stable_keys=(),
        )
        index.names.setdefault(key, {})
        index.names[key][person_id] = "canonical_name"
        remember_exact(name)

    known_ids = set(index.people)
    seen_alias_ids: set[int] = set()
    for row in _rows(document, "PersonAlias"):
        alias_id = _require_row_id(row, "PersonAlias")
        if alias_id in seen_alias_ids:
            raise ArtifactError(f"duplicate PersonAlias id {alias_id}")
        seen_alias_ids.add(alias_id)
        if _is_pending(row):
            index.pending_rows_excluded += 1
            continue
        person_id = _person_id(row, "PersonAlias")
        if person_id in pending_person_ids:
            index.pending_rows_excluded += 1
            continue
        if person_id not in known_ids:
            raise ArtifactError(
                f"PersonAlias refers to Person {person_id}, which is not in the production index"
            )
        _alias_name, key = _production_name(row.get("name"), f"PersonAlias {alias_id}")
        previous = index._alias_ids.get((person_id, key))
        if previous is not None and previous != alias_id:
            raise ArtifactError(
                f"Person {person_id} has conflicting alias ids for one normalized name"
            )
        index._alias_ids[(person_id, key)] = alias_id
        remember_exact(_alias_name)
        current = index.names.setdefault(key, {})
        # A canonical hit stays canonical. The alias id is still stored.
        # The same normalized alias on another person stays a second hit.
        current.setdefault(person_id, "person_alias")

    bindings: dict[int, list[str]] = {}
    seen_binding_ids: set[int] = set()
    stable_owner: dict[str, int] = {}
    for row in _rows(document, "PersonRegistryImportBinding"):
        if "id" in row:
            binding_id = _require_row_id(row, "PersonRegistryImportBinding")
            if binding_id in seen_binding_ids:
                raise ArtifactError(
                    f"duplicate PersonRegistryImportBinding id {binding_id}"
                )
            seen_binding_ids.add(binding_id)
        if _is_pending(row):
            index.pending_rows_excluded += 1
            continue
        person_id = _person_id(row, "PersonRegistryImportBinding")
        if person_id in pending_person_ids:
            index.pending_rows_excluded += 1
            continue
        if person_id not in known_ids:
            raise ArtifactError(
                f"PersonRegistryImportBinding refers to Person {person_id}, "
                "which is not in the production index"
            )
        stable_key = row.get("stable_key")
        if not isinstance(stable_key, str) or not stable_key.strip():
            raise ArtifactError("PersonRegistryImportBinding.stable_key is required")
        owner = stable_owner.get(stable_key)
        if owner is not None and owner != person_id:
            raise ArtifactError(
                f"stable_key {stable_key} is bound to Person {owner} and Person {person_id}"
            )
        stable_owner[stable_key] = person_id
        bindings.setdefault(person_id, [])
        if stable_key not in bindings[person_id]:
            bindings[person_id].append(stable_key)
    for person_id, keys in bindings.items():
        person = index.people[person_id]
        index.people[person_id] = IndexedPerson(
            person_id=person.person_id,
            canonical_name=person.canonical_name,
            stable_keys=tuple(sorted(keys)),
        )

    index.archive_item_person_rows_ignored = len(_rows(document, "ArchiveItemPerson"))
    index.photo_person_rows_ignored = len(_rows(document, "PhotoPerson"))
    return index
