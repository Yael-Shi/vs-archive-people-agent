"""Markdown review report for mention-run-comparison-v3.

The report restates the comparison artifact. It does not decide which span
is correct.
"""

from __future__ import annotations


def render_comparison_report(document: dict) -> str:
    """Render a deterministic Markdown review packet from a comparison artifact."""
    metrics = document["metrics"]
    contribution = document["detector_contribution"]
    lines = [
        "# Mention run comparison",
        "",
        "This report compares stored mention spans. It does not decide which",
        "result is correct, and it does not treat the pilot as ground truth.",
        "",
        "## Provenance",
        "",
        f"- Schema: `{document['schema_version']}`",
        f"- Kind: `{document['kind']}`",
        f"- Command: `{document['comparison_command']}`",
        f"- Producer: `{document['producer']['name']} {document['producer']['version']}`",
        f"- Logical hash: `{document['logical_content_sha256']}`",
        f"- Pilot file hash: `{document['pilot_file_sha256']}`",
        f"- Pilot kind: `{document['pilot_kind']}`",
        f"- Proposal file hash: `{document['mention_proposals_file_sha256']}`",
        f"- Proposal logical hash: `{document['mention_proposals_logical_sha256']}`",
        f"- Proposal schema: `{document['mention_proposals_schema_version']}`",
        f"- Proposal run id: `{document['proposal_run_id']}`",
        f"- Diagnostics available: {document['diagnostics_available']}",
        f"- Discovery available: {document['discovery_available']}",
        f"- Discovery file hash: `{_or_none(document['discovery_file_sha256'])}`",
        f"- Diagnostics file hash: `{_or_none(document['diagnostics_file_sha256'])}`",
        (
            "- Diagnostics logical hash: "
            f"`{_or_none(document['diagnostics_logical_sha256'])}`"
        ),
        (
            "- Document snapshot file hash: "
            f"`{_or_none(document['document_snapshot_file_sha256'])}`"
        ),
        f"- Official snapshot file matches: {document['official_snapshot_file_matches']}",
        f"- Source equivalence: `{document['source_equivalence']}`",
        (
            "- Comparable source pairs: "
            f"`{document['source_equivalence_summary']['comparable_pairs_status']}`"
        ),
        (
            "- Non-comparable source pairs: "
            f"{document['source_equivalence_summary']['noncomparable_source_pair_count']}"
        ),
        f"- {document['source_equivalence_summary']['semantics']}",
        "",
        "## Historical corpus recovery",
        "",
        document["historical_corpus_recovery"]["statement"],
        "",
        (
            "- Recovered item count: "
            f"{document['historical_corpus_recovery']['recovered_item_count']}"
        ),
        (
            "- Recovered order: "
            + _id_list(
                document["historical_corpus_recovery"][
                    "archive_item_ids_in_input_order"
                ]
            )
        ),
        (
            "- Pilot result used for that recovery: "
            f"`{document['historical_corpus_recovery']['pilot_result']}`"
        ),
        (
            "- Additional recoverable corpus artifact found: "
            f"{document['historical_corpus_recovery']['additional_recoverable_corpus_artifact_found']}"
        ),
        (
            "- This comparison matches that recovered corpus: "
            f"{document['historical_corpus_recovery']['compared_corpus_matches_recovered_corpus']}"
        ),
        "",
        "## Corpus",
        "",
        f"- Archive item count: {document['corpus']['item_count']}",
        ("- Archive item IDs: " + _id_list(document["corpus"]["archive_item_ids"])),
        (
            "- Pilot input order: "
            + _id_list(document["corpus"]["pilot_archive_item_ids_in_input_order"])
        ),
        "",
        "| Archive item | Evidence source | Source equivalence |",
        "| --- | --- | --- |",
    ]
    for item in document["items"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item["archive_item_id"]),
                    _cell(item["evidence_source"]),
                    _cell(item["source_equivalence"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Metrics",
            "",
            "These counts describe the two outputs. They are not precision or recall.",
            "`overlap_different_span_count` counts pilot and official mention rows",
            "inside overlap-different-span groups. One pilot row overlapping one",
            "official row counts as 2, not as one adjudication case.",
            "",
            "| Metric | Value |",
            "| --- | --- |",
        ]
    )
    for key in _METRIC_ORDER:
        lines.append(f"| `{key}` | {_or_none(metrics[key])} |")
    lines.extend(
        [
            "",
            "## Detector contribution",
            "",
            "Detector contribution is not identity correctness.",
            "",
            "| Metric | Value |",
            "| --- | --- |",
        ]
    )
    for key in _DETECTOR_ORDER:
        lines.append(f"| `{key}` | {contribution[key]} |")
    lines.extend(["", "## Diagnostics", ""])
    if not document["diagnostics_available"]:
        lines.append("Diagnostics were not supplied. Counts are unavailable.")
    elif not document["diagnostic_counts"]:
        lines.append("Diagnostics were supplied and contain no rows.")
    else:
        lines.extend(
            [
                "| Section | Reason | Count |",
                "| --- | --- | --- |",
            ]
        )
        for row in document["diagnostic_counts"]:
            lines.append(f"| `{row['section']}` | `{row['reason']}` | {row['count']} |")
        lines.extend(
            [
                "",
                "Unique diagnostic occurrences collapse exact duplicate records.",
                "The identity is archive item, evidence source, section, reason,",
                "offsets, surface, and segment id. Raw rows stay in the JSON.",
                "",
                "| Section | Reason | Unique count |",
                "| --- | --- | --- |",
            ]
        )
        for row in document["unique_diagnostic_counts"]:
            lines.append(f"| `{row['section']}` | `{row['reason']}` | {row['count']} |")
    lines.extend(["", "## Limitations", ""])
    for limitation in document["limitations"]:
        lines.append(f"- {limitation}")
    lines.extend(
        [
            "",
            "## Review rows",
            "",
            "Each row is a disagreement, a diagnostic-only span, or a",
            "known-name-only official detection. The comparison reason is",
            "descriptive.",
            "",
        ]
    )
    lines.extend(_review_table(document["review_rows"]))
    lines.extend(["", "## Diagnostic context", ""])
    if not document["diagnostic_rows"]:
        lines.append("No diagnostic rows are included.")
    else:
        lines.extend(
            [
                "| Item | Source | Section | Reason | Offsets | Surface | Published |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for row in document["diagnostic_rows"]:
            offsets = _span(row["start_offset"], row["end_offset"])
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(row["archive_item_id"]),
                        _cell(row["evidence_source"]),
                        _cell(row["section"]),
                        _cell(row["reason"]),
                        offsets,
                        _cell(row["surface"]),
                        "yes" if row["published_official_span"] else "no",
                    ]
                )
                + " |"
            )
    lines.append("")
    return "\n".join(lines)


