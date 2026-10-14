"""Compare a historical mention pilot with official mention proposals.

This is evaluation output. It does not call a provider, and it is not a
checkpoint step. Pilot rows are not ground truth. Occurrence identity is the
archive item, evidence source, and outer offsets.
"""

from __future__ import annotations

from dataclasses import dataclass

from vs_archive_people.artifacts.io import (
    DISCOVERY_SELECTION_SCHEMA,
    MENTION_PROPOSAL_DIAGNOSTICS_SCHEMA,
    MENTION_PROPOSALS_SCHEMA,
    MENTION_RUN_COMPARISON_V3_SCHEMA,
    ArtifactError,
    InputHashChangedError,
    RunIdMismatchError,
    UnsupportedSchemaVersionError,
    require_object,
    sha256_bytes,
)
from vs_archive_people.artifacts.provenance import (
    stamp_generated_artifact,
    verify_generated_artifact,
)
from vs_archive_people.evidence.text_bodies import (
    require_document_snapshot,
    select_text_bodies,
)
from vs_archive_people.find_mentions.registry import (
    KNOWN_NAME_DETECTOR_ID,
    SEMANTIC_DETECTOR_ID,
)
from vs_archive_people.mention_pilot.run import displayed_body_for_item

COMPARISON_KIND = "MENTION_RUN_COMPARISON_NOT_OPERATIONAL"
COMPARISON_COMMAND = "compare-mention-runs"
SINGLE_PILOT_KIND = "GEMINI_MENTION_PILOT_NOT_ARTIFACT"
SOURCE_EQUIVALENT = "SOURCE_EQUIVALENT"
SOURCE_DRIFT = "SOURCE_DRIFT"
SOURCE_UNKNOWN = "SOURCE_EQUIVALENCE_UNKNOWN"
BOTH_SAME_SPAN = "BOTH_SAME_SPAN"
BOTH_OVERLAP = "BOTH_OVERLAP_DIFFERENT_SPAN"
OFFICIAL_ONLY = "OFFICIAL_ONLY"
PILOT_ONLY = "PILOT_ONLY"
OFFICIAL_SOURCE_NONCOMPARABLE = "OFFICIAL_SOURCE_NONCOMPARABLE"
PILOT_SOURCE_NONCOMPARABLE = "PILOT_SOURCE_NONCOMPARABLE"
NON_COMPARABLE = "NON_COMPARABLE"
SOURCE_EQUIVALENCE_SEMANTICS = (
    "source_equivalence summarizes comparable archive_item_id and "
    "evidence_source pairs only. A source role present on only one side is "
    "non-comparable. It does not change this summary and does not mean the "
    "comparable bodies are unknown."
)
DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
DIAGNOSTIC_SECTIONS = ("rejected", "suppressed", "conflicts")
RECOVERED_CORPUS_IDS = (22, 156, 182, 184, 268, 273, 288, 292, 297, 323)
CORPUS_RECOVERY_STATEMENT = (
    "A 10-item historical Gemini mention-pilot corpus was recovered from the "
    "refined pilot result. No additional recoverable pilot corpus artifact was "
    "found in the repository or the broader filesystem search. This does not "
    "establish that no other corpus ever existed."
)


@dataclass(frozen=True)
class _PilotMention:
    archive_item_id: int
    evidence_source: str
    comparable: bool
    start: int | None
    end: int | None
    exact_mention: str | None
    name_surface: str | None
    name_known: bool
    title_surface: str | None
    title_known: bool
    surface_kind: str | None
    surface_kind_known: bool
    reason: str | None


@dataclass(frozen=True)
class _OfficialMention:
    archive_item_id: int
    evidence_source: str
    start: int
    end: int
    exact_mention: str
    name_surface: str
    title_surface: str | None
    surface_kind: str | None
    detector_ids: tuple[str, ...]
    known_name_match_quality: str | None


@dataclass(frozen=True)
class _SourceBody:
    archive_item_id: int
    evidence_source: str
    pilot_body_sha256: str | None
    input_order: int


def build_mention_run_comparison(
    pilot: object,
    proposals: object,
    *,
    pilot_file_sha256: str,
    mention_proposals_file_sha256: str,
    diagnostics: object | None = None,
    diagnostics_file_sha256: str | None = None,
    document_snapshot: object | None = None,
    document_snapshot_file_sha256: str | None = None,
    discovery: object | None = None,
    discovery_file_sha256: str | None = None,
    created_at: str | None = None,
) -> dict:
    """Compare stored pilot output with a published mention-proposals-v1 file."""
    _require_sha256(pilot_file_sha256, "pilot file sha256")
    _require_sha256(mention_proposals_file_sha256, "mention proposals file sha256")
    proposal = _verify_proposals(proposals)
    diagnostic = _verify_diagnostics(
        diagnostics,
        diagnostics_file_sha256,
        proposal_logical_sha256=proposal["logical_content_sha256"],
        proposal_file_sha256=mention_proposals_file_sha256,
    )
    snapshot, snapshot_matches = _snapshot_binding(
        document_snapshot,
        document_snapshot_file_sha256,
        proposal["document_snapshot_file_sha256"],
    )
    selection = _verify_discovery(
        discovery,
        discovery_file_sha256,
        proposal,
    )
    pilot_kind, sources, mentions = _load_pilot(pilot)
    official = _load_official(proposal["mentions"])
    diagnostic_rows = _load_diagnostic_rows(diagnostic, official)
    compared = _compare_sources(
        sources,
        mentions,
        official,
        diagnostic_rows,
        snapshot=snapshot,
        snapshot_matches=snapshot_matches,
        selected_item_ids=_selected_item_ids(selection),
    )
    document = _assemble(
        pilot_kind=pilot_kind,
        pilot_file_sha256=pilot_file_sha256,
        proposal=proposal,
        mention_proposals_file_sha256=mention_proposals_file_sha256,
        diagnostic=diagnostic,
        diagnostics_file_sha256=diagnostics_file_sha256,
        document_snapshot_file_sha256=document_snapshot_file_sha256,
        snapshot_matches=snapshot_matches,
        discovery=selection,
        discovery_file_sha256=discovery_file_sha256,
        sources=sources,
        official=official,
        compared=compared,
        diagnostic_rows=diagnostic_rows,
    )
    stamp_generated_artifact(document, created_at=created_at)
    return document


def _verify_proposals(proposals: object) -> dict:
    document = require_object(proposals, "mention proposals")
    if document.get("schema_version") != MENTION_PROPOSALS_SCHEMA:
        raise UnsupportedSchemaVersionError(
            f"mention proposals schema_version must be {MENTION_PROPOSALS_SCHEMA}"
        )
    return verify_generated_artifact(
        document, MENTION_PROPOSALS_SCHEMA, "mention proposals"
    )


