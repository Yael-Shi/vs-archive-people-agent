import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from test_mention_run_comparison import (
    CREATED,
    FILE_SHA,
    SOURCE,
    compare,
    official_mention,
    pilot_bundle,
    pilot_candidate,
    pilot_item,
    proposal,
)
from vs_archive_people.artifacts.io import (
    ArtifactError,
    InputHashChangedError,
    LogicalHashMismatchError,
    UnsupportedSchemaVersionError,
    sha256_file,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import stamp_generated_artifact
from vs_archive_people.artifacts.schema_validate import step_chain, validate_artifact
from vs_archive_people.cli import main
from vs_archive_people.evaluation.adjudicate import (
    _attach_diagnostics,
    build_mention_adjudication,
    build_mention_adjudication_decisions,
    require_mention_adjudication_decisions,
    require_unique_case_ids,
)
from vs_archive_people.evaluation.adjudication_report import render_adjudication_report
from vs_archive_people.evaluation.compare import build_mention_run_comparison
from vs_archive_people.find_mentions.run import diagnostics_artifact

TRANSLATION = "ocr_document.displayed_hebrew_translation.text"


def package(document: dict) -> dict:
    return build_mention_adjudication(
        document,
        comparison_file_sha256=FILE_SHA,
        created_at=CREATED,
    )


def types(packet: dict) -> list[str]:
    return [case["case_type"] for case in packet["cases"]]


def _pilot_case(start: int, end: int, text: str) -> dict:
    return {
        "archive_item_id": 10,
        "evidence_source": SOURCE,
        "case_type": "PILOT_ONLY",
        "pilot_spans": [
            {
                "start_offset": start,
                "end_offset": end,
                "exact_mention": text,
                "name_surface": text,
                "title_surface": None,
                "surface_kind": "FULL_NAME",
            }
        ],
        "official_spans": [],
        "diagnostics": [],
    }


def _diagnostic(start: int, end: int) -> dict:
    return {
        "section": "suppressed",
        "archive_item_id": 10,
        "evidence_source": SOURCE,
        "surface": "Ada",
        "reason": "EMBEDDED_KNOWN_NAME",
        "segment_id": "S0001",
        "start_offset": start,
        "end_offset": end,
        "published_official_span": False,
    }


def _diagnostic_locations(packet: dict) -> list[tuple]:
    return sorted(
        (
            case["case_type"],
            note["reason"],
            note["surface"],
            note["start_offset"],
            note["end_offset"],
        )
        for case in packet["cases"]
        for note in case["diagnostics"]
    )


def _note(
    surface: str,
    start: int | None,
    end: int | None,
    *,
    reason: str,
    section: str = "suppressed",
    segment: str | None = "S0001",
) -> dict:
    row = {
        "archive_item_id": 10,
        "evidence_source": SOURCE,
        "surface": surface,
        "reason": reason,
        "segment_id": segment,
        "_section": section,
    }
    if start is not None:
        row["start_offset"] = start
        row["end_offset"] = end
    return row


class MentionAdjudicationTests(unittest.TestCase):
    def test_pilot_only_row_is_one_case(self):
        packet = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
                [official_mention("Dana", 10)],
            )
        )
        pilot = [case for case in packet["cases"] if case["case_type"] == "PILOT_ONLY"]
        self.assertEqual(len(pilot), 1)
        self.assertEqual(pilot[0]["pilot_spans"][0]["exact_mention"], "Ada")
        self.assertEqual(pilot[0]["official_spans"], [])
        self.assertEqual(pilot[0]["review_status"], "PENDING")
        self.assertIsNone(pilot[0]["decision"])
        self.assertIsNone(pilot[0]["reviewer_note"])
        self.assertFalse(packet["pilot_is_ground_truth"])
        self.assertFalse(packet["precision_recall_computed"])
        self.assertFalse(packet["tuning_performed"])
        self.assertFalse(packet["apply_authorized"])
        self.assertNotIn("package-mention-adjudication", step_chain())

    def test_official_only_row_is_one_case(self):
        packet = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
                [official_mention("Dana", 10)],
            )
        )
        official = [
            case for case in packet["cases"] if case["case_type"] == "OFFICIAL_ONLY"
        ]
        self.assertEqual(len(official), 1)
        self.assertEqual(official[0]["official_spans"][0]["exact_mention"], "Dana")
        self.assertEqual(official[0]["pilot_spans"], [])

    def test_directional_overlap_pair_is_one_case(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana Cohen", 0)])]),
            [official_mention("Dana", 0)],
        )
        self.assertEqual(
            sum(
                1
                for row in document["review_rows"]
                if row["category"] == "BOTH_OVERLAP_DIFFERENT_SPAN"
            ),
            2,
        )
        packet = package(document)
        overlaps = [
            case
            for case in packet["cases"]
            if case["case_type"] == "OVERLAP_DIFFERENT_SPAN"
        ]
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["review_row_count"], 2)
        self.assertEqual(overlaps[0]["pilot_spans"][0]["exact_mention"], "Dana Cohen")
        self.assertEqual(overlaps[0]["official_spans"][0]["exact_mention"], "Dana")
        self.assertEqual(packet["summary"]["overlap_review_row_count"], 2)
        self.assertEqual(packet["summary"]["overlap_logical_case_count"], 1)
        self.assertEqual(packet["summary"]["overlap_review_rows_collapsed"], 1)

    def test_multi_row_overlap_component_is_one_case(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Max", 1)])]),
            [official_mention("xMax", 0), official_mention("xMax B", 0)],
        )
        packet = package(document)
        overlaps = [
            case
            for case in packet["cases"]
            if case["case_type"] == "OVERLAP_DIFFERENT_SPAN"
        ]
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(len(overlaps[0]["pilot_spans"]), 1)
        self.assertEqual(len(overlaps[0]["official_spans"]), 2)
        self.assertGreater(overlaps[0]["review_row_count"], 1)
        self.assertEqual(
            packet["summary"]["overlap_review_rows_collapsed"],
            packet["summary"]["overlap_review_row_count"] - 1,
        )

    def test_same_span_surface_difference_is_one_case(self):
        packet = package(
            compare(
                pilot_bundle(
                    [pilot_item(10, [pilot_candidate("Dana Cohen", 0, name="Dana")])]
                ),
                [official_mention("Dana Cohen", 0)],
            )
        )
        self.assertEqual(types(packet), ["SAME_SPAN_SURFACE_DIFFERENCE"])
        case = packet["cases"][0]
        self.assertEqual(case["pilot_spans"][0]["name_surface"], "Dana")
        self.assertEqual(case["official_spans"][0]["name_surface"], "Dana Cohen")
        self.assertEqual(case["pilot_spans"][0]["start_offset"], 0)
        self.assertEqual(case["official_spans"][0]["end_offset"], len("Dana Cohen"))

    def test_diagnostic_attaches_to_pilot_only_case(self):
        packet = self._with_notes(
            [pilot_candidate("Ada", 0)],
            [official_mention("Dana", 10)],
            [_note("Ada", 0, 3, reason="EMBEDDED_KNOWN_NAME")],
        )
        pilot = next(
            case for case in packet["cases"] if case["case_type"] == "PILOT_ONLY"
        )
        self.assertEqual(len(pilot["diagnostics"]), 1)
        self.assertEqual(pilot["diagnostics"][0]["reason"], "EMBEDDED_KNOWN_NAME")
        self.assertEqual(pilot["diagnostics"][0]["section"], "suppressed")
        self.assertFalse(pilot["diagnostics"][0]["published_official_span"])
        self.assertNotIn("DIAGNOSTIC_SUPPRESSION", types(packet))

    def test_standalone_diagnostic_when_no_occurrence_matches(self):
        packet = self._with_notes(
            [pilot_candidate("Ada", 0)],
            [official_mention("Dana", 10)],
            [_note("לבון", 40, 44, reason="PLACE_OR_INSTITUTION_CONTEXT")],
        )
        standalone = [
            case
            for case in packet["cases"]
            if case["case_type"] == "DIAGNOSTIC_SUPPRESSION"
        ]
        self.assertEqual(len(standalone), 1)
        self.assertEqual(standalone[0]["pilot_spans"], [])
        self.assertEqual(standalone[0]["official_spans"], [])
        self.assertEqual(standalone[0]["diagnostics"][0]["surface"], "לבון")

    def test_duplicate_raw_diagnostics_do_not_duplicate_case_evidence(self):
        note = _note("Ada", 0, 3, reason="EMBEDDED_KNOWN_NAME")
        packet = self._with_notes(
            [pilot_candidate("Ada", 0)],
            [official_mention("Dana", 10)],
            [note, dict(note)],
        )
        pilot = next(
            case for case in packet["cases"] if case["case_type"] == "PILOT_ONLY"
        )
        self.assertEqual(len(pilot["diagnostics"]), 1)
        self.assertEqual(packet["summary"]["raw_diagnostic_record_count"], 2)
        self.assertEqual(packet["summary"]["unique_diagnostic_occurrence_count"], 1)

    def test_split_conflict_case_and_attached_split(self):
        attached = self._with_notes(
            [pilot_candidate("AdaX", 0)],
            [official_mention("Dana", 10)],
            [_note("AdaX", 0, 4, reason="SPLIT_CONFLICT", section="conflicts")],
        )
        pilot = next(
            case for case in attached["cases"] if case["case_type"] == "PILOT_ONLY"
        )
        self.assertEqual(pilot["diagnostics"][0]["reason"], "SPLIT_CONFLICT")
        self.assertNotIn("SPLIT_CONFLICT", types(attached))
        self.assertEqual(attached["summary"]["cases_involving_split_conflicts"], 1)
        standalone = self._with_notes(
            [pilot_candidate("Ada", 0)],
            [official_mention("Dana", 10)],
            [
                _note(
                    "דר משה",
                    40,
                    46,
                    reason="SPLIT_CONFLICT",
                    section="conflicts",
                    segment=None,
                )
            ],
        )
        split = next(
            case
            for case in standalone["cases"]
            if case["case_type"] == "SPLIT_CONFLICT"
        )
        self.assertEqual(split["diagnostics"][0]["section"], "conflicts")
        self.assertIsNone(split["decision"])

    def test_known_name_only_metadata_is_preserved(self):
        packet = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)])]),
                [
                    official_mention(
                        "Dana",
                        0,
                        detector_ids=("known-name",),
                        quality="EXACT_BOUNDARY",
                    )
                ],
            )
        )
        self.assertEqual(types(packet), ["SAME_SPAN_KNOWN_NAME_ONLY"])
        official = packet["cases"][0]["official_spans"][0]
        self.assertEqual(official["detector_ids"], ["known-name"])
        self.assertEqual(official["known_name_match_quality"], "EXACT_BOUNDARY")
        self.assertEqual(
            packet["summary"]["cases_with_known_name_only_official_detection"], 1
        )

    def test_noncomparable_source_is_separated(self):
        packet = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
                [official_mention("Dana", 0, source=TRANSLATION)],
            )
        )
        self.assertNotIn("PILOT_ONLY", types(packet))
        self.assertNotIn("OFFICIAL_ONLY", types(packet))
        sources = [
            case
            for case in packet["cases"]
            if case["case_type"] == "NONCOMPARABLE_SOURCE"
        ]
        self.assertEqual(len(sources), 2)
        translation = next(
            case for case in sources if case["evidence_source"] == TRANSLATION
        )
        self.assertEqual(translation["official_spans"][0]["exact_mention"], "Dana")
        self.assertEqual(translation["pilot_spans"], [])
        self.assertEqual(packet["summary"]["noncomparable_case_count"], 2)

    def test_noncomparable_pilot_row_is_its_own_case(self):
        mentions: list[dict] = []
        official = proposal(mentions)
        diagnostics = diagnostics_artifact(
            run_id="run-compare",
            mention_proposals_logical_sha256=official["logical_content_sha256"],
            mention_proposals_file_sha256=FILE_SHA,
            rejected=[],
            suppressed=[
                {
                    key: value
                    for key, value in _note(
                        "Dana", 0, 4, reason="EMBEDDED_KNOWN_NAME"
                    ).items()
                    if key != "_section"
                }
            ],
            conflicts=[],
            created_at=CREATED,
        )
        document = build_mention_run_comparison(
            pilot_bundle(
                [pilot_item(10, [pilot_candidate("Dana", 0, include_offsets=False)])]
            ),
            official,
            pilot_file_sha256=FILE_SHA,
            mention_proposals_file_sha256=FILE_SHA,
            diagnostics=diagnostics,
            diagnostics_file_sha256=FILE_SHA,
            created_at=CREATED,
        )
        packet = package(document)
        pilot = next(
            case
            for case in packet["cases"]
            if case["case_type"] == "NONCOMPARABLE_PILOT_ROW"
        )
        self.assertIsNone(pilot["pilot_spans"][0]["start_offset"])
        self.assertEqual(pilot["pilot_spans"][0]["exact_mention"], "Dana")
        self.assertNotIn("PILOT_ONLY", types(packet))

    def test_case_ids_and_order_are_deterministic(self):
        forward = package(
            compare(
                pilot_bundle(
                    [
                        pilot_item(20, [pilot_candidate("Noa", 0)]),
                        pilot_item(10, [pilot_candidate("Ada", 5)]),
                    ]
                ),
                [],
            )
        )
        reverse = package(
            compare(
                pilot_bundle(
                    [
                        pilot_item(10, [pilot_candidate("Ada", 5)]),
                        pilot_item(20, [pilot_candidate("Noa", 0)]),
                    ]
                ),
                [],
            )
        )
        self.assertEqual(
            [case["case_id"] for case in forward["cases"]],
            [case["case_id"] for case in reverse["cases"]],
        )
        self.assertEqual(
            [case["archive_item_id"] for case in forward["cases"]], [10, 20]
        )
        self.assertEqual(forward["cases"][0]["case_id"], "C0001")
        self.assertEqual(forward["cases"][1]["case_id"], "C0002")
        again = package(
            compare(
                pilot_bundle(
                    [
                        pilot_item(20, [pilot_candidate("Noa", 0)]),
                        pilot_item(10, [pilot_candidate("Ada", 5)]),
                    ]
                ),
                [],
            )
        )
        self.assertEqual(
            forward["logical_content_sha256"], again["logical_content_sha256"]
        )

    def test_malformed_geometry_fails_closed(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
            [official_mention("Dana", 10)],
        )
        broken = json.loads(json.dumps(document))
        broken["review_rows"][0]["pilot_start_offset"] = 8
        broken["review_rows"][0]["pilot_end_offset"] = 2
        stamp_generated_artifact(broken, created_at=CREATED)
        with self.assertRaises(Exception) as caught:
            package(broken)
        self.assertIn("malformed review geometry", str(caught.exception))

    def test_wrong_comparison_schema_is_rejected(self):
        with self.assertRaises(UnsupportedSchemaVersionError):
            package({"schema_version": "mention-run-comparison-v2"})

    def test_logical_hash_mismatch_is_rejected(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
            [official_mention("Dana", 10)],
        )
        broken = json.loads(json.dumps(document))
        broken["logical_content_sha256"] = "0" * 64
        with self.assertRaises(LogicalHashMismatchError):
            package(broken)

    def test_duplicate_case_id_is_rejected(self):
        with self.assertRaises(Exception) as caught:
            require_unique_case_ids([{"case_id": "C0001"}, {"case_id": "C0001"}])
        self.assertIn("duplicate case id", str(caught.exception))

    def test_markdown_preserves_newlines_and_pipes(self):
        surface = "Ada|Noa\r\nLevi"
        packet = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate(surface, 0)])]),
                [official_mention("Dana", 30)],
            )
        )
        report = render_adjudication_report(packet)
        self.assertIn(f"```\n{surface}\n```", report)
        self.assertIn("`PILOT_ONLY`", report)
        self.assertIn("decision: null", report)
        self.assertIn("No score is computed for either output.", report)
        self.assertNotIn("precision", report.lower())
        self.assertNotIn("recall", report.lower())

    def test_empty_disagreement_set(self):
        packet = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate("Dana Cohen", 0)])]),
                [official_mention("Dana Cohen", 0)],
            )
        )
        self.assertEqual(packet["cases"], [])
        self.assertEqual(packet["summary"]["logical_case_count"], 0)
        self.assertEqual(packet["summary"]["item_count"], 0)
        self.assertEqual(packet["summary"]["case_type_counts"], [])
        report = render_adjudication_report(packet)
        self.assertIn("No logical cases.", report)
        decisions = build_mention_adjudication_decisions(
            packet, adjudication_file_sha256=FILE_SHA
        )
        self.assertEqual(decisions["decisions"], [])
        self.assertEqual(
            decisions["adjudication_logical_sha256"], packet["logical_content_sha256"]
        )
        self.assertFalse(decisions["tuning_performed"])

    def test_decision_template_binds_packet_and_stays_pending(self):
        packet = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
                [official_mention("Dana", 10)],
            )
        )
        decisions = build_mention_adjudication_decisions(
            packet, adjudication_file_sha256="b" * 64
        )
        self.assertEqual(
            decisions["schema_version"], "mention-adjudication-decisions-v1"
        )
        self.assertEqual(
            [row["case_id"] for row in decisions["decisions"]],
            [case["case_id"] for case in packet["cases"]],
        )
        self.assertTrue(all(row["decision"] is None for row in decisions["decisions"]))
        filled = json.loads(json.dumps(decisions))
        filled["decisions"][0]["decision"] = "PILOT_PREFERRED"
        filled["decisions"][0]["reviewer_note"] = "span includes the title"
        validate_artifact(filled)
        self.assertIsNone(packet["cases"][0]["decision"])

    def test_command_writes_packet_report_and_decision_template(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
            [official_mention("Dana", 10)],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            comparison = root / "comparison.json"
            output = root / "adjudication.json"
            report = root / "adjudication.md"
            decisions = root / "decisions.json"
            write_json_atomic(comparison, document)
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "package-mention-adjudication",
                        "--comparison",
                        str(comparison),
                        "--output",
                        str(output),
                        "--report",
                        str(report),
                        "--decisions",
                        str(decisions),
                    ]
                )
            self.assertEqual(code, 0)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], "mention-adjudication-v1")
            self.assertEqual(saved["comparison_file_sha256"], sha256_file(comparison))
            self.assertIn(
                saved["cases"][0]["pilot_spans"][0]["exact_mention"],
                report.read_text(encoding="utf-8"),
            )
            template = json.loads(decisions.read_text(encoding="utf-8"))
            self.assertEqual(template["adjudication_file_sha256"], sha256_file(output))
            self.assertEqual(
                template["adjudication_logical_sha256"],
                saved["logical_content_sha256"],
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                again = main(
                    [
                        "package-mention-adjudication",
                        "--comparison",
                        str(comparison),
                        "--output",
                        str(output),
                        "--report",
                        str(report),
                        "--decisions",
                        str(decisions),
                    ]
                )
            self.assertEqual(again, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"], "ARTIFACT_EXISTS"
            )

    def test_decision_validation_accepts_template_filled_rows_and_reordering(self):
        packet = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
                [official_mention("Dana", 10)],
            )
        )
        before = json.dumps(packet, sort_keys=True)
        file_sha = "b" * 64
        decisions = build_mention_adjudication_decisions(
            packet, adjudication_file_sha256=file_sha
        )
        checked = require_mention_adjudication_decisions(
            packet, decisions, adjudication_file_sha256=file_sha
        )
        self.assertEqual(
            [row["case_id"] for row in checked["decisions"]],
            [row["case_id"] for row in decisions["decisions"]],
        )
        self.assertEqual(json.dumps(packet, sort_keys=True), before)
        filled = json.loads(json.dumps(decisions))
        filled["decisions"][0]["decision"] = "BOTH_ACCEPTABLE"
        filled["decisions"][0]["reviewer_note"] = "both spans are usable"
        require_mention_adjudication_decisions(
            packet, filled, adjudication_file_sha256=file_sha
        )
        reordered = json.loads(json.dumps(filled))
        reordered["decisions"].reverse()
        require_mention_adjudication_decisions(
            packet, reordered, adjudication_file_sha256=file_sha
        )
        self.assertEqual(json.dumps(packet, sort_keys=True), before)
        self.assertIsNone(packet["cases"][0]["decision"])

    def test_decision_case_set_and_packet_hashes_fail_closed(self):
        packet = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
                [official_mention("Dana", 10)],
            )
        )
        file_sha = "b" * 64
        decisions = build_mention_adjudication_decisions(
            packet, adjudication_file_sha256=file_sha
        )
        duplicate = json.loads(json.dumps(decisions))
        duplicate["decisions"].append(dict(duplicate["decisions"][0]))
        with self.assertRaises(ArtifactError) as duplicated:
            require_mention_adjudication_decisions(
                packet, duplicate, adjudication_file_sha256=file_sha
            )
        self.assertIn("duplicate case id", str(duplicated.exception))
        unknown = json.loads(json.dumps(decisions))
        unknown["decisions"].append(
            {"case_id": "C9999", "decision": None, "reviewer_note": None}
        )
        with self.assertRaises(ArtifactError) as unknown_case:
            require_mention_adjudication_decisions(
                packet, unknown, adjudication_file_sha256=file_sha
            )
        self.assertIn("unknown case id", str(unknown_case.exception))
        missing = json.loads(json.dumps(decisions))
        removed = missing["decisions"].pop()
        with self.assertRaises(ArtifactError) as missing_case:
            require_mention_adjudication_decisions(
                packet, missing, adjudication_file_sha256=file_sha
            )
        self.assertIn(
            f"missing case id {removed['case_id']}", str(missing_case.exception)
        )
        replaced = json.loads(json.dumps(decisions))
        replaced["decisions"][0]["case_id"] = "C9999"
        with self.assertRaises(ArtifactError):
            require_mention_adjudication_decisions(
                packet, replaced, adjudication_file_sha256=file_sha
            )
        drifted = json.loads(json.dumps(decisions))
        drifted["adjudication_logical_sha256"] = "0" * 64
        with self.assertRaises(InputHashChangedError):
            require_mention_adjudication_decisions(
                packet, drifted, adjudication_file_sha256=file_sha
            )
        with self.assertRaises(InputHashChangedError):
            require_mention_adjudication_decisions(
                packet, decisions, adjudication_file_sha256="c" * 64
            )
        other = package(
            compare(
                pilot_bundle([pilot_item(10, [pilot_candidate("Noa", 0)])]),
                [official_mention("Levi", 10)],
            )
        )
        with self.assertRaises(InputHashChangedError):
            require_mention_adjudication_decisions(
                other, decisions, adjudication_file_sha256=file_sha
            )

    def test_one_exact_diagnostic_attaches_and_two_exact_matches_fail(self):
        packet = self._with_notes(
            [pilot_candidate("Ada", 0)],
            [official_mention("Dana", 10)],
            [_note("Ada", 0, 3, reason="EMBEDDED_KNOWN_NAME")],
        )
        pilot = next(
            case for case in packet["cases"] if case["case_type"] == "PILOT_ONLY"
        )
        self.assertEqual(pilot["diagnostics"][0]["surface"], "Ada")
        note = _diagnostic(0, 3)
        first = _pilot_case(0, 3, "Ada")
        second = _pilot_case(0, 3, "Ada")
        with self.assertRaises(ArtifactError) as caught:
            _attach_diagnostics([first, second], [note])
        self.assertIn("diagnostic matches multiple cases", str(caught.exception))
        reversed_cases = [second, first]
        with self.assertRaises(ArtifactError):
            _attach_diagnostics(reversed_cases, [dict(note)])

    def test_one_overlap_attaches_and_two_overlaps_stay_standalone(self):
        contained = self._with_notes(
            [pilot_candidate("ABCDE", 0)],
            [official_mention("ZZ", 20)],
            [_note("BC", 1, 3, reason="EMBEDDED_KNOWN_NAME")],
        )
        pilot = next(
            case for case in contained["cases"] if case["case_type"] == "PILOT_ONLY"
        )
        self.assertEqual(pilot["diagnostics"][0]["start_offset"], 1)
        self.assertNotIn("DIAGNOSTIC_SUPPRESSION", types(contained))
        ambiguous = self._with_notes(
            [pilot_candidate("AAAA", 0)],
            [official_mention("BBBB", 6)],
            [_note("MIDDLE", 3, 8, reason="PLACE_OR_INSTITUTION_CONTEXT")],
        )
        self._assert_unassigned(ambiguous, "PLACE_OR_INSTITUTION_CONTEXT", "MIDDLE")
        reversed_mentions = self._with_notes(
            [pilot_candidate("BBBB", 6)],
            [official_mention("AAAA", 0)],
            [_note("MIDDLE", 3, 8, reason="PLACE_OR_INSTITUTION_CONTEXT")],
        )
        self._assert_unassigned(
            reversed_mentions, "PLACE_OR_INSTITUTION_CONTEXT", "MIDDLE"
        )
        self.assertEqual(
            _diagnostic_locations(ambiguous),
            _diagnostic_locations(reversed_mentions),
        )
        overlap_note = _diagnostic(3, 8)
        overlap_note["surface"] = "MIDDLE"
        overlap_note["reason"] = "PLACE_OR_INSTITUTION_CONTEXT"
        forward_cases = [_pilot_case(0, 4, "AAAA"), _pilot_case(6, 10, "BBBB")]
        reverse_cases = [_pilot_case(6, 10, "BBBB"), _pilot_case(0, 4, "AAAA")]
        forward = _attach_diagnostics(forward_cases, [overlap_note])
        reverse = _attach_diagnostics(reverse_cases, [dict(overlap_note)])
        self.assertEqual(forward[0][1], "ambiguous")
        self.assertEqual(reverse[0][1], "ambiguous")
        self.assertEqual(forward_cases[0]["diagnostics"], [])
        self.assertEqual(forward_cases[1]["diagnostics"], [])
        self.assertEqual(reverse_cases[0]["diagnostics"], [])
        self.assertEqual(reverse_cases[1]["diagnostics"], [])
        split = self._with_notes(
            [pilot_candidate("AAAA", 0)],
            [official_mention("BBBB", 6)],
            [
                _note(
                    "MIDDLE",
                    3,
                    8,
                    reason="SPLIT_CONFLICT",
                    section="conflicts",
                    segment=None,
                )
            ],
        )
        standalone = self._assert_unassigned(split, "SPLIT_CONFLICT", "MIDDLE")
        self.assertEqual(standalone["case_type"], "SPLIT_CONFLICT")
        self.assertEqual(standalone["diagnostics"][0]["reason"], "SPLIT_CONFLICT")
        self.assertIn(
            "overlaps multiple mention cases", standalone["comparison_reason"]
        )

    def _assert_unassigned(self, packet: dict, reason: str, surface: str) -> dict:
        found = [
            case
            for case in packet["cases"]
            if any(
                note["reason"] == reason and note["surface"] == surface
                for note in case["diagnostics"]
            )
        ]
        self.assertEqual(len(found), 1)
        case = found[0]
        self.assertEqual(case["pilot_spans"], [])
        self.assertEqual(case["official_spans"], [])
        self.assertIn("overlaps multiple mention cases", case["comparison_reason"])
        occurrences = [
            note
            for item in packet["cases"]
            for note in item["diagnostics"]
            if note["surface"] == surface and note["reason"] == reason
        ]
        self.assertEqual(len(occurrences), 1)
        return case

    def _with_notes(self, candidates, mentions, notes) -> dict:
        grouped = {"rejected": [], "suppressed": [], "conflicts": []}
        for note in notes:
            row = dict(note)
            section = row.pop("_section")
            grouped[section].append(row)
        official = proposal(mentions)
        diagnostics = diagnostics_artifact(
            run_id="run-compare",
            mention_proposals_logical_sha256=official["logical_content_sha256"],
            mention_proposals_file_sha256=FILE_SHA,
            rejected=grouped["rejected"],
            suppressed=grouped["suppressed"],
            conflicts=grouped["conflicts"],
            created_at=CREATED,
        )
        document = build_mention_run_comparison(
            pilot_bundle([pilot_item(10, candidates)]),
            official,
            pilot_file_sha256=FILE_SHA,
            mention_proposals_file_sha256=FILE_SHA,
            diagnostics=diagnostics,
            diagnostics_file_sha256=FILE_SHA,
            created_at=CREATED,
        )
        return package(document)