_METRIC_ORDER = (
    "corpus_item_count",
    "pilot_mention_count",
    "official_mention_count",
    "comparable_pilot_mention_count",
    "non_comparable_pilot_mention_count",
    "exact_span_agreement_count",
    "overlap_different_span_count",
    "official_only_count",
    "pilot_only_count",
    "source_pairs_comparable",
    "source_pairs_noncomparable",
    "source_pairs_with_any_disagreement",
    "official_noncomparable_source_mention_count",
    "pilot_noncomparable_source_mention_count",
    "same_span_name_surface_differs_count",
    "same_span_title_surface_differs_count",
    "same_span_surface_kind_differs_count",
    "items_with_zero_pilot_mentions",
    "items_with_zero_official_mentions",
    "items_with_any_disagreement",
    "items_with_source_drift",
    "items_source_equivalent",
    "items_source_equivalence_unknown",
    "split_conflict_count",
    "absorb_ambiguous_count",
    "diagnostic_only_span_count",
    "raw_diagnostic_record_count",
    "unique_diagnostic_occurrence_count",
    "unique_diagnostic_only_span_count",
)

_DETECTOR_ORDER = (
    "semantic_only_published_count",
    "known_name_only_published_count",
    "both_detectors_published_count",
    "other_detector_published_count",
    "known_name_exact_boundary_count",
    "known_name_prefixed_count",
)


def _review_table(rows: list[dict]) -> list[str]:
    if not rows:
        return ["No review rows."]
    header = (
        "| Item | Source | Source equivalence | Category | Reason | "
        "Pilot span | Pilot text | Official span | Official exact mention | "
        "Official name | Official title | Official kind | Detectors | "
        "Known-name quality | Diagnostic |"
    )
    table = [
        header,
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        table.append(
            "| "
            + " | ".join(
                [
                    str(row["archive_item_id"]),
                    _cell(row["evidence_source"]),
                    _cell(row["source_equivalence"]),
                    _cell(row["category"]),
                    _cell(row["comparison_reason"]),
                    _span(row["pilot_start_offset"], row["pilot_end_offset"]),
                    _cell(row["pilot_exact_mention"]),
                    _span(row["official_start_offset"], row["official_end_offset"]),
                    _cell(row["official_exact_mention"]),
                    _cell(row["official_name_surface"]),
                    _cell(row["official_title_surface"]),
                    _cell(row["official_surface_kind"]),
                    _cell(",".join(row["official_detector_ids"])),
                    _cell(row["known_name_match_quality"]),
                    _cell(row["diagnostic_reason"]),
                ]
            )
            + " |"
        )
    return table


def _id_list(values: list[int]) -> str:
    if not values:
        return "(none)"
    return ", ".join(str(value) for value in values)


def _span(start: int | None, end: int | None) -> str:
    if start is None or end is None:
        return ""
    return f"{start}-{end}"


def _cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    text = text.replace("\u2028", " ").replace("\u2029", " ")
    return text.replace("|", "\\|")


def _or_none(value: object) -> str:
    if value is None:
        return "null"
    return str(value)