def _verify_diagnostics(
    diagnostics: object | None,
    diagnostics_file_sha256: str | None,
    *,
    proposal_logical_sha256: str,
    proposal_file_sha256: str,
) -> dict | None:
    if diagnostics is None and diagnostics_file_sha256 is None:
        return None
    if diagnostics is None or diagnostics_file_sha256 is None:
        raise ArtifactError(
            "diagnostics and diagnostics file sha256 must be supplied together"
        )
    _require_sha256(diagnostics_file_sha256, "diagnostics file sha256")
    document = require_object(diagnostics, "mention proposal diagnostics")
    if document.get("schema_version") != MENTION_PROPOSAL_DIAGNOSTICS_SCHEMA:
        raise UnsupportedSchemaVersionError(
            "mention proposal diagnostics schema_version must be "
            f"{MENTION_PROPOSAL_DIAGNOSTICS_SCHEMA}"
        )
    verified = verify_generated_artifact(
        document,
        MENTION_PROPOSAL_DIAGNOSTICS_SCHEMA,
        "mention proposal diagnostics",
    )
    if verified.get("mention_proposals_logical_sha256") != proposal_logical_sha256:
        raise InputHashChangedError(
            "diagnostics mention_proposals_logical_sha256 does not match "
            "the proposal logical hash"
        )
    if verified.get("mention_proposals_file_sha256") != proposal_file_sha256:
        raise InputHashChangedError(
            "diagnostics mention_proposals_file_sha256 does not match the proposal file"
        )
    return verified


def _verify_discovery(
    discovery: object | None,
    discovery_file_sha256: str | None,
    proposal: dict,
) -> dict | None:
    if discovery is None and discovery_file_sha256 is None:
        return None
    if discovery is None or discovery_file_sha256 is None:
        raise ArtifactError(
            "discovery and discovery file sha256 must be supplied together"
        )
    _require_sha256(discovery_file_sha256, "discovery file sha256")
    document = require_object(discovery, "discovery selection")
    if document.get("schema_version") != DISCOVERY_SELECTION_SCHEMA:
        raise UnsupportedSchemaVersionError(
            f"discovery selection schema_version must be {DISCOVERY_SELECTION_SCHEMA}"
        )
    verified = verify_generated_artifact(
        document, DISCOVERY_SELECTION_SCHEMA, "discovery selection"
    )
    if verified.get("logical_content_sha256") != proposal.get(
        "discovery_logical_sha256"
    ):
        raise InputHashChangedError(
            "discovery logical hash does not match mention-proposals-v1"
        )
    if discovery_file_sha256 != proposal.get("discovery_file_sha256"):
        raise InputHashChangedError(
            "discovery file sha256 does not match mention-proposals-v1"
        )
    if verified.get("document_snapshot_file_sha256") != proposal.get(
        "document_snapshot_file_sha256"
    ):
        raise InputHashChangedError(
            "discovery document snapshot hash does not match mention-proposals-v1"
        )
    if verified.get("run_id") != proposal.get("run_id"):
        raise RunIdMismatchError("discovery run_id does not match mention-proposals-v1")
    return verified


def _selected_item_ids(selection: dict | None) -> set[int] | None:
    if selection is None:
        return None
    rows = selection.get("selected_items")
    if not isinstance(rows, list):
        raise ArtifactError("discovery selected_items must be a list")
    selected: set[int] = set()
    for row in rows:
        item = require_object(row, "discovery selected item")
        item_id = item.get("archive_item_id")
        if type(item_id) is not int:
            raise ArtifactError("discovery selected archive_item_id must be an integer")
        selected.add(item_id)
    return selected


def _snapshot_binding(
    document_snapshot: object | None,
    document_snapshot_file_sha256: str | None,
    proposal_snapshot_sha256: str,
) -> tuple[dict | None, bool | None]:
    if document_snapshot is None and document_snapshot_file_sha256 is None:
        return None, None
    if document_snapshot is None or document_snapshot_file_sha256 is None:
        raise ArtifactError(
            "document snapshot and document snapshot file sha256 "
            "must be supplied together"
        )
    _require_sha256(document_snapshot_file_sha256, "document snapshot file sha256")
    snapshot = require_document_snapshot(
        require_object(document_snapshot, "document text snapshot")
    )
    return snapshot, document_snapshot_file_sha256 == proposal_snapshot_sha256


def _load_pilot(
    pilot: object,
) -> tuple[str, list[_SourceBody], list[_PilotMention]]:
    if isinstance(pilot, list):
        if not pilot:
            raise ArtifactError("pilot list is empty; pass a pilot object with items")
        sources: list[_SourceBody] = []
        mentions: list[_PilotMention] = []
        pilot_kind = ""
        for index, row in enumerate(pilot):
            kind, source, rows = _one_pilot_document(row, input_order=index)
            if index == 0:
                pilot_kind = kind
            elif kind != pilot_kind:
                raise ArtifactError("pilot documents in one list must share a kind")
            sources.append(source)
            mentions.extend(rows)
        _require_unique_sources(sources)
        _require_unique_comparable(mentions)
        return pilot_kind, sources, mentions
    document = require_object(pilot, "pilot output")
    kind = document.get("kind")
    if not isinstance(kind, str) or "PILOT" not in kind.upper():
        raise ArtifactError("pilot output kind is not a mention-pilot result")
    if "items" in document and "candidates" in document:
        raise ArtifactError("pilot output cannot contain both items and candidates")
    if "items" in document:
        items = document.get("items")
        if not isinstance(items, list):
            raise ArtifactError("pilot items must be a list")
        sources = []
        mentions = []
        for index, item in enumerate(items):
            source, rows = _pilot_item(item, input_order=index)
            sources.append(source)
            mentions.extend(rows)
        _require_unique_sources(sources)
        _require_unique_comparable(mentions)
        return kind, sources, mentions
    single_kind, source, rows = _one_pilot_document(document, input_order=0)
    return single_kind, [source], rows


def _one_pilot_document(
    document: object, *, input_order: int
) -> tuple[str, _SourceBody, list[_PilotMention]]:
    row = require_object(document, "pilot output")
    kind = row.get("kind")
    if kind != SINGLE_PILOT_KIND:
        raise ArtifactError(
            f"pilot output kind must be {SINGLE_PILOT_KIND} or a pilot items bundle"
        )
    source, mentions = _pilot_item(row, input_order=input_order)
    return kind, source, mentions


