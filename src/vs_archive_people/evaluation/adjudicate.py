"""Package mention-run-comparison-v3 into pending human adjudication cases.

Comparison rows are not cases. Overlap components become one case. Diagnostics
are evidence on a related occurrence, or a standalone case when no occurrence
matches. Every case starts pending. This module does not tune a detector,
call a provider, or write production data.
"""

from __future__ import annotations

from vs_archive_people.artifacts.io import (
    MENTION_ADJUDICATION_DECISIONS_SCHEMA,
    MENTION_ADJUDICATION_SCHEMA,
    MENTION_RUN_COMPARISON_V3_SCHEMA,
    ArtifactError,
    InputHashChangedError,
    UnsupportedSchemaVersionError,
    require_object,
)
from vs_archive_people.artifacts.provenance import (
    stamp_generated_artifact,
    verify_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import validate_artifact
from vs_archive_people.evaluation.compare import (
    BOTH_OVERLAP,
    BOTH_SAME_SPAN,
    DIAGNOSTIC_ONLY,
    NON_COMPARABLE,
    OFFICIAL_ONLY,
    OFFICIAL_SOURCE_NONCOMPARABLE,
    PILOT_ONLY,
    PILOT_SOURCE_NONCOMPARABLE,
    _unique_diagnostic_rows,
)
from vs_archive_people.find_mentions.registry import (
    KNOWN_NAME_DETECTOR_ID,
    SEMANTIC_DETECTOR_ID,
)

ADJUDICATION_KIND = "MENTION_ADJUDICATION_NOT_OPERATIONAL"
ADJUDICATION_COMMAND = "package-mention-adjudication"
DECISIONS_KIND = "MENTION_ADJUDICATION_DECISIONS_NOT_OPERATIONAL"
REVIEW_PENDING = "PENDING"
CASE_PILOT_ONLY = "PILOT_ONLY"
CASE_OFFICIAL_ONLY = "OFFICIAL_ONLY"
CASE_OVERLAP = "OVERLAP_DIFFERENT_SPAN"
CASE_SAME_SPAN = "SAME_SPAN_SURFACE_DIFFERENCE"
CASE_SAME_SPAN_KNOWN_NAME = "SAME_SPAN_KNOWN_NAME_ONLY"
CASE_SPLIT = "SPLIT_CONFLICT"
CASE_DIAGNOSTIC = "DIAGNOSTIC_SUPPRESSION"
CASE_NONCOMPARABLE_SOURCE = "NONCOMPARABLE_SOURCE"
CASE_NONCOMPARABLE_PILOT = "NONCOMPARABLE_PILOT_ROW"
DECISIONS = (
    "OFFICIAL_PREFERRED",
    "PILOT_PREFERRED",
    "BOTH_ACCEPTABLE",
    "BOTH_WRONG",
    "NOT_A_PERSON",
    "NEEDS_RESEARCH",
)
_OCCURRENCE_CATEGORIES = (
    BOTH_SAME_SPAN,
    BOTH_OVERLAP,
    OFFICIAL_ONLY,
    PILOT_ONLY,
    OFFICIAL_SOURCE_NONCOMPARABLE,
    PILOT_SOURCE_NONCOMPARABLE,
    NON_COMPARABLE,
)
_NONCOMPARABLE_SOURCE_CATEGORIES = (
    OFFICIAL_SOURCE_NONCOMPARABLE,
    PILOT_SOURCE_NONCOMPARABLE,
)


def build_mention_adjudication(
    comparison: object,
    *,
    comparison_file_sha256: str,
    created_at: str | None = None,
) -> dict:
    """Build an immutable pending adjudication packet from one comparison."""
    document = _verify_comparison(comparison)
    _require_sha256(comparison_file_sha256, "comparison file sha256")
    equivalence = _source_equivalence(document["items"])
    review_rows = _review_rows(document["review_rows"], equivalence)
    diagnostic_rows = _diagnostic_rows(document["diagnostic_rows"])
    _require_diagnostic_only_rows(review_rows, diagnostic_rows)
    cases = _occurrence_cases(review_rows, equivalence)
    unique_notes = _unique_diagnostic_rows(diagnostic_rows)
    unmatched = _attach_diagnostics(cases, unique_notes)
    cases.extend(_standalone_diagnostic_cases(unmatched, equivalence))
    _require_distinct_case_geometry(cases)
    ordered = sorted(cases, key=_case_sort_key)
    for index, case in enumerate(ordered, start=1):
        case["case_id"] = f"C{index:04d}"
    _require_unique_case_ids(ordered)
    packet = _assemble(
        document,
        comparison_file_sha256=comparison_file_sha256,
        cases=ordered,
        review_rows=review_rows,
        raw_diagnostic_count=len(diagnostic_rows),
        unique_diagnostic_count=len(unique_notes),
    )
    stamp_generated_artifact(packet, created_at=created_at)
    validate_artifact(packet)
    return packet


def build_mention_adjudication_decisions(
    packet: object,
    *,
    adjudication_file_sha256: str,
) -> dict:
    """Empty decision rows bound to one generated adjudication packet.

    The returned file is the manual decision record. It is not stamped, so a
    later reviewer can fill ``decision`` and ``reviewer_note`` without
    changing the packet hash.
    """
    document = _verify_packet(packet)
    _require_sha256(adjudication_file_sha256, "adjudication file sha256")
    decisions = {
        "schema_version": MENTION_ADJUDICATION_DECISIONS_SCHEMA,
        "kind": DECISIONS_KIND,
        "adjudication_schema_version": MENTION_ADJUDICATION_SCHEMA,
        "adjudication_logical_sha256": document["logical_content_sha256"],
        "adjudication_file_sha256": adjudication_file_sha256,
        "production_write_performed": False,
        "apply_authorized": False,
        "tuning_performed": False,
        "human_approval_performed": False,
        "decisions": [
            {
                "case_id": case["case_id"],
                "decision": None,
                "reviewer_note": None,
            }
            for case in document["cases"]
        ],
    }
    validate_artifact(decisions)
    require_mention_adjudication_decisions(
        document,
        decisions,
        adjudication_file_sha256=adjudication_file_sha256,
    )
    return decisions


def require_mention_adjudication_decisions(
    packet: object,
    decisions: object,
    *,
    adjudication_file_sha256: str,
) -> dict:
    """Require an editable decision file to name this packet's cases exactly once.

    Row order is not meaningful. The case-id set must equal the packet. The
    packet is not modified.
    """
    document = _verify_packet(packet)
    _require_sha256(adjudication_file_sha256, "adjudication file sha256")
    reviewed = _verify_decisions(decisions)
    if reviewed.get("adjudication_schema_version") != MENTION_ADJUDICATION_SCHEMA:
        raise UnsupportedSchemaVersionError(
            "decisions adjudication_schema_version must be "
            f"{MENTION_ADJUDICATION_SCHEMA}"
        )
    if (
        reviewed.get("adjudication_logical_sha256")
        != document["logical_content_sha256"]
    ):
        raise InputHashChangedError(
            "decisions adjudication_logical_sha256 does not match "
            "the adjudication packet"
        )
    if reviewed.get("adjudication_file_sha256") != adjudication_file_sha256:
        raise InputHashChangedError(
            "decisions adjudication_file_sha256 does not match "
            "the adjudication packet file"
        )
    packet_ids = [case["case_id"] for case in document["cases"]]
    if len(set(packet_ids)) != len(packet_ids):
        raise ArtifactError("duplicate case id")
    seen: set[str] = set()
    for row in reviewed["decisions"]:
        case_id = row["case_id"]
        if case_id in seen:
            raise ArtifactError(f"duplicate case id {case_id}")
        seen.add(case_id)
        decision = row.get("decision")
        if decision is not None and decision not in DECISIONS:
            raise ArtifactError(f"decision {decision!r} is not an allowed value")
    unknown = sorted(seen - set(packet_ids))
    if unknown:
        raise ArtifactError(f"unknown case id {unknown[0]}")
    missing = [case_id for case_id in packet_ids if case_id not in seen]
    if missing:
        raise ArtifactError(f"missing case id {missing[0]}")
    return reviewed


def require_unique_case_ids(cases: list[dict]) -> None:
    """Fail when two packaged cases share a case id."""
    _require_unique_case_ids(cases)


def _verify_comparison(comparison: object) -> dict:
    document = require_object(comparison, "mention run comparison")
    if document.get("schema_version") != MENTION_RUN_COMPARISON_V3_SCHEMA:
        raise UnsupportedSchemaVersionError(
            "mention run comparison schema_version must be "
            f"{MENTION_RUN_COMPARISON_V3_SCHEMA}"
        )
    return verify_generated_artifact(
        document, MENTION_RUN_COMPARISON_V3_SCHEMA, "mention run comparison"
    )


def _verify_decisions(decisions: object) -> dict:
    document = require_object(decisions, "mention adjudication decisions")
    if document.get("schema_version") != MENTION_ADJUDICATION_DECISIONS_SCHEMA:
        raise UnsupportedSchemaVersionError(
            "mention adjudication decisions schema_version must be "
            f"{MENTION_ADJUDICATION_DECISIONS_SCHEMA}"
        )
    validate_artifact(document)
    return document


def _verify_packet(packet: object) -> dict:
    document = require_object(packet, "mention adjudication")
    if document.get("schema_version") != MENTION_ADJUDICATION_SCHEMA:
        raise UnsupportedSchemaVersionError(
            f"mention adjudication schema_version must be {MENTION_ADJUDICATION_SCHEMA}"
        )
    return verify_generated_artifact(
        document, MENTION_ADJUDICATION_SCHEMA, "mention adjudication"
    )


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ArtifactError(f"{label} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ArtifactError(f"{label} must be a SHA-256 hex digest") from exc


def _source_equivalence(items: object) -> dict[tuple[int, str], str]:
    if not isinstance(items, list):
        raise ArtifactError("malformed review geometry: items must be a list")
    found: dict[tuple[int, str], str] = {}
    for item in items:
        row = require_object(item, "comparison item")
        key = (
            _item_id(row.get("archive_item_id")),
            _source(row.get("evidence_source")),
        )
        if key in found:
            raise ArtifactError(
                "malformed review geometry: duplicate comparison source pair"
            )
        equivalence = row.get("source_equivalence")
        if not isinstance(equivalence, str) or not equivalence:
            raise ArtifactError(
                "malformed review geometry: source equivalence is missing"
            )
        found[key] = equivalence
    return found


def _review_rows(rows: object, equivalence: dict[tuple[int, str], str]) -> list[dict]:
    if not isinstance(rows, list):
        raise ArtifactError("malformed review geometry: review_rows must be a list")
    checked: list[dict] = []
    for entry in rows:
        row = require_object(entry, "comparison review row")
        category = row.get("category")
        if category not in _OCCURRENCE_CATEGORIES and category != DIAGNOSTIC_ONLY:
            raise ArtifactError(
                f"malformed review geometry: unknown review category {category!r}"
            )
        key = (
            _item_id(row.get("archive_item_id")),
            _source(row.get("evidence_source")),
        )
        if key not in equivalence:
            raise ArtifactError(
                "malformed review geometry: review row has no source pair"
            )
        if row.get("source_equivalence") != equivalence[key]:
            raise ArtifactError(
                "malformed review geometry: review row source equivalence "
                "does not match the comparison item"
            )
        reason = row.get("comparison_reason")
        if not isinstance(reason, str) or not reason:
            raise ArtifactError(
                "malformed review geometry: comparison reason is missing"
            )
        checked.append(row)
    return checked


def _diagnostic_rows(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        raise ArtifactError("malformed review geometry: diagnostic_rows must be a list")
    checked: list[dict] = []
    for entry in rows:
        note = require_object(entry, "diagnostic row")
        _diagnostic_note(note)
        checked.append(note)
    return checked


def _require_diagnostic_only_rows(
    rows: list[dict], diagnostic_rows: list[dict]
) -> None:
    known = {
        (
            note["archive_item_id"],
            note["evidence_source"],
            note["start_offset"],
            note["end_offset"],
            note["reason"],
        )
        for note in diagnostic_rows
        if isinstance(note, dict)
    }
    for row in rows:
        if row["category"] != DIAGNOSTIC_ONLY:
            continue
        key = (
            row["archive_item_id"],
            row["evidence_source"],
            row.get("official_start_offset"),
            row.get("official_end_offset"),
            row.get("diagnostic_reason"),
        )
        if key not in known:
            raise ArtifactError(
                "malformed review geometry: diagnostic review row has no "
                "diagnostic record"
            )


def _occurrence_cases(
    rows: list[dict], equivalence: dict[tuple[int, str], str]
) -> list[dict]:
    grouped: dict[tuple[int, str], list[dict]] = {}
    for row in rows:
        if row["category"] == DIAGNOSTIC_ONLY:
            continue
        key = (row["archive_item_id"], row["evidence_source"])
        grouped.setdefault(key, []).append(row)
    cases: list[dict] = []
    for key in sorted(grouped):
        cases.extend(_cases_for_source(key, grouped[key], equivalence[key]))
    return cases


def _cases_for_source(
    key: tuple[int, str], rows: list[dict], equivalence: str
) -> list[dict]:
    by_category: dict[str, list[dict]] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row)
    cases: list[dict] = []
    source_rows = [
        row
        for category in _NONCOMPARABLE_SOURCE_CATEGORIES
        for row in by_category.get(category, [])
    ]
    if by_category.get(OFFICIAL_SOURCE_NONCOMPARABLE) and by_category.get(
        PILOT_SOURCE_NONCOMPARABLE
    ):
        raise ArtifactError(
            "malformed review geometry: one source is both pilot-only and "
            "official-only non-comparable"
        )
    if source_rows:
        cases.append(
            _span_case(
                key,
                CASE_NONCOMPARABLE_SOURCE,
                equivalence,
                source_rows,
                pilot_spans=_collect_pilot_spans(source_rows),
                official_spans=_collect_official_spans(source_rows),
            )
        )
    for row in by_category.get(NON_COMPARABLE, []):
        cases.append(
            _span_case(
                key,
                CASE_NONCOMPARABLE_PILOT,
                equivalence,
                [row],
                pilot_spans=[_pilot_span(row, allow_missing_offsets=True)],
                official_spans=[],
            )
        )
    cases.extend(
        _one_span_cases(
            key,
            by_category.get(PILOT_ONLY, []),
            equivalence,
            CASE_PILOT_ONLY,
            pilot=True,
        )
    )
    cases.extend(
        _one_span_cases(
            key,
            by_category.get(OFFICIAL_ONLY, []),
            equivalence,
            CASE_OFFICIAL_ONLY,
            pilot=False,
        )
    )
    for row in by_category.get(BOTH_SAME_SPAN, []):
        pilot = _pilot_span(row)
        official = _official_span(row)
        if (pilot["start_offset"], pilot["end_offset"]) != (
            official["start_offset"],
            official["end_offset"],
        ):
            raise ArtifactError(
                "malformed review geometry: same-span row offsets differ"
            )
        cases.append(
            _span_case(
                key,
                _same_span_case_type(row, official),
                equivalence,
                [row],
                pilot_spans=[pilot],
                official_spans=[official],
            )
        )
    cases.extend(_overlap_cases(key, by_category.get(BOTH_OVERLAP, []), equivalence))
    return cases


def _one_span_cases(
    key: tuple[int, str],
    rows: list[dict],
    equivalence: str,
    case_type: str,
    *,
    pilot: bool,
) -> list[dict]:
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        span = _pilot_span(row) if pilot else _official_span(row)
        if pilot and _official_present(row):
            raise ArtifactError(
                "malformed review geometry: pilot-only row includes an official span"
            )
        if not pilot and _pilot_present(row):
            raise ArtifactError(
                "malformed review geometry: official-only row includes a pilot span"
            )
        span_key = (span["start_offset"], span["end_offset"], span["exact_mention"])
        grouped.setdefault(span_key, []).append(row)
    cases: list[dict] = []
    for span_key in sorted(grouped, key=lambda item: (item[0], item[1], item[2])):
        members = grouped[span_key]
        if pilot:
            spans = _collect_pilot_spans(members)
            official_spans: list[dict] = []
            pilot_spans = spans
        else:
            pilot_spans = []
            official_spans = _collect_official_spans(members)
        cases.append(
            _span_case(
                key,
                case_type,
                equivalence,
                members,
                pilot_spans=pilot_spans,
                official_spans=official_spans,
            )
        )
    return cases


def _overlap_cases(
    key: tuple[int, str], rows: list[dict], equivalence: str
) -> list[dict]:
    if not rows:
        return []
    pilots: dict[tuple[int, int], dict] = {}
    officials: dict[tuple[int, int], dict] = {}
    for row in rows:
        pilot_here = _pilot_present(row)
        official_here = _official_present(row)
        if not pilot_here and not official_here:
            raise ArtifactError("malformed review geometry: overlap row has no span")
        if pilot_here:
            span = _pilot_span(row)
            _store_span(pilots, span)
        if official_here:
            span = _official_span(row)
            _store_span(officials, span)
        if pilot_here and official_here:
            pilot_span = _pilot_span(row)
            official_span = _official_span(row)
            if (pilot_span["start_offset"], pilot_span["end_offset"]) == (
                official_span["start_offset"],
                official_span["end_offset"],
            ):
                raise ArtifactError(
                    "malformed review geometry: overlap row has equal offsets"
                )
            if not _overlaps(
                pilot_span["start_offset"],
                pilot_span["end_offset"],
                official_span["start_offset"],
                official_span["end_offset"],
            ):
                raise ArtifactError(
                    "malformed review geometry: overlap row spans do not overlap"
                )
    components = _connected_components(pilots, officials)
    cases: list[dict] = []
    assigned: set[int] = set()
    for component in components:
        pilot_keys = {span_key for side, span_key in component if side == "pilot"}
        official_keys = {span_key for side, span_key in component if side == "official"}
        if not pilot_keys or not official_keys:
            raise ArtifactError(
                "malformed review geometry: overlap component is missing a side"
            )
        member_indexes = [
            index
            for index, row in enumerate(rows)
            if _row_in_component(row, pilot_keys, official_keys)
        ]
        for index in member_indexes:
            if index in assigned:
                raise ArtifactError(
                    "malformed review geometry: overlap row belongs to two components"
                )
            assigned.add(index)
        members = [rows[index] for index in member_indexes]
        cases.append(
            _span_case(
                key,
                CASE_OVERLAP,
                equivalence,
                members,
                pilot_spans=[pilots[span_key] for span_key in sorted(pilot_keys)],
                official_spans=[
                    officials[span_key] for span_key in sorted(official_keys)
                ],
            )
        )
    if len(assigned) != len(rows):
        raise ArtifactError("malformed review geometry: overlap row was not grouped")
    return cases


def _connected_components(
    pilots: dict[tuple[int, int], dict], officials: dict[tuple[int, int], dict]
) -> list[list[tuple[str, tuple[int, int]]]]:
    nodes = [("pilot", key) for key in pilots]
    nodes.extend(("official", key) for key in officials)
    parent = {node: node for node in nodes}

    def find(node: tuple[str, tuple[int, int]]) -> tuple[str, tuple[int, int]]:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for pilot_key, pilot in pilots.items():
        for official_key, official in officials.items():
            if _overlaps(
                pilot["start_offset"],
                pilot["end_offset"],
                official["start_offset"],
                official["end_offset"],
            ):
                union(("pilot", pilot_key), ("official", official_key))
    grouped: dict[tuple, list] = {}
    for node in nodes:
        grouped.setdefault(find(node), []).append(node)
    return [
        grouped[key] for key in sorted(grouped, key=lambda item: (item[0], item[1]))
    ]


def _row_in_component(row: dict, pilot_keys: set, official_keys: set) -> bool:
    if _pilot_present(row):
        key = (_pilot_span(row)["start_offset"], _pilot_span(row)["end_offset"])
        if key in pilot_keys:
            return True
    if _official_present(row):
        key = (
            _official_span(row)["start_offset"],
            _official_span(row)["end_offset"],
        )
        if key in official_keys:
            return True
    return False


def _span_case(
    key: tuple[int, str],
    case_type: str,
    equivalence: str,
    rows: list[dict],
    *,
    pilot_spans: list[dict],
    official_spans: list[dict],
) -> dict:
    return _blank_case(
        archive_item_id=key[0],
        evidence_source=key[1],
        source_equivalence=equivalence,
        case_type=case_type,
        comparison_reason=_joined_reasons(rows),
        review_row_count=len(rows),
        pilot_spans=_sort_spans(pilot_spans),
        official_spans=_sort_spans(official_spans),
    )


def _blank_case(
    *,
    archive_item_id: int,
    evidence_source: str,
    source_equivalence: str,
    case_type: str,
    comparison_reason: str,
    review_row_count: int,
    pilot_spans: list[dict],
    official_spans: list[dict],
    diagnostics: list[dict] | None = None,
) -> dict:
    return {
        "archive_item_id": archive_item_id,
        "evidence_source": evidence_source,
        "source_equivalence": source_equivalence,
        "case_type": case_type,
        "comparison_reason": comparison_reason,
        "review_row_count": review_row_count,
        "pilot_spans": pilot_spans,
        "official_spans": official_spans,
        "diagnostics": [] if diagnostics is None else diagnostics,
        "review_status": REVIEW_PENDING,
        "decision": None,
        "reviewer_note": None,
    }


def _attach_diagnostics(cases: list[dict], notes: list[dict]) -> list[tuple[dict, str]]:
    unmatched: list[tuple[dict, str]] = []
    for note in notes:
        _diagnostic_note(note)
        exact: list[dict] = []
        overlapping: list[dict] = []
        for case in cases:
            if (case["archive_item_id"], case["evidence_source"]) != (
                note["archive_item_id"],
                note["evidence_source"],
            ):
                continue
            quality = _diagnostic_match(note, case)
            if quality == "exact":
                exact.append(case)
            elif quality == "overlap":
                overlapping.append(case)
        if len(exact) > 1:
            raise ArtifactError(
                "malformed review geometry: diagnostic matches multiple cases"
            )
        if len(exact) == 1:
            _add_diagnostic(exact[0], note)
            continue
        if len(overlapping) == 1:
            _add_diagnostic(overlapping[0], note)
            continue
        if len(overlapping) > 1:
            unmatched.append((note, "ambiguous"))
            continue
        unmatched.append((note, "none"))
    for case in cases:
        case["diagnostics"] = _sort_diagnostics(case["diagnostics"])
    return unmatched


def _diagnostic_match(note: dict, case: dict) -> str | None:
    start = note["start_offset"]
    end = note["end_offset"]
    if start is None or end is None:
        return None
    overlapped = False
    for span in [*case["pilot_spans"], *case["official_spans"]]:
        span_start = span["start_offset"]
        span_end = span["end_offset"]
        if span_start is None or span_end is None:
            continue
        if (span_start, span_end) == (start, end):
            return "exact"
        if _overlaps(start, end, span_start, span_end):
            overlapped = True
    if overlapped:
        return "overlap"
    return None


def _add_diagnostic(case: dict, note: dict) -> None:
    payload = _diagnostic_payload(note)
    if payload in case["diagnostics"]:
        return
    case["diagnostics"].append(payload)


def _standalone_diagnostic_cases(
    notes: list[tuple[dict, str]], equivalence: dict[tuple[int, str], str]
) -> list[dict]:
    cases: list[dict] = []
    for note, attachment in notes:
        key = (note["archive_item_id"], note["evidence_source"])
        if key not in equivalence:
            raise ArtifactError(
                "malformed review geometry: diagnostic has no source pair"
            )
        case_type = (
            CASE_SPLIT if note["reason"] == "SPLIT_CONFLICT" else CASE_DIAGNOSTIC
        )
        if attachment == "ambiguous":
            reason = (
                f"{note['section']} diagnostic {note['reason']} overlaps "
                "multiple mention cases and was not attached"
            )
        else:
            reason = (
                f"{note['section']} diagnostic {note['reason']} has no "
                "corresponding mention case"
            )
        cases.append(
            _blank_case(
                archive_item_id=key[0],
                evidence_source=key[1],
                source_equivalence=equivalence[key],
                case_type=case_type,
                comparison_reason=reason,
                review_row_count=0,
                pilot_spans=[],
                official_spans=[],
                diagnostics=[_diagnostic_payload(note)],
            )
        )
    return cases


def _same_span_case_type(row: dict, official: dict) -> str:
    if any(
        row.get(name) == "DIFFERENT"
        for name in (
            "name_surface_relation",
            "title_surface_relation",
            "surface_kind_relation",
        )
    ):
        return CASE_SAME_SPAN
    if _known_name_only(official["detector_ids"]):
        return CASE_SAME_SPAN_KNOWN_NAME
    return CASE_SAME_SPAN


def _known_name_only(detector_ids: list[str]) -> bool:
    found = set(detector_ids)
    return KNOWN_NAME_DETECTOR_ID in found and SEMANTIC_DETECTOR_ID not in found


def _collect_pilot_spans(rows: list[dict]) -> list[dict]:
    spans: list[dict] = []
    for row in rows:
        if _pilot_present(row):
            spans.append(_pilot_span(row, allow_missing_offsets=True))
    return _dedupe_spans(spans)


def _collect_official_spans(rows: list[dict]) -> list[dict]:
    spans: list[dict] = []
    for row in rows:
        if _official_present(row):
            spans.append(_official_span(row))
    return _dedupe_spans(spans)


def _store_span(found: dict[tuple[int, int], dict], span: dict) -> None:
    key = (span["start_offset"], span["end_offset"])
    previous = found.get(key)
    if previous is None:
        found[key] = span
        return
    if previous != span:
        raise ArtifactError(
            "malformed review geometry: one offset pair has two surfaces"
        )


def _dedupe_spans(spans: list[dict]) -> list[dict]:
    found: dict[tuple, dict] = {}
    for span in spans:
        key = (
            span["start_offset"],
            span["end_offset"],
            span["exact_mention"],
        )
        previous = found.get(key)
        if previous is None:
            found[key] = span
            continue
        if previous != span:
            raise ArtifactError(
                "malformed review geometry: one span has conflicting surfaces"
            )
    return list(found.values())


def _pilot_present(row: dict) -> bool:
    return any(
        row.get(name) is not None
        for name in (
            "pilot_start_offset",
            "pilot_end_offset",
            "pilot_exact_mention",
        )
    )


def _official_present(row: dict) -> bool:
    if row.get("category") == DIAGNOSTIC_ONLY:
        return False
    return any(
        row.get(name) is not None
        for name in (
            "official_start_offset",
            "official_end_offset",
            "official_exact_mention",
        )
    )


def _pilot_span(row: dict, *, allow_missing_offsets: bool = False) -> dict:
    start, end = _offset_pair(
        row.get("pilot_start_offset"),
        row.get("pilot_end_offset"),
        allow_missing=allow_missing_offsets,
        label="pilot",
    )
    exact = row.get("pilot_exact_mention")
    if exact is not None and not isinstance(exact, str):
        raise ArtifactError("malformed review geometry: pilot exact mention")
    if start is not None and not isinstance(exact, str):
        raise ArtifactError(
            "malformed review geometry: pilot span is missing exact mention"
        )
    return {
        "start_offset": start,
        "end_offset": end,
        "exact_mention": exact,
        "name_surface": _optional_text(row.get("pilot_name_surface"), "pilot name"),
        "title_surface": _optional_text(row.get("pilot_title_surface"), "pilot title"),
        "surface_kind": _optional_text(row.get("pilot_surface_kind"), "pilot kind"),
    }


def _official_span(row: dict) -> dict:
    start, end = _offset_pair(
        row.get("official_start_offset"),
        row.get("official_end_offset"),
        allow_missing=False,
        label="official",
    )
    exact = row.get("official_exact_mention")
    if not isinstance(exact, str):
        raise ArtifactError(
            "malformed review geometry: official span is missing exact mention"
        )
    detectors = row.get("official_detector_ids")
    if not isinstance(detectors, list) or any(
        not isinstance(item, str) or not item for item in detectors
    ):
        raise ArtifactError(
            "malformed review geometry: official detector ids are missing"
        )
    quality = row.get("known_name_match_quality")
    if quality not in {None, "EXACT_BOUNDARY", "PREFIXED"}:
        raise ArtifactError(
            "malformed review geometry: known-name match quality is invalid"
        )
    return {
        "start_offset": start,
        "end_offset": end,
        "exact_mention": exact,
        "name_surface": _optional_text(
            row.get("official_name_surface"), "official name"
        ),
        "title_surface": _optional_text(
            row.get("official_title_surface"), "official title"
        ),
        "surface_kind": _optional_text(
            row.get("official_surface_kind"), "official kind"
        ),
        "detector_ids": list(detectors),
        "known_name_match_quality": quality,
    }


def _offset_pair(
    start: object, end: object, *, allow_missing: bool, label: str
) -> tuple[int | None, int | None]:
    if start is None and end is None:
        if allow_missing:
            return None, None
        raise ArtifactError(
            f"malformed review geometry: {label} span is missing offsets"
        )
    if type(start) is not int or type(end) is not int or start < 0 or end < 0:
        raise ArtifactError(f"malformed review geometry: {label} offsets are invalid")
    if start >= end:
        raise ArtifactError(
            f"malformed review geometry: {label} offsets are not ordered"
        )
    return start, end


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ArtifactError(f"malformed review geometry: {label} surface is invalid")
    return value


def _diagnostic_note(note: dict) -> None:
    if not isinstance(note, dict):
        raise ArtifactError("malformed review geometry: diagnostic row")
    for name in (
        "section",
        "reason",
        "surface",
        "archive_item_id",
        "evidence_source",
        "published_official_span",
    ):
        if name not in note:
            raise ArtifactError(
                "malformed review geometry: diagnostic row is incomplete"
            )
    segment = note.get("segment_id")
    if "segment_id" not in note or (
        segment is not None and not isinstance(segment, str)
    ):
        raise ArtifactError("malformed review geometry: diagnostic segment id")
    if note["section"] not in {"rejected", "suppressed", "conflicts"}:
        raise ArtifactError("malformed review geometry: diagnostic section")
    if not isinstance(note["reason"], str) or not note["reason"]:
        raise ArtifactError("malformed review geometry: diagnostic reason")
    if not isinstance(note["surface"], str):
        raise ArtifactError("malformed review geometry: diagnostic surface")
    if not isinstance(note["published_official_span"], bool):
        raise ArtifactError("malformed review geometry: published-official-span flag")
    start = note.get("start_offset")
    end = note.get("end_offset")
    if start is None and end is None:
        return
    if type(start) is not int or type(end) is not int or start < 0 or start >= end:
        raise ArtifactError("malformed review geometry: diagnostic offsets")


def _diagnostic_payload(note: dict) -> dict:
    segment = note.get("segment_id")
    if segment is not None and not isinstance(segment, str):
        raise ArtifactError("malformed review geometry: diagnostic segment id")
    return {
        "section": note["section"],
        "reason": note["reason"],
        "surface": note["surface"],
        "start_offset": note["start_offset"],
        "end_offset": note["end_offset"],
        "segment_id": segment,
        "published_official_span": note["published_official_span"],
    }


def _sort_spans(spans: list[dict]) -> list[dict]:
    return sorted(
        spans,
        key=lambda span: (
            span["start_offset"] if span["start_offset"] is not None else -1,
            span["end_offset"] if span["end_offset"] is not None else -1,
            span["exact_mention"] or "",
        ),
    )


def _sort_diagnostics(notes: list[dict]) -> list[dict]:
    return sorted(
        notes,
        key=lambda note: (
            note["section"],
            note["reason"],
            note["start_offset"] if note["start_offset"] is not None else -1,
            note["end_offset"] if note["end_offset"] is not None else -1,
            note["surface"],
            note["segment_id"] or "",
        ),
    )


def _joined_reasons(rows: list[dict]) -> str:
    reasons = sorted({row["comparison_reason"] for row in rows})
    return " | ".join(reasons)


def _overlaps(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and right_start < left_end


def _case_sort_key(case: dict) -> tuple:
    anchors = [*case["pilot_spans"], *case["official_spans"], *case["diagnostics"]]
    start = -1
    end = -1
    text = ""
    if anchors:
        first = min(
            anchors,
            key=lambda span: (
                span["start_offset"] if span.get("start_offset") is not None else -1,
                span["end_offset"] if span.get("end_offset") is not None else -1,
                span.get("exact_mention") or span.get("surface") or "",
            ),
        )
        if first.get("start_offset") is not None:
            start = first["start_offset"]
        if first.get("end_offset") is not None:
            end = first["end_offset"]
        text = first.get("exact_mention") or first.get("surface") or ""
    return (
        case["archive_item_id"],
        case["evidence_source"],
        start,
        end,
        case["case_type"],
        text,
        case["comparison_reason"],
    )


def _case_geometry(case: dict) -> tuple:
    def span_key(span: dict) -> tuple:
        return (
            span["start_offset"],
            span["end_offset"],
            span["exact_mention"],
            span.get("name_surface"),
            span.get("title_surface"),
            span.get("surface_kind"),
            tuple(span.get("detector_ids") or []),
            span.get("known_name_match_quality"),
        )

    def note_key(note: dict) -> tuple:
        return (
            note["section"],
            note["reason"],
            note["start_offset"],
            note["end_offset"],
            note["surface"],
            note["segment_id"],
            note["published_official_span"],
        )

    return (
        case["archive_item_id"],
        case["evidence_source"],
        case["case_type"],
        tuple(span_key(span) for span in case["pilot_spans"]),
        tuple(span_key(span) for span in case["official_spans"]),
        tuple(note_key(note) for note in case["diagnostics"]),
    )


def _require_distinct_case_geometry(cases: list[dict]) -> None:
    seen: set[tuple] = set()
    for case in cases:
        identity = _case_geometry(case)
        if identity in seen:
            raise ArtifactError("duplicate case geometry")
        seen.add(identity)


def _require_unique_case_ids(cases: list[dict]) -> None:
    seen: set[str] = set()
    for case in cases:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ArtifactError("duplicate case id")
        if case_id in seen:
            raise ArtifactError("duplicate case id")
        seen.add(case_id)


def _assemble(
    comparison: dict,
    *,
    comparison_file_sha256: str,
    cases: list[dict],
    review_rows: list[dict],
    raw_diagnostic_count: int,
    unique_diagnostic_count: int,
) -> dict:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["case_type"]] = counts.get(case["case_type"], 0) + 1
    items = sorted({case["archive_item_id"] for case in cases})
    pairs = sorted(
        {(case["archive_item_id"], case["evidence_source"]) for case in cases}
    )
    overlap_rows = sum(1 for row in review_rows if row["category"] == BOTH_OVERLAP)
    overlap_cases = counts.get(CASE_OVERLAP, 0)
    return {
        "schema_version": MENTION_ADJUDICATION_SCHEMA,
        "kind": ADJUDICATION_KIND,
        "operational_pipeline": False,
        "pilot_is_ground_truth": False,
        "precision_recall_computed": False,
        "production_write_performed": False,
        "human_approval_performed": False,
        "apply_authorized": False,
        "tuning_performed": False,
        "adjudication_command": ADJUDICATION_COMMAND,
        "comparison_schema_version": MENTION_RUN_COMPARISON_V3_SCHEMA,
        "comparison_logical_sha256": comparison["logical_content_sha256"],
        "comparison_file_sha256": comparison_file_sha256,
        "summary": {
            "logical_case_count": len(cases),
            "case_type_counts": [
                {"case_type": case_type, "count": counts[case_type]}
                for case_type in sorted(counts)
            ],
            "items_represented": items,
            "item_count": len(items),
            "source_pairs_represented": [
                {"archive_item_id": item_id, "evidence_source": source}
                for item_id, source in pairs
            ],
            "source_pair_count": len(pairs),
            "cases_with_diagnostics": sum(1 for case in cases if case["diagnostics"]),
            "cases_with_known_name_only_official_detection": sum(
                1
                for case in cases
                if any(
                    _known_name_only(span["detector_ids"])
                    for span in case["official_spans"]
                )
            ),
            "cases_involving_split_conflicts": sum(
                1
                for case in cases
                if case["case_type"] == CASE_SPLIT
                or any(
                    note["reason"] == "SPLIT_CONFLICT" for note in case["diagnostics"]
                )
            ),
            "noncomparable_case_count": counts.get(CASE_NONCOMPARABLE_SOURCE, 0)
            + counts.get(CASE_NONCOMPARABLE_PILOT, 0),
            "overlap_review_row_count": overlap_rows,
            "overlap_logical_case_count": overlap_cases,
            "overlap_review_rows_collapsed": overlap_rows - overlap_cases,
            "raw_diagnostic_record_count": raw_diagnostic_count,
            "unique_diagnostic_occurrence_count": unique_diagnostic_count,
        },
        "cases": cases,
        "decisions": list(DECISIONS),
        "limitations": [
            "Comparison review rows are not human cases one-to-one. "
            "Directional overlap rows in one connected component are one case.",
            "Diagnostics are evidence attached to a related occurrence. "
            "A diagnostic attaches to the one case with an exact span match, "
            "or to the one case it overlaps when no span is exact. If it "
            "overlaps more than one case, it stays a separate case and is not "
            "assigned. Duplicate raw diagnostic records are collapsed to one "
            "unique occurrence.",
            "Every case starts review_status PENDING with a null decision. "
            "Diagnostics and known-name support do not choose a decision.",
            "Human decisions belong in mention-adjudication-decisions-v1. "
            "That file is valid for later use only when it names each case id "
            "once and its packet hashes match. Filling it does not tune the "
            "finder, retire the pilot or v1, change production identity, or "
            "authorize apply.",
            "Pilot output is not ground truth. No score is computed for either output.",
        ],
    }


def _item_id(value: object) -> int:
    if type(value) is not int:
        raise ArtifactError("malformed review geometry: archive item id")
    return value


def _source(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ArtifactError("malformed review geometry: evidence source")
    return value
