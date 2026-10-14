"""Markdown review report for mention-adjudication-v1.

The report shows exact source strings. It does not decide a case.
"""

from __future__ import annotations


def render_adjudication_report(document: dict) -> str:
    """Render one compact section per logical adjudication case."""
    summary = document["summary"]
    lines = [
        "# Mention adjudication",
        "",
        "These are pending human-review cases. A comparison row is not a case.",
        "Overlap components are grouped. Diagnostics are evidence, not decisions.",
        "Neither the pilot nor the official finder is treated as correct.",
        "No score is computed for either output.",
        "",
        "Record decisions in the separate mention-adjudication-decisions-v1 file.",
        "This packet stays pending.",
        "",
        "## Provenance",
        "",
        f"- Schema: `{document['schema_version']}`",
        f"- Kind: `{document['kind']}`",
        f"- Command: `{document['adjudication_command']}`",
        (
            "- Producer: "
            f"`{document['producer']['name']} {document['producer']['version']}`"
        ),
        f"- Logical hash: `{document['logical_content_sha256']}`",
        f"- Comparison schema: `{document['comparison_schema_version']}`",
        f"- Comparison logical hash: `{document['comparison_logical_sha256']}`",
        f"- Comparison file hash: `{document['comparison_file_sha256']}`",
        "",
        "## Summary",
        "",
        f"- Logical cases: {summary['logical_case_count']}",
        f"- Items represented: {summary['item_count']}",
        f"- Source pairs represented: {summary['source_pair_count']}",
        f"- Cases with diagnostics: {summary['cases_with_diagnostics']}",
        (
            "- Cases with known-name-only official detection: "
            f"{summary['cases_with_known_name_only_official_detection']}"
        ),
        (
            "- Cases involving split conflicts: "
            f"{summary['cases_involving_split_conflicts']}"
        ),
        f"- Non-comparable cases: {summary['noncomparable_case_count']}",
        f"- Overlap review rows: {summary['overlap_review_row_count']}",
        f"- Overlap logical cases: {summary['overlap_logical_case_count']}",
        (
            "- Overlap review rows collapsed into logical cases: "
            f"{summary['overlap_review_rows_collapsed']}"
        ),
        f"- Raw diagnostic records: {summary['raw_diagnostic_record_count']}",
        (
            "- Unique diagnostic occurrences: "
            f"{summary['unique_diagnostic_occurrence_count']}"
        ),
        "",
        "| Case type | Count |",
        "| --- | --- |",
    ]
    if summary["case_type_counts"]:
        for row in summary["case_type_counts"]:
            lines.append(f"| `{row['case_type']}` | {row['count']} |")
    else:
        lines.append("| (none) | 0 |")
    lines.extend(["", "## Cases", ""])
    if not document["cases"]:
        lines.append("No logical cases.")
        lines.append("")
    for case in document["cases"]:
        lines.extend(_case_section(case))
    lines.extend(["## Limits", ""])
    for limitation in document["limitations"]:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


def _case_section(case: dict) -> list[str]:
    lines = [
        f"## `{case['case_id']}`",
        "",
        f"- Archive item: {case['archive_item_id']}",
        f"- Evidence source: {_inline(case['evidence_source'])}",
        f"- Source equivalence: `{case['source_equivalence']}`",
        f"- Case type: `{case['case_type']}`",
        f"- Comparison reason: {_inline(case['comparison_reason'])}",
        f"- Review rows grouped: {case['review_row_count']}",
        "",
        "### Pilot",
        "",
    ]
    lines.extend(_spans(case["pilot_spans"], official=False))
    lines.extend(["", "### Official", ""])
    lines.extend(_spans(case["official_spans"], official=True))
    lines.extend(["", "### Diagnostics", ""])
    if not case["diagnostics"]:
        lines.extend(["(none)", ""])
    for index, note in enumerate(case["diagnostics"], start=1):
        lines.extend(
            [
                (
                    f"{index}. `{note['section']}` / `{note['reason']}` / "
                    f"{_offsets(note['start_offset'], note['end_offset'])} / "
                    f"segment {_inline(note['segment_id'])} / "
                    f"published official span `{note['published_official_span']}`"
                ),
                "",
                _fence(note["surface"]),
                "",
            ]
        )
    lines.extend(
        [
            "### Decision",
            "",
            f"- review_status: `{case['review_status']}`",
            "- decision: null",
            "- reviewer_note: null",
            "",
            "Leave this packet unchanged. Write the decision in the decision file.",
            "",
        ]
    )
    return lines


def _spans(spans: list[dict], *, official: bool) -> list[str]:
    if not spans:
        return ["(none)"]
    lines: list[str] = []
    for index, span in enumerate(spans, start=1):
        lines.append(f"{index}. {_offsets(span['start_offset'], span['end_offset'])}")
        lines.append(f"- Name surface: {_inline(span['name_surface'])}")
        lines.append(f"- Title surface: {_inline(span['title_surface'])}")
        lines.append(f"- Surface kind: {_inline(span['surface_kind'])}")
        if official:
            detectors = ", ".join(span["detector_ids"]) or "(none)"
            lines.append(f"- Detector ids: {_inline(detectors)}")
            lines.append(
                "- Known-name match quality: "
                f"{_inline(span['known_name_match_quality'])}"
            )
        lines.extend(["", _fence(span["exact_mention"]), ""])
    return lines


def _offsets(start: int | None, end: int | None) -> str:
    if start is None or end is None:
        return "offsets unavailable"
    return f"`{start}-{end}`"


def _inline(value: object) -> str:
    if value is None:
        return "`null`"
    text = str(value)
    fence = "`"
    while fence in text:
        fence += "`"
    return f"{fence}{text}{fence}"


def _fence(value: object) -> str:
    if value is None:
        return "`null`"
    text = str(value)
    marker = "```"
    while marker in text:
        marker += "`"
    return f"{marker}\n{text}\n{marker}"