def _pilot_item(
    item: object, *, input_order: int
) -> tuple[_SourceBody, list[_PilotMention]]:
    row = require_object(item, "pilot item")
    archive_item_id = _require_item_id(row.get("archive_item_id"))
    evidence_source = row.get("evidence_source", row.get("source"))
    if not isinstance(evidence_source, str) or not evidence_source.strip():
        raise ArtifactError("pilot item is missing an evidence source")
    candidates = row.get("candidates", [])
    if not isinstance(candidates, list):
        raise ArtifactError("pilot candidates must be a list")
    mentions = [
        _pilot_mention(candidate, archive_item_id, evidence_source)
        for candidate in candidates
    ]
    return (
        _SourceBody(
            archive_item_id,
            evidence_source,
            _pilot_body_sha256(row),
            input_order,
        ),
        mentions,
    )


def _pilot_body_sha256(row: dict) -> str | None:
    stored = row.get("body_sha256")
    if stored is not None:
        if not isinstance(stored, str):
            raise ArtifactError("pilot body_sha256 must be a string")
        _require_sha256(stored, "pilot body_sha256")
        return stored.lower()
    body_text = row.get("body_text")
    if body_text is None:
        return None
    if not isinstance(body_text, str):
        raise ArtifactError("pilot body_text must be a string")
    return sha256_bytes(body_text.encode("utf-8"))


def _pilot_mention(
    candidate: object, archive_item_id: int, evidence_source: str
) -> _PilotMention:
    row = require_object(candidate, "pilot candidate")
    start, end, comparable, reason = _pilot_offsets(row)
    exact = row.get("exact_mention")
    if exact is not None and not isinstance(exact, str):
        raise ArtifactError("pilot exact_mention must be a string")
    name, name_known = _optional_surface(row, "name_surface")
    title, title_known = _optional_surface(row, "title_surface")
    if "gemini_kind" in row:
        surface_kind = row.get("gemini_kind")
        surface_kind_known = True
    elif "surface_kind" in row:
        surface_kind = row.get("surface_kind")
        surface_kind_known = True
    else:
        surface_kind = None
        surface_kind_known = False
    if surface_kind is not None and not isinstance(surface_kind, str):
        raise ArtifactError("pilot surface kind must be a string or null")
    return _PilotMention(
        archive_item_id,
        evidence_source,
        comparable,
        start,
        end,
        exact,
        name,
        name_known,
        title,
        title_known,
        surface_kind,
        surface_kind_known,
        reason,
    )


def _pilot_offsets(row: dict) -> tuple[int | None, int | None, bool, str | None]:
    if "start_offset" not in row or "end_offset" not in row:
        return None, None, False, "pilot row has no recoverable offsets"
    start = row.get("start_offset")
    end = row.get("end_offset")
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        return None, None, False, "pilot row has no recoverable offsets"
    return start, end, True, None


def _optional_surface(row: dict, key: str) -> tuple[str | None, bool]:
    if key not in row:
        return None, False
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise ArtifactError(f"pilot {key} must be a string or null")
    return value, True


def _load_official(mentions: object) -> list[_OfficialMention]:
    if not isinstance(mentions, list):
        raise ArtifactError("mention proposals mentions must be a list")
    loaded: list[_OfficialMention] = []
    for mention in mentions:
        row = require_object(mention, "official mention")
        detector_ids = row.get("detector_ids")
        if not isinstance(detector_ids, list) or not all(
            isinstance(item, str) and item for item in detector_ids
        ):
            raise ArtifactError("official detector_ids must be a list of strings")
        loaded.append(
            _OfficialMention(
                archive_item_id=_require_item_id(row.get("archive_item_id")),
                evidence_source=_require_source(row.get("evidence_source")),
                start=row["start_offset"],
                end=row["end_offset"],
                exact_mention=row["exact_mention"],
                name_surface=row["name_surface"],
                title_surface=row["title_surface"],
                surface_kind=row["surface_kind"],
                detector_ids=tuple(detector_ids),
                known_name_match_quality=row["known_name_match_quality"],
            )
        )
    return loaded


def _load_diagnostic_rows(
    diagnostic: dict | None, official: list[_OfficialMention]
) -> list[dict]:
    if diagnostic is None:
        return []
    published = {
        (row.archive_item_id, row.evidence_source, row.start, row.end)
        for row in official
    }
    rows: list[dict] = []
    for section in DIAGNOSTIC_SECTIONS:
        entries = diagnostic.get(section)
        if not isinstance(entries, list):
            raise ArtifactError(f"diagnostics {section} must be a list")
        for entry in entries:
            row = require_object(entry, f"diagnostics {section} row")
            start = row.get("start_offset")
            end = row.get("end_offset")
            if start is None and end is None:
                start_value = None
                end_value = None
            elif type(start) is int and type(end) is int and 0 <= start < end:
                start_value = start
                end_value = end
            else:
                raise ArtifactError(
                    "diagnostic offsets must both be present and ordered"
                )
            item_id = _require_item_id(row.get("archive_item_id"))
            source = _require_source(row.get("evidence_source"))
            reason = row.get("reason")
            surface = row.get("surface")
            if not isinstance(reason, str) or not reason:
                raise ArtifactError("diagnostic reason must be a non-empty string")
            if not isinstance(surface, str):
                raise ArtifactError("diagnostic surface must be a string")
            segment_id = row.get("segment_id")
            if segment_id is not None and (
                not isinstance(segment_id, str) or not segment_id
            ):
                raise ArtifactError("diagnostic segment_id must be a string or null")
            published_span = (
                start_value is not None
                and (
                    item_id,
                    source,
                    start_value,
                    end_value,
                )
                in published
            )
            rows.append(
                {
                    "section": section,
                    "archive_item_id": item_id,
                    "evidence_source": source,
                    "surface": surface,
                    "reason": reason,
                    "segment_id": segment_id,
                    "start_offset": start_value,
                    "end_offset": end_value,
                    "published_official_span": published_span,
                }
            )
    return sorted(rows, key=_diagnostic_sort_key)


def _compare_sources(
    sources: list[_SourceBody],
    mentions: list[_PilotMention],
    official: list[_OfficialMention],
    diagnostic_rows: list[dict],
    *,
    snapshot: dict | None,
    snapshot_matches: bool | None,
    selected_item_ids: set[int] | None,
) -> dict:
    keys = {(source.archive_item_id, source.evidence_source) for source in sources}
    keys.update((row.archive_item_id, row.evidence_source) for row in official)
    keys.update(
        (row["archive_item_id"], row["evidence_source"]) for row in diagnostic_rows
    )
    pilot_hash = {
        (source.archive_item_id, source.evidence_source): source.pilot_body_sha256
        for source in sources
    }
    items: list[dict] = []
    disagreements: list[dict] = []
    review_rows: list[dict] = []
    for archive_item_id, evidence_source in sorted(keys):
        key = (archive_item_id, evidence_source)
        pilot_rows = [row for row in mentions if _mention_key(row) == key]
        official_rows = [row for row in official if _mention_key(row) == key]
        notes = [
            row
            for row in diagnostic_rows
            if (row["archive_item_id"], row["evidence_source"]) == key
        ]
        official_hash = _official_body_sha256(
            snapshot,
            snapshot_matches,
            archive_item_id,
            evidence_source,
        )
        equivalence = _equivalence(pilot_hash.get(key), official_hash)
        pilot_evaluated = key in pilot_hash
        official_evaluated = _official_evaluated(
            pilot_evaluated,
            official_rows,
            notes,
            snapshot,
            snapshot_matches,
            selected_item_ids,
            archive_item_id,
            evidence_source,
        )
        pair_comparable = pilot_evaluated and official_evaluated
        if pair_comparable:
            counts, source_disagreements, source_review = _classify_source(
                pilot_rows, official_rows, notes
            )
        else:
            counts, source_disagreements, source_review = _noncomparable_source(
                pilot_rows,
                official_rows,
                notes,
                pilot_evaluated=pilot_evaluated,
            )
        for row in source_disagreements:
            row["source_equivalence"] = equivalence
        for row in source_review:
            row["source_equivalence"] = equivalence
        items.append(
            {
                "archive_item_id": archive_item_id,
                "evidence_source": evidence_source,
                "source_equivalence": equivalence,
                "pilot_body_sha256": pilot_hash.get(key),
                "official_body_sha256": official_hash,
                "pilot_mention_count": len(pilot_rows),
                "official_mention_count": len(official_rows),
                "comparable_pilot_mention_count": sum(
                    1 for row in pilot_rows if row.comparable
                ),
                "non_comparable_pilot_mention_count": sum(
                    1 for row in pilot_rows if not row.comparable
                ),
                **counts,
                "source_pair_comparable": pair_comparable,
                "has_disagreement": bool(source_disagreements),
            }
        )
        disagreements.extend(source_disagreements)
        review_rows.extend(source_review)
    return {
        "items": items,
        "disagreements": _sort_rows(disagreements),
        "review_rows": _sort_rows(review_rows),
    }


def _official_evaluated(
    pilot_evaluated: bool,
    official_rows: list[_OfficialMention],
    notes: list[dict],
    snapshot: dict | None,
    snapshot_matches: bool | None,
    selected_item_ids: set[int] | None,
    archive_item_id: int,
    evidence_source: str,
) -> bool:
    if official_rows or notes:
        return True
    if not pilot_evaluated:
        return False
    if snapshot_matches is not True or selected_item_ids is None:
        return False
    if archive_item_id not in selected_item_ids:
        return False
    return _official_ran_displayed_source(snapshot, archive_item_id, evidence_source)


def _official_ran_displayed_source(
    snapshot: dict | None, archive_item_id: int, evidence_source: str
) -> bool:
    """True when find-mentions would pass this role for a selected item.

    Selection is by archive item. Each displayed body is run once, under the
    first text role of that body. A second role on the same body is not a
    separate official source. A body that merely exists in the snapshot is
    not enough: the item must also be in the bound discovery selection.
    """
    if snapshot is None:
        return False
    document = require_document_snapshot(snapshot)
    item = next(
        (
            row
            for row in document["items"]
            if row.get("archive_item_id") == archive_item_id
        ),
        None,
    )
    if item is None:
        return False
    try:
        bodies = select_text_bodies(item)
    except ArtifactError:
        return False
    return any(
        (body.get("text_roles") or [None])[0] == evidence_source for body in bodies
    )


def _official_has_displayed_body(
    snapshot: dict | None, archive_item_id: int, evidence_source: str
) -> bool:
    if snapshot is None:
        return False
    try:
        displayed_body_for_item(snapshot, archive_item_id, evidence_source)
    except ArtifactError:
        return False
    return True


def _noncomparable_source(
    pilot_rows: list[_PilotMention],
    official_rows: list[_OfficialMention],
    notes: list[dict],
    *,
    pilot_evaluated: bool,
) -> tuple[dict, list[dict], list[dict]]:
    review_rows: list[dict] = []
    if pilot_evaluated:
        reason = (
            "pilot source role was not evaluated by the official run; "
            "these mentions are not pilot-only disagreements"
        )
        for pilot in pilot_rows:
            review_rows.append(
                _pair_row(PILOT_SOURCE_NONCOMPARABLE, pilot, None, reason)
            )
        pilot_count = len(pilot_rows)
        official_count = 0
    else:
        reason = (
            "official source role was not in the pilot; "
            "these mentions are not official-only disagreements"
        )
        for official in official_rows:
            review_rows.append(
                _pair_row(OFFICIAL_SOURCE_NONCOMPARABLE, None, official, reason)
            )
        pilot_count = 0
        official_count = len(official_rows)
    for note in _unique_diagnostic_rows(notes):
        if note["published_official_span"]:
            continue
        review_rows.append(_diagnostic_review_row(note))
    return _zero_comparison_counts(official_count, pilot_count), [], review_rows


def _zero_comparison_counts(official_count: int, pilot_count: int) -> dict:
    return {
        "exact_span_agreement_count": 0,
        "overlap_different_span_count": 0,
        "official_only_count": 0,
        "pilot_only_count": 0,
        "same_span_name_surface_differs_count": 0,
        "same_span_title_surface_differs_count": 0,
        "same_span_surface_kind_differs_count": 0,
        "official_noncomparable_source_mention_count": official_count,
        "pilot_noncomparable_source_mention_count": pilot_count,
    }


def _classify_source(
    pilot_rows: list[_PilotMention],
    official_rows: list[_OfficialMention],
    notes: list[dict],
) -> tuple[dict, list[dict], list[dict]]:
    comparable = [row for row in pilot_rows if row.comparable]
    exact_pilot = {(row.start, row.end): row for row in comparable}
    exact_official = {(row.start, row.end): row for row in official_rows}
    shared = sorted(set(exact_pilot) & set(exact_official))
    disagreements: list[dict] = []
    review_rows: list[dict] = []
    same_name = 0
    same_title = 0
    same_kind = 0
    for start, end in shared:
        pilot = exact_pilot[(start, end)]
        official = exact_official[(start, end)]
        row = _pair_row(BOTH_SAME_SPAN, pilot, official, None)
        if row["name_surface_relation"] == "DIFFERENT":
            same_name += 1
        if row["title_surface_relation"] == "DIFFERENT":
            same_title += 1
        if row["surface_kind_relation"] == "DIFFERENT":
            same_kind += 1
        if _surface_differs(row):
            disagreements.append(row)
        if _surface_differs(row) or _known_name_only(official):
            review_rows.append(row)
    remaining_pilot = [
        row for row in comparable if (row.start, row.end) not in exact_official
    ]
    remaining_official = [
        row for row in official_rows if (row.start, row.end) not in exact_pilot
    ]
    overlap_count = 0
    official_only = 0
    pilot_only = 0
    for members in _overlap_groups(remaining_pilot, remaining_official):
        pilots = [remaining_pilot[index] for side, index in members if side == "pilot"]
        officials = [
            remaining_official[index] for side, index in members if side == "official"
        ]
        if pilots and officials:
            overlap_count += len(pilots) + len(officials)
            for pilot in pilots:
                row = _overlap_row(pilot, officials, pilot_side=True)
                disagreements.append(row)
                review_rows.append(row)
            for official in officials:
                row = _overlap_row(official, pilots, pilot_side=False)
                disagreements.append(row)
                review_rows.append(row)
        elif officials:
            official_only += len(officials)
            for official in officials:
                row = _official_only_row(official)
                disagreements.append(row)
                review_rows.append(row)
        else:
            pilot_only += len(pilots)
            for pilot in pilots:
                row = _pilot_only_row(pilot)
                disagreements.append(row)
                review_rows.append(row)
    for pilot in pilot_rows:
        if pilot.comparable:
            continue
        row = _noncomparable_row(pilot)
        disagreements.append(row)
        review_rows.append(row)
    for note in _unique_diagnostic_rows(notes):
        if note["published_official_span"]:
            continue
        row = _diagnostic_review_row(note)
        disagreements.append(row)
        review_rows.append(row)
    _attach_diagnostic_reasons(disagreements, notes)
    _attach_diagnostic_reasons(review_rows, notes)
    counts = {
        "exact_span_agreement_count": len(shared),
        "overlap_different_span_count": overlap_count,
        "official_only_count": official_only,
        "pilot_only_count": pilot_only,
        "same_span_name_surface_differs_count": same_name,
        "same_span_title_surface_differs_count": same_title,
        "same_span_surface_kind_differs_count": same_kind,
        "official_noncomparable_source_mention_count": 0,
        "pilot_noncomparable_source_mention_count": 0,
    }
    return counts, disagreements, review_rows


def _overlap_groups(
    pilot_rows: list[_PilotMention], official_rows: list[_OfficialMention]
) -> list[list[tuple[str, int]]]:
    nodes = [("pilot", index) for index in range(len(pilot_rows))]
    nodes.extend(("official", index) for index in range(len(official_rows)))
    parent = {node: node for node in nodes}

    def find(node: tuple[str, int]) -> tuple[str, int]:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: tuple[str, int], right: tuple[str, int]) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for pilot_index, pilot in enumerate(pilot_rows):
        for official_index, official in enumerate(official_rows):
            if _overlaps(pilot.start, pilot.end, official.start, official.end):
                union(("pilot", pilot_index), ("official", official_index))
    grouped: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for node in nodes:
        grouped.setdefault(find(node), []).append(node)
    return [grouped[key] for key in sorted(grouped)]


def _pair_row(
    category: str,
    pilot: _PilotMention | None,
    official: _OfficialMention | None,
    reason: str | None,
) -> dict:
    name_relation = _relation(
        pilot.name_known if pilot else False,
        pilot.name_surface if pilot else None,
        official is not None,
        official.name_surface if official else None,
    )
    title_relation = _relation(
        pilot.title_known if pilot else False,
        pilot.title_surface if pilot else None,
        official is not None,
        official.title_surface if official else None,
    )
    kind_relation = _relation(
        pilot.surface_kind_known if pilot else False,
        pilot.surface_kind if pilot else None,
        official is not None,
        official.surface_kind if official else None,
    )
    if reason is None:
        reason = _same_span_reason(
            name_relation, title_relation, kind_relation, official
        )
    anchor = pilot if pilot is not None else official
    if anchor is None:
        raise ArtifactError("comparison row is missing both spans")
    return _review_row(
        category=category,
        archive_item_id=anchor.archive_item_id,
        evidence_source=anchor.evidence_source,
        comparison_reason=reason,
        pilot=pilot,
        official=official,
        diagnostic_reason=None,
        name_relation=name_relation,
        title_relation=title_relation,
        kind_relation=kind_relation,
    )


def _same_span_reason(
    name_relation: str,
    title_relation: str,
    kind_relation: str,
    official: _OfficialMention | None,
) -> str:
    parts: list[str] = []
    if name_relation == "DIFFERENT":
        parts.append("name surface differs")
    if title_relation == "DIFFERENT":
        parts.append("title surface differs")
    if kind_relation == "DIFFERENT":
        parts.append("surface kind differs")
    if official is not None and _known_name_only(official):
        parts.append("known-name detector only")
    if not parts:
        return "same outer span"
    return "same outer span; " + "; ".join(parts)


def _overlap_row(focus, counterparts, *, pilot_side: bool) -> dict:
    ordered = sorted(counterparts, key=lambda item: (item.start, item.end))
    spans = ", ".join(f"{row.start}-{row.end}" for row in ordered)
    other = "official" if pilot_side else "pilot"
    reason = f"overlapping {other} span with different offsets ({spans})"
    single = ordered[0] if len(ordered) == 1 else None
    if pilot_side:
        official = single
        pilot = focus
        if isinstance(official, _OfficialMention) and _known_name_only(official):
            reason = f"{reason}; known-name detector only"
        return _pair_row(BOTH_OVERLAP, pilot, official, reason)
    pilot = single
    official = focus
    if _known_name_only(official):
        reason = f"{reason}; known-name detector only"
    return _pair_row(BOTH_OVERLAP, pilot, official, reason)


def _official_only_row(official: _OfficialMention) -> dict:
    reason = "no overlapping pilot span"
    if _known_name_only(official):
        reason = "no overlapping pilot span; known-name detector only"
    return _pair_row(OFFICIAL_ONLY, None, official, reason)


def _pilot_only_row(pilot: _PilotMention) -> dict:
    return _pair_row(PILOT_ONLY, pilot, None, "no overlapping official span")


def _noncomparable_row(pilot: _PilotMention) -> dict:
    return _pair_row(
        NON_COMPARABLE,
        pilot,
        None,
        pilot.reason or "pilot row has no recoverable offsets",
    )


def _diagnostic_review_row(note: dict) -> dict:
    reason = note["reason"]
    return _review_row(
        category=DIAGNOSTIC_ONLY,
        archive_item_id=note["archive_item_id"],
        evidence_source=note["evidence_source"],
        comparison_reason=(
            f"official diagnostics record {reason}; span was not published"
        ),
        pilot=None,
        official=None,
        diagnostic_reason=reason,
        name_relation="UNAVAILABLE",
        title_relation="UNAVAILABLE",
        kind_relation="UNAVAILABLE",
        official_text=note["surface"],
        official_start=note["start_offset"],
        official_end=note["end_offset"],
    )


def _review_row(
    *,
    category: str,
    archive_item_id: int,
    evidence_source: str,
    comparison_reason: str,
    pilot: _PilotMention | None,
    official: _OfficialMention | None,
    diagnostic_reason: str | None,
    name_relation: str,
    title_relation: str,
    kind_relation: str,
    official_text: str | None = None,
    official_start: int | None = None,
    official_end: int | None = None,
) -> dict:
    if official is not None:
        official_text = official.exact_mention
        official_start = official.start
        official_end = official.end
    return {
        "archive_item_id": archive_item_id,
        "evidence_source": evidence_source,
        "category": category,
        "comparison_reason": comparison_reason,
        "pilot_start_offset": None if pilot is None else pilot.start,
        "pilot_end_offset": None if pilot is None else pilot.end,
        "pilot_exact_mention": None if pilot is None else pilot.exact_mention,
        "pilot_name_surface": None
        if pilot is None or not pilot.name_known
        else pilot.name_surface,
        "pilot_title_surface": None
        if pilot is None or not pilot.title_known
        else pilot.title_surface,
        "pilot_surface_kind": None
        if pilot is None or not pilot.surface_kind_known
        else pilot.surface_kind,
        "official_start_offset": official_start,
        "official_end_offset": official_end,
        "official_exact_mention": official_text,
        "official_name_surface": None if official is None else official.name_surface,
        "official_title_surface": None if official is None else official.title_surface,
        "official_surface_kind": None if official is None else official.surface_kind,
        "official_detector_ids": []
        if official is None
        else list(official.detector_ids),
        "known_name_match_quality": None
        if official is None
        else official.known_name_match_quality,
        "diagnostic_reason": diagnostic_reason,
        "name_surface_relation": name_relation,
        "title_surface_relation": title_relation,
        "surface_kind_relation": kind_relation,
    }


def _attach_diagnostic_reasons(rows: list[dict], notes: list[dict]) -> None:
    by_span: dict[tuple, str] = {}
    for note in notes:
        if note["start_offset"] is None:
            continue
        key = (
            note["archive_item_id"],
            note["evidence_source"],
            note["start_offset"],
            note["end_offset"],
        )
        by_span.setdefault(key, note["reason"])
    for row in rows:
        if row["diagnostic_reason"]:
            continue
        for start_key, end_key in (
            ("pilot_start_offset", "pilot_end_offset"),
            ("official_start_offset", "official_end_offset"),
        ):
            start = row[start_key]
            end = row[end_key]
            if start is None:
                continue
            reason = by_span.get(
                (row["archive_item_id"], row["evidence_source"], start, end)
            )
            if reason:
                row["diagnostic_reason"] = reason
                break


def _assemble(
    *,
    pilot_kind: str,
    pilot_file_sha256: str,
    proposal: dict,
    mention_proposals_file_sha256: str,
    diagnostic: dict | None,
    diagnostics_file_sha256: str | None,
    document_snapshot_file_sha256: str | None,
    snapshot_matches: bool | None,
    discovery: dict | None,
    discovery_file_sha256: str | None,
    sources: list[_SourceBody],
    official: list[_OfficialMention],
    compared: dict,
    diagnostic_rows: list[dict],
) -> dict:
    items = compared["items"]
    archive_ids = sorted({row["archive_item_id"] for row in items})
    pilot_order: list[int] = []
    for source in sorted(sources, key=lambda row: row.input_order):
        if source.archive_item_id not in pilot_order:
            pilot_order.append(source.archive_item_id)
    pilot_by_item: dict[int, int] = {}
    official_by_item: dict[int, int] = {}
    equivalence_by_item: dict[int, set[str]] = {}
    disagreement_items: set[int] = set()
    for row in items:
        item_id = row["archive_item_id"]
        pilot_by_item[item_id] = (
            pilot_by_item.get(item_id, 0) + row["pilot_mention_count"]
        )
        official_by_item[item_id] = (
            official_by_item.get(item_id, 0) + row["official_mention_count"]
        )
        if row["source_pair_comparable"]:
            equivalence_by_item.setdefault(item_id, set()).add(
                row["source_equivalence"]
            )
        if row["has_disagreement"]:
            disagreement_items.add(item_id)
    item_equivalence = {
        item_id: _item_equivalence(statuses)
        for item_id, statuses in equivalence_by_item.items()
    }
    diagnostics_available = diagnostic is not None
    published_items = [
        {key: value for key, value in row.items() if not key.startswith("same_span_")}
        for row in items
    ]
    return {
        "schema_version": MENTION_RUN_COMPARISON_V3_SCHEMA,
        "kind": COMPARISON_KIND,
        "operational_pipeline": False,
        "pilot_is_ground_truth": False,
        "precision_recall_computed": False,
        "production_write_performed": False,
        "human_approval_performed": False,
        "apply_authorized": False,
        "comparison_command": COMPARISON_COMMAND,
        "pilot_file_sha256": pilot_file_sha256,
        "pilot_kind": pilot_kind,
        "mention_proposals_file_sha256": mention_proposals_file_sha256,
        "mention_proposals_logical_sha256": proposal["logical_content_sha256"],
        "mention_proposals_schema_version": MENTION_PROPOSALS_SCHEMA,
        "proposal_run_id": proposal["run_id"],
        "diagnostics_available": diagnostics_available,
        "diagnostics_file_sha256": diagnostics_file_sha256,
        "diagnostics_logical_sha256": None
        if diagnostic is None
        else diagnostic["logical_content_sha256"],
        "diagnostics_schema_version": None
        if diagnostic is None
        else MENTION_PROPOSAL_DIAGNOSTICS_SCHEMA,
        "discovery_available": discovery is not None,
        "discovery_file_sha256": discovery_file_sha256,
        "discovery_logical_sha256": None
        if discovery is None
        else discovery["logical_content_sha256"],
        "discovery_schema_version": None
        if discovery is None
        else DISCOVERY_SELECTION_SCHEMA,
        "document_snapshot_file_sha256": document_snapshot_file_sha256,
        "official_snapshot_file_matches": snapshot_matches,
        "source_equivalence": _rollup_comparable_equivalence(items),
        "source_equivalence_summary": _source_equivalence_summary(items),
        "historical_corpus_recovery": _historical_corpus_recovery(pilot_order),
        "corpus": {
            "archive_item_ids": archive_ids,
            "pilot_archive_item_ids_in_input_order": pilot_order,
            "item_count": len(archive_ids),
            "sources": [
                {
                    "archive_item_id": row["archive_item_id"],
                    "evidence_source": row["evidence_source"],
                }
                for row in published_items
            ],
        },
        "metrics": _metrics(
            items,
            archive_ids,
            pilot_by_item,
            official_by_item,
            disagreement_items,
            item_equivalence,
            diagnostic_rows,
            diagnostics_available,
        ),
        "detector_contribution": _detector_contribution(official),
        "diagnostic_counts": _diagnostic_counts(diagnostic_rows)
        if diagnostics_available
        else [],
        "unique_diagnostic_counts": _diagnostic_counts(
            _unique_diagnostic_rows(diagnostic_rows)
        )
        if diagnostics_available
        else [],
        "items": published_items,
        "disagreements": compared["disagreements"],
        "review_rows": compared["review_rows"],
        "diagnostic_rows": diagnostic_rows,
        "limitations": _limitations(
            diagnostics_available, snapshot_matches, discovery is not None
        ),
    }


def _metrics(
    items: list[dict],
    archive_ids: list[int],
    pilot_by_item: dict[int, int],
    official_by_item: dict[int, int],
    disagreement_items: set[int],
    item_equivalence: dict[int, str],
    diagnostic_rows: list[dict],
    diagnostics_available: bool,
) -> dict:
    equivalence_values = list(item_equivalence.values())
    return {
        "corpus_item_count": len(archive_ids),
        "pilot_mention_count": sum(row["pilot_mention_count"] for row in items),
        "official_mention_count": sum(row["official_mention_count"] for row in items),
        "comparable_pilot_mention_count": sum(
            row["comparable_pilot_mention_count"] for row in items
        ),
        "non_comparable_pilot_mention_count": sum(
            row["non_comparable_pilot_mention_count"] for row in items
        ),
        "exact_span_agreement_count": sum(
            row["exact_span_agreement_count"] for row in items
        ),
        "overlap_different_span_count": sum(
            row["overlap_different_span_count"] for row in items
        ),
        "official_only_count": sum(row["official_only_count"] for row in items),
        "pilot_only_count": sum(row["pilot_only_count"] for row in items),
        "source_pairs_comparable": sum(
            1 for row in items if row["source_pair_comparable"]
        ),
        "source_pairs_noncomparable": sum(
            1 for row in items if not row["source_pair_comparable"]
        ),
        "source_pairs_with_any_disagreement": sum(
            1
            for row in items
            if row["source_pair_comparable"] and row["has_disagreement"]
        ),
        "official_noncomparable_source_mention_count": sum(
            row["official_noncomparable_source_mention_count"] for row in items
        ),
        "pilot_noncomparable_source_mention_count": sum(
            row["pilot_noncomparable_source_mention_count"] for row in items
        ),
        "same_span_name_surface_differs_count": sum(
            row["same_span_name_surface_differs_count"] for row in items
        ),
        "same_span_title_surface_differs_count": sum(
            row["same_span_title_surface_differs_count"] for row in items
        ),
        "same_span_surface_kind_differs_count": sum(
            row["same_span_surface_kind_differs_count"] for row in items
        ),
        "items_with_zero_pilot_mentions": sum(
            1 for item_id in archive_ids if pilot_by_item.get(item_id, 0) == 0
        ),
        "items_with_zero_official_mentions": sum(
            1 for item_id in archive_ids if official_by_item.get(item_id, 0) == 0
        ),
        "items_with_any_disagreement": len(disagreement_items),
        "items_with_source_drift": equivalence_values.count(SOURCE_DRIFT),
        "items_source_equivalent": equivalence_values.count(SOURCE_EQUIVALENT),
        "items_source_equivalence_unknown": equivalence_values.count(SOURCE_UNKNOWN),
        "split_conflict_count": _reason_count(diagnostic_rows, "SPLIT_CONFLICT")
        if diagnostics_available
        else None,
        "absorb_ambiguous_count": _reason_count(diagnostic_rows, "ABSORB_AMBIGUOUS")
        if diagnostics_available
        else None,
        "diagnostic_only_span_count": sum(
            1 for row in diagnostic_rows if not row["published_official_span"]
        )
        if diagnostics_available
        else None,
        "raw_diagnostic_record_count": len(diagnostic_rows)
        if diagnostics_available
        else None,
        "unique_diagnostic_occurrence_count": len(
            _unique_diagnostic_rows(diagnostic_rows)
        )
        if diagnostics_available
        else None,
        "unique_diagnostic_only_span_count": len(
            _unique_diagnostic_rows(
                [row for row in diagnostic_rows if not row["published_official_span"]]
            )
        )
        if diagnostics_available
        else None,
    }


def _detector_contribution(official: list[_OfficialMention]) -> dict:
    semantic_only = 0
    known_only = 0
    both = 0
    other = 0
    exact = 0
    prefixed = 0
    for row in official:
        ids = set(row.detector_ids)
        has_semantic = SEMANTIC_DETECTOR_ID in ids
        has_known = KNOWN_NAME_DETECTOR_ID in ids
        if has_semantic and has_known:
            both += 1
        elif has_known:
            known_only += 1
        elif has_semantic:
            semantic_only += 1
        else:
            other += 1
        if has_known and row.known_name_match_quality == "EXACT_BOUNDARY":
            exact += 1
        elif has_known and row.known_name_match_quality == "PREFIXED":
            prefixed += 1
    return {
        "semantic_only_published_count": semantic_only,
        "known_name_only_published_count": known_only,
        "both_detectors_published_count": both,
        "other_detector_published_count": other,
        "known_name_exact_boundary_count": exact,
        "known_name_prefixed_count": prefixed,
    }


def _unique_diagnostic_rows(rows: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    unique: list[dict] = []
    for row in rows:
        key = (
            row["archive_item_id"],
            row["evidence_source"],
            row["section"],
            row["reason"],
            row["start_offset"],
            row["end_offset"],
            row["surface"],
            row["segment_id"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _diagnostic_counts(rows: list[dict]) -> list[dict]:
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["section"], row["reason"])
        counts[key] = counts.get(key, 0) + 1
    return [
        {"section": section, "reason": reason, "count": counts[(section, reason)]}
        for section, reason in sorted(counts)
    ]


def _item_equivalence(statuses: set[str]) -> str:
    if SOURCE_DRIFT in statuses:
        return SOURCE_DRIFT
    if SOURCE_UNKNOWN in statuses:
        return SOURCE_UNKNOWN
    return SOURCE_EQUIVALENT


def _rollup_comparable_equivalence(items: list[dict]) -> str:
    statuses = {
        row["source_equivalence"] for row in items if row["source_pair_comparable"]
    }
    if not statuses:
        return SOURCE_UNKNOWN
    return _item_equivalence(statuses)


def _source_equivalence_summary(items: list[dict]) -> dict:
    comparable = [row for row in items if row["source_pair_comparable"]]
    noncomparable = [row for row in items if not row["source_pair_comparable"]]
    statuses = {row["source_equivalence"] for row in comparable}
    if not comparable:
        status = "NO_COMPARABLE_PAIRS"
    elif SOURCE_DRIFT in statuses:
        status = "HAS_SOURCE_DRIFT"
    elif SOURCE_UNKNOWN in statuses:
        status = "HAS_UNKNOWN"
    else:
        status = "ALL_EQUIVALENT"
    return {
        "comparable_pairs_status": status,
        "noncomparable_source_pair_count": len(noncomparable),
        "semantics": SOURCE_EQUIVALENCE_SEMANTICS,
    }


def _historical_corpus_recovery(pilot_order: list[int]) -> dict:
    return {
        "recovered_item_count": len(RECOVERED_CORPUS_IDS),
        "archive_item_ids_in_input_order": list(RECOVERED_CORPUS_IDS),
        "pilot_result": "refined",
        "additional_recoverable_corpus_artifact_found": False,
        "compared_corpus_matches_recovered_corpus": pilot_order
        == list(RECOVERED_CORPUS_IDS),
        "statement": CORPUS_RECOVERY_STATEMENT,
    }


def _limitations(
    diagnostics_available: bool,
    snapshot_matches: bool | None,
    discovery_available: bool,
) -> list[str]:
    limitations: list[str] = [
        "Pilot output is not ground truth. Precision and recall are not computed.",
        CORPUS_RECOVERY_STATEMENT,
        (
            "Comparison identity is archive item plus evidence source. A source "
            "role present on only one side is OFFICIAL_SOURCE_NONCOMPARABLE or "
            "PILOT_SOURCE_NONCOMPARABLE and is excluded from official-only, "
            "pilot-only, overlap, and disagreement counts."
        ),
        (
            "overlap_different_span_count is the number of pilot and official "
            "mention rows inside overlap-different-span groups. One pilot row "
            "overlapping one official row counts as 2, not as one adjudication case."
        ),
        SOURCE_EQUIVALENCE_SEMANTICS,
        (
            "An official source is evaluated when that exact archive item and "
            "evidence source has an official mention or diagnostic, or when a "
            "proposal-bound discovery selection includes the item and the bound "
            "snapshot shows find-mentions would run that displayed role. A missing "
            "or mismatched snapshot is not that evidence. SOURCE_EQUIVALENCE_UNKNOWN "
            "is not that evidence."
        ),
    ]
    if not diagnostics_available:
        limitations.append(
            "mention-proposal-diagnostics-v1 was not supplied. "
            "Diagnostic metrics are unavailable."
        )
    if snapshot_matches is None:
        limitations.append(
            "No document-text snapshot was supplied. "
            "Official body hashes are unknown, so source equivalence is unknown."
        )
    elif snapshot_matches is False:
        limitations.append(
            "The document snapshot file hash does not match "
            "mention-proposals-v1 document_snapshot_file_sha256. "
            "Source equivalence is unknown."
        )
    if not discovery_available:
        limitations.append(
            "discovery-selection-v1 was not supplied. A source with no official "
            "mention and no diagnostic cannot be shown to have been evaluated."
        )
    return limitations


def _official_body_sha256(
    snapshot: dict | None,
    snapshot_matches: bool | None,
    archive_item_id: int,
    evidence_source: str,
) -> str | None:
    if snapshot is None or snapshot_matches is not True:
        return None
    if not _official_has_displayed_body(snapshot, archive_item_id, evidence_source):
        return None
    selected = displayed_body_for_item(snapshot, archive_item_id, evidence_source)
    return sha256_bytes(selected["text"].encode("utf-8"))


def _equivalence(pilot_sha: str | None, official_sha: str | None) -> str:
    if pilot_sha is None or official_sha is None:
        return SOURCE_UNKNOWN
    if pilot_sha.lower() == official_sha.lower():
        return SOURCE_EQUIVALENT
    return SOURCE_DRIFT


def _relation(
    left_known: bool, left: str | None, right_known: bool, right: str | None
) -> str:
    if not left_known or not right_known:
        return "UNAVAILABLE"
    if left == right:
        return "SAME"
    return "DIFFERENT"


def _surface_differs(row: dict) -> bool:
    return any(
        row[key] == "DIFFERENT"
        for key in (
            "name_surface_relation",
            "title_surface_relation",
            "surface_kind_relation",
        )
    )


def _known_name_only(official: _OfficialMention) -> bool:
    ids = set(official.detector_ids)
    return KNOWN_NAME_DETECTOR_ID in ids and SEMANTIC_DETECTOR_ID not in ids


def _overlaps(
    left_start: int | None,
    left_end: int | None,
    right_start: int | None,
    right_end: int | None,
) -> bool:
    if None in {left_start, left_end, right_start, right_end}:
        return False
    return left_start < right_end and right_start < left_end


def _mention_key(row: _PilotMention | _OfficialMention) -> tuple[int, str]:
    return row.archive_item_id, row.evidence_source


def _sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=_review_sort_key)


def _review_sort_key(row: dict) -> tuple:
    start = row["pilot_start_offset"]
    if start is None:
        start = row["official_start_offset"]
    end = row["pilot_end_offset"]
    if end is None:
        end = row["official_end_offset"]
    return (
        row["archive_item_id"],
        row["evidence_source"],
        start if start is not None else -1,
        end if end is not None else -1,
        row["category"],
        row["comparison_reason"],
        row["pilot_exact_mention"] or "",
        row["official_exact_mention"] or "",
    )


def _diagnostic_sort_key(row: dict) -> tuple:
    return (
        row["archive_item_id"],
        row["evidence_source"],
        row["start_offset"] if row["start_offset"] is not None else -1,
        row["end_offset"] if row["end_offset"] is not None else -1,
        row["section"],
        row["reason"],
        row["surface"],
        row["segment_id"] or "",
    )


def _reason_count(rows: list[dict], reason: str) -> int:
    return sum(1 for row in rows if row["reason"] == reason)


def _require_unique_sources(sources: list[_SourceBody]) -> None:
    seen: set[tuple[int, str]] = set()
    for source in sources:
        key = (source.archive_item_id, source.evidence_source)
        if key in seen:
            raise ArtifactError(
                "duplicate pilot source for one archive item and evidence source"
            )
        seen.add(key)


def _require_unique_comparable(mentions: list[_PilotMention]) -> None:
    seen: set[tuple[int, str, int, int]] = set()
    for mention in mentions:
        if not mention.comparable or mention.start is None or mention.end is None:
            continue
        key = (
            mention.archive_item_id,
            mention.evidence_source,
            mention.start,
            mention.end,
        )
        if key in seen:
            raise ArtifactError(
                "duplicate comparable pilot span for one archive item and evidence source"
            )
        seen.add(key)


def _require_item_id(value: object) -> int:
    if type(value) is not int:
        raise ArtifactError("archive_item_id must be an integer")
    return value


def _require_source(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError("evidence_source must be a non-empty string")
    return value


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ArtifactError(f"{label} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ArtifactError(f"{label} must be a SHA-256 hex digest") from exc
