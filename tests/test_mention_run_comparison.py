import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vs_archive_people.artifacts.io import (
    InputHashChangedError,
    LogicalHashMismatchError,
    UnsupportedSchemaVersionError,
    sha256_file,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import stamp_generated_artifact
from vs_archive_people.artifacts.schema_validate import (
    published_schema_versions,
    step_chain,
    validate_artifact,
)
from vs_archive_people.cli import main
from vs_archive_people.evaluation.compare import build_mention_run_comparison
from vs_archive_people.evaluation.report import render_comparison_report
from vs_archive_people.find_mentions.run import diagnostics_artifact

CREATED = "2026-01-01T00:00:00Z"
SOURCE = "ocr_document.displayed_transcription.text"
RUN = "run-compare"
FILE_SHA = "a" * 64
SNAPSHOT_SHA = "b" * 64


def _detector(detector_id: str) -> dict:
    if detector_id == "semantic-surface":
        return {
            "id": detector_id,
            "role": "semantic",
            "implementation": "gemini-3.8-flash",
            "version": "1",
            "reproducibility": {"prompt_sha256": "c" * 64},
        }
    if detector_id == "known-name":
        return {
            "id": detector_id,
            "role": "deterministic",
            "implementation": "exact-literal",
            "version": "1",
        }
    return {
        "id": detector_id,
        "role": "human",
        "implementation": "manual",
        "version": "1",
    }


def official_mention(
    surface: str,
    start: int,
    *,
    item_id: int = 10,
    source: str = SOURCE,
    detector_ids: tuple[str, ...] = ("semantic-surface",),
    quality: str | None = None,
    kind: str | None = "FULL_NAME",
    name: str | None = None,
    title: str | None = None,
) -> dict:
    name_surface = surface if name is None else name
    name_start = start + surface.index(name_surface)
    row = {
        "archive_item_id": item_id,
        "evidence_source": source,
        "exact_mention": surface,
        "start_offset": start,
        "end_offset": start + len(surface),
        "evidence_snippet": surface,
        "name_surface": name_surface,
        "name_start_offset": name_start,
        "name_end_offset": name_start + len(name_surface),
        "title_surface": title,
        "title_start_offset": None,
        "title_end_offset": None,
        "surface_kind": kind,
        "detector_ids": list(detector_ids),
        "known_name_match_quality": quality,
    }
    if title is not None:
        title_start = start + surface.index(title)
        row["title_start_offset"] = title_start
        row["title_end_offset"] = title_start + len(title)
        row["surface_kind"] = "TITLE_AND_NAME" if kind is None else kind
    return row


def proposal(
    mentions: list[dict],
    *,
    snapshot_sha: str = SNAPSHOT_SHA,
    discovery_logical: str = "d" * 64,
    discovery_file: str = "e" * 64,
) -> dict:
    detector_ids: list[str] = []
    for mention in mentions:
        for detector_id in mention["detector_ids"]:
            if detector_id not in detector_ids:
                detector_ids.append(detector_id)
    document = {
        "schema_version": "mention-proposals-v1",
        "run_id": RUN,
        "document_snapshot_file_sha256": snapshot_sha,
        "discovery_logical_sha256": discovery_logical,
        "discovery_file_sha256": discovery_file,
        "identity_snapshot_file_sha256": "f" * 64,
        "production_write_performed": False,
        "detectors": [_detector(detector_id) for detector_id in detector_ids],
        "mentions": mentions,
    }
    return stamp_generated_artifact(document, created_at=CREATED)


def pilot_candidate(
    surface: str,
    start: int,
    *,
    name: str | None = None,
    title: str | None = None,
    kind: str | None = "FULL_NAME",
    include_offsets: bool = True,
) -> dict:
    row = {
        "exact_mention": surface,
        "name_surface": surface if name is None else name,
        "title_surface": title,
        "gemini_kind": kind,
    }
    if include_offsets:
        row["start_offset"] = start
        row["end_offset"] = start + len(surface)
    return row


def pilot_bundle(items: list[dict]) -> dict:
    return {
        "kind": "GEMINI_REAL_MENTION_PILOT_REFINED_NOT_ARTIFACT",
        "items": items,
    }


def pilot_item(
    item_id: int,
    candidates: list[dict],
    *,
    source: str = SOURCE,
    body: str | None = None,
) -> dict:
    row = {
        "archive_item_id": item_id,
        "source": source,
        "candidates": candidates,
    }
    if body is not None:
        row["body_text"] = body
    return row


def compare(pilot: object, mentions: list[dict], **kwargs) -> dict:
    official = proposal(mentions, snapshot_sha=kwargs.pop("snapshot_sha", SNAPSHOT_SHA))
    return build_mention_run_comparison(
        pilot,
        official,
        pilot_file_sha256=kwargs.pop("pilot_file_sha256", FILE_SHA),
        mention_proposals_file_sha256=kwargs.pop(
            "mention_proposals_file_sha256", FILE_SHA
        ),
        created_at=CREATED,
        **kwargs,
    )


def _markdown_columns(line: str) -> int:
    columns = 0
    escaped = False
    for character in line:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "|":
            columns += 1
    return columns


def categories(document: dict) -> list[str]:
    return [row["category"] for row in document["disagreements"]]


class CompareMentionRunsTests(unittest.TestCase):
    def test_exact_same_span_is_agreement_and_not_ground_truth(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana Cohen", 0)])]),
            [official_mention("Dana Cohen", 0)],
        )
        self.assertFalse(document["pilot_is_ground_truth"])
        self.assertFalse(document["precision_recall_computed"])
        self.assertFalse(document["operational_pipeline"])
        self.assertEqual(document["metrics"]["exact_span_agreement_count"], 1)
        self.assertEqual(document["metrics"]["pilot_only_count"], 0)
        self.assertEqual(document["metrics"]["official_only_count"], 0)
        self.assertEqual(document["disagreements"], [])
        self.assertEqual(
            document["detector_contribution"]["semantic_only_published_count"], 1
        )
        self.assertNotIn(
            "mention-run-comparison-v1", published_schema_versions().values()
        )
        self.assertNotIn(
            "mention-run-comparison-v3", published_schema_versions().values()
        )
        self.assertEqual(document["schema_version"], "mention-run-comparison-v3")
        self.assertNotIn("compare-mention-runs", step_chain())
        recovery = document["historical_corpus_recovery"]
        self.assertEqual(recovery["recovered_item_count"], 10)
        self.assertEqual(recovery["pilot_result"], "refined")
        self.assertFalse(recovery["additional_recoverable_corpus_artifact_found"])
        self.assertFalse(recovery["compared_corpus_matches_recovered_corpus"])
        self.assertIn("No additional recoverable", recovery["statement"])
        self.assertIn(
            "does not establish that no other corpus ever existed",
            recovery["statement"],
        )

    def test_same_span_name_difference_stays_same_span(self):
        document = compare(
            pilot_bundle(
                [pilot_item(10, [pilot_candidate("Dana Cohen", 0, name="Dana")])]
            ),
            [official_mention("Dana Cohen", 0)],
        )
        self.assertEqual(document["metrics"]["exact_span_agreement_count"], 1)
        self.assertEqual(document["metrics"]["same_span_name_surface_differs_count"], 1)
        self.assertEqual(categories(document), ["BOTH_SAME_SPAN"])
        self.assertEqual(
            document["disagreements"][0]["name_surface_relation"], "DIFFERENT"
        )
        self.assertIn(
            "name surface differs", document["disagreements"][0]["comparison_reason"]
        )

    def test_same_span_title_and_kind_differences(self):
        document = compare(
            pilot_bundle(
                [
                    pilot_item(
                        10,
                        [
                            pilot_candidate(
                                "Dr Dana",
                                0,
                                name="Dana",
                                title="Doctor",
                                kind="PARTIAL_NAME",
                            )
                        ],
                    )
                ]
            ),
            [
                official_mention(
                    "Dr Dana",
                    0,
                    name="Dana",
                    title="Dr",
                    kind="TITLE_AND_NAME",
                )
            ],
        )
        row = document["disagreements"][0]
        self.assertEqual(row["category"], "BOTH_SAME_SPAN")
        self.assertEqual(row["title_surface_relation"], "DIFFERENT")
        self.assertEqual(row["surface_kind_relation"], "DIFFERENT")
        self.assertEqual(row["name_surface_relation"], "SAME")
        self.assertEqual(
            document["metrics"]["same_span_title_surface_differs_count"], 1
        )
        self.assertEqual(document["metrics"]["same_span_surface_kind_differs_count"], 1)

    def test_overlap_different_span(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana Cohen", 0)])]),
            [official_mention("Dana", 0)],
        )
        self.assertEqual(document["metrics"]["overlap_different_span_count"], 2)
        self.assertEqual(document["metrics"]["exact_span_agreement_count"], 0)
        self.assertEqual(
            sorted(categories(document)),
            ["BOTH_OVERLAP_DIFFERENT_SPAN", "BOTH_OVERLAP_DIFFERENT_SPAN"],
        )

    def test_official_only_and_pilot_only(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
            [official_mention("Dana", 10)],
        )
        self.assertEqual(sorted(categories(document)), ["OFFICIAL_ONLY", "PILOT_ONLY"])
        self.assertTrue(
            all(
                row["source_equivalence"] == "SOURCE_EQUIVALENCE_UNKNOWN"
                for row in document["disagreements"]
            )
        )

    def test_repeated_surface_is_compared_by_offsets(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)])]),
            [official_mention("Dana", 10)],
        )
        self.assertEqual(document["metrics"]["exact_span_agreement_count"], 0)
        self.assertEqual(sorted(categories(document)), ["OFFICIAL_ONLY", "PILOT_ONLY"])
        self.assertEqual(document["disagreements"][0]["pilot_exact_mention"], "Dana")
        self.assertEqual(document["disagreements"][1]["official_exact_mention"], "Dana")

    def test_ordering_is_deterministic(self):
        forward = compare(
            pilot_bundle(
                [
                    pilot_item(20, [pilot_candidate("Noa", 0)]),
                    pilot_item(
                        10, [pilot_candidate("Dana", 5), pilot_candidate("Ada", 0)]
                    ),
                ]
            ),
            [
                official_mention("Noa", 4, item_id=20),
                official_mention("Dana", 5, item_id=10),
            ],
        )
        reverse = compare(
            pilot_bundle(
                [
                    pilot_item(
                        10,
                        [pilot_candidate("Ada", 0), pilot_candidate("Dana", 5)],
                    ),
                    pilot_item(20, [pilot_candidate("Noa", 0)]),
                ]
            ),
            [
                official_mention("Dana", 5, item_id=10),
                official_mention("Noa", 4, item_id=20),
            ],
        )
        self.assertEqual([row["archive_item_id"] for row in forward["items"]], [10, 20])
        self.assertEqual(
            forward["corpus"]["pilot_archive_item_ids_in_input_order"], [20, 10]
        )
        self.assertEqual(
            reverse["corpus"]["pilot_archive_item_ids_in_input_order"], [10, 20]
        )
        self.assertEqual(forward["disagreements"], reverse["disagreements"])
        self.assertEqual(forward["metrics"], reverse["metrics"])
        self.assertLess(
            forward["disagreements"][0]["archive_item_id"],
            forward["disagreements"][-1]["archive_item_id"],
        )

    def test_known_name_only_and_both_detectors(self):
        document = compare(
            pilot_bundle([pilot_item(10, [])]),
            [
                official_mention(
                    "Dana",
                    0,
                    detector_ids=("known-name",),
                    quality="PREFIXED",
                ),
                official_mention(
                    "Noa Cohen",
                    10,
                    detector_ids=("semantic-surface", "known-name"),
                    quality="EXACT_BOUNDARY",
                ),
            ],
        )
        contribution = document["detector_contribution"]
        self.assertEqual(contribution["known_name_only_published_count"], 1)
        self.assertEqual(contribution["both_detectors_published_count"], 1)
        self.assertEqual(contribution["semantic_only_published_count"], 0)
        self.assertEqual(contribution["known_name_prefixed_count"], 1)
        self.assertEqual(contribution["known_name_exact_boundary_count"], 1)
        known_only = [
            row
            for row in document["review_rows"]
            if row["official_detector_ids"] == ["known-name"]
        ]
        self.assertEqual(len(known_only), 1)
        self.assertEqual(known_only[0]["known_name_match_quality"], "PREFIXED")
        self.assertIn("known-name detector only", known_only[0]["comparison_reason"])

    def test_diagnostics_present_and_absent(self):
        mentions = [official_mention("Dana", 0)]
        official = proposal(mentions)
        pilot = pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)])])
        absent = build_mention_run_comparison(
            pilot,
            official,
            pilot_file_sha256=FILE_SHA,
            mention_proposals_file_sha256=FILE_SHA,
            created_at=CREATED,
        )
        self.assertFalse(absent["diagnostics_available"])
        self.assertIsNone(absent["metrics"]["split_conflict_count"])
        self.assertEqual(absent["diagnostic_counts"], [])
        self.assertEqual(absent["metrics"]["exact_span_agreement_count"], 1)
        diagnostics = diagnostics_artifact(
            run_id=RUN,
            mention_proposals_logical_sha256=official["logical_content_sha256"],
            mention_proposals_file_sha256=FILE_SHA,
            rejected=[
                {
                    "archive_item_id": 10,
                    "evidence_source": SOURCE,
                    "surface": "missing",
                    "reason": "SURFACE_NOT_IN_SEGMENT",
                    "segment_id": "S0001",
                }
            ],
            suppressed=[
                {
                    "archive_item_id": 10,
                    "evidence_source": SOURCE,
                    "surface": "הנשיא",
                    "reason": "TITLE_OR_ROLE_WITHOUT_NAME",
                    "segment_id": "S0002",
                    "start_offset": 30,
                    "end_offset": 35,
                },
                {
                    "archive_item_id": 10,
                    "evidence_source": SOURCE,
                    "surface": "Dana",
                    "reason": "EMBEDDED_KNOWN_NAME",
                    "segment_id": "S0003",
                    "start_offset": 40,
                    "end_offset": 44,
                },
            ],
            conflicts=[
                {
                    "archive_item_id": 10,
                    "evidence_source": SOURCE,
                    "surface": 'ד"ר משה',
                    "reason": "SPLIT_CONFLICT",
                    "segment_id": None,
                    "start_offset": 50,
                    "end_offset": 57,
                },
                {
                    "archive_item_id": 10,
                    "evidence_source": SOURCE,
                    "surface": "משה",
                    "reason": "ABSORB_AMBIGUOUS",
                    "segment_id": None,
                    "start_offset": 60,
                    "end_offset": 63,
                },
            ],
            created_at=CREATED,
        )
        present = build_mention_run_comparison(
            pilot,
            official,
            pilot_file_sha256=FILE_SHA,
            mention_proposals_file_sha256=FILE_SHA,
            diagnostics=diagnostics,
            diagnostics_file_sha256=FILE_SHA,
            created_at=CREATED,
        )
        reasons = {
            (row["section"], row["reason"]): row["count"]
            for row in present["diagnostic_counts"]
        }
        self.assertEqual(reasons[("conflicts", "SPLIT_CONFLICT")], 1)
        self.assertEqual(reasons[("conflicts", "ABSORB_AMBIGUOUS")], 1)
        self.assertEqual(reasons[("suppressed", "TITLE_OR_ROLE_WITHOUT_NAME")], 1)
        self.assertEqual(reasons[("suppressed", "EMBEDDED_KNOWN_NAME")], 1)
        self.assertEqual(reasons[("rejected", "SURFACE_NOT_IN_SEGMENT")], 1)
        self.assertEqual(present["metrics"]["split_conflict_count"], 1)
        self.assertEqual(present["metrics"]["absorb_ambiguous_count"], 1)
        self.assertTrue(
            any(
                row["category"] == "DIAGNOSTIC_ONLY"
                and row["diagnostic_reason"] == "SPLIT_CONFLICT"
                for row in present["review_rows"]
            )
        )
        self.assertEqual(present["diagnostic_rows"][0]["archive_item_id"], 10)

    def test_noncomparable_pilot_row(self):
        mentions: list[dict] = []
        official = proposal(mentions)
        diagnostics = diagnostics_artifact(
            run_id=RUN,
            mention_proposals_logical_sha256=official["logical_content_sha256"],
            mention_proposals_file_sha256=FILE_SHA,
            rejected=[],
            suppressed=[
                {
                    "archive_item_id": 10,
                    "evidence_source": SOURCE,
                    "surface": "Dana",
                    "reason": "EMBEDDED_KNOWN_NAME",
                    "segment_id": "S0001",
                    "start_offset": 0,
                    "end_offset": 4,
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
        self.assertEqual(document["metrics"]["non_comparable_pilot_mention_count"], 1)
        self.assertEqual(document["metrics"]["comparable_pilot_mention_count"], 0)
        missing = [
            row
            for row in document["disagreements"]
            if row["category"] == "NON_COMPARABLE"
        ]
        self.assertEqual(len(missing), 1)
        self.assertIsNone(missing[0]["pilot_start_offset"])
        self.assertTrue(document["items"][0]["source_pair_comparable"])

    def test_source_drift_equivalent_and_unknown(self):
        text = "Dana Cohen"
        snapshot = {
            "schema_version": "document-text-snapshot-v1",
            "exported_at": "2026-01-01T00:00:00+00:00",
            "items": [
                {
                    "archive_item_id": 10,
                    "item_type": "OCR_DOCUMENT",
                    "ocr_document": {"displayed_transcription": {"text": text}},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            snapshot_sha = sha256_file(path)
        drifted = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)], body="other")]),
            [official_mention("Dana", 0)],
            snapshot_sha=snapshot_sha,
            document_snapshot=snapshot,
            document_snapshot_file_sha256=snapshot_sha,
        )
        self.assertEqual(drifted["source_equivalence"], "SOURCE_DRIFT")
        self.assertEqual(drifted["metrics"]["items_with_source_drift"], 1)
        equivalent = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)], body=text)]),
            [official_mention("Dana", 0)],
            snapshot_sha=snapshot_sha,
            document_snapshot=snapshot,
            document_snapshot_file_sha256=snapshot_sha,
        )
        self.assertEqual(
            equivalent["items"][0]["source_equivalence"], "SOURCE_EQUIVALENT"
        )
        unknown = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)], body=text)]),
            [official_mention("Dana", 0)],
        )
        self.assertEqual(unknown["source_equivalence"], "SOURCE_EQUIVALENCE_UNKNOWN")
        mismatched = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)], body="other")]),
            [official_mention("Dana", 0)],
            snapshot_sha=SNAPSHOT_SHA,
            document_snapshot=snapshot,
            document_snapshot_file_sha256=snapshot_sha,
        )
        self.assertEqual(mismatched["source_equivalence"], "SOURCE_EQUIVALENCE_UNKNOWN")
        self.assertFalse(mismatched["official_snapshot_file_matches"])

    def test_empty_corpus(self):
        document = compare(pilot_bundle([]), [])
        self.assertEqual(document["metrics"]["corpus_item_count"], 0)
        self.assertEqual(document["metrics"]["pilot_mention_count"], 0)
        self.assertEqual(document["metrics"]["official_mention_count"], 0)
        self.assertEqual(document["items"], [])
        self.assertEqual(document["disagreements"], [])
        self.assertEqual(document["source_equivalence"], "SOURCE_EQUIVALENCE_UNKNOWN")

    def test_wrong_official_artifact_fails_closed(self):
        with self.assertRaises(UnsupportedSchemaVersionError):
            build_mention_run_comparison(
                pilot_bundle([]),
                {"schema_version": "evidence-package-v1"},
                pilot_file_sha256=FILE_SHA,
                mention_proposals_file_sha256=FILE_SHA,
                created_at=CREATED,
            )
        broken = proposal([])
        broken["run_id"] = "changed"
        with self.assertRaises(LogicalHashMismatchError):
            build_mention_run_comparison(
                pilot_bundle([]),
                broken,
                pilot_file_sha256=FILE_SHA,
                mention_proposals_file_sha256=FILE_SHA,
                created_at=CREATED,
            )

    def test_cli_writes_json_and_markdown_without_diagnostics(self):
        pilot = pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)])])
        official = proposal([official_mention("Ada", 10)])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pilot_path = root / "pilot.json"
            proposal_path = root / "proposals.json"
            output = root / "comparison.json"
            report = root / "comparison.md"
            pilot_path.write_text(json.dumps(pilot), encoding="utf-8")
            write_json_atomic(proposal_path, official)
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "compare-mention-runs",
                        "--pilot",
                        str(pilot_path),
                        "--proposal",
                        str(proposal_path),
                        "--output",
                        str(output),
                        "--report",
                        str(report),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "OK")
            self.assertFalse(payload["diagnostics_available"])
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], "mention-run-comparison-v3")
            self.assertIn("PILOT_ONLY", report.read_text(encoding="utf-8"))
            self.assertIn("OFFICIAL_ONLY", report.read_text(encoding="utf-8"))
            stderr = StringIO()
            with redirect_stderr(stderr):
                blocked = main(
                    [
                        "compare-mention-runs",
                        "--pilot",
                        str(pilot_path),
                        "--proposal",
                        str(pilot_path),
                        "--output",
                        str(root / "blocked.json"),
                        "--report",
                        str(root / "blocked.md"),
                    ]
                )
            self.assertEqual(blocked, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"],
                "UNSUPPORTED_SCHEMA_VERSION",
            )
            self.assertFalse((root / "blocked.json").exists())

    def test_official_source_absent_from_pilot_is_not_official_only(self):
        transcription = "ocr_document.displayed_transcription.text"
        translation = "ocr_document.displayed_hebrew_translation.text"
        text = "Dana Cohen"
        translated = "דנה כהן"
        snapshot = {
            "schema_version": "document-text-snapshot-v1",
            "exported_at": "2026-01-01T00:00:00+00:00",
            "items": [
                {
                    "archive_item_id": 292,
                    "item_type": "OCR_DOCUMENT",
                    "ocr_document": {
                        "displayed_transcription": {"text": text},
                        "displayed_hebrew_translation": {"text": translated},
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            snapshot_sha = sha256_file(path)
        document = compare(
            pilot_bundle(
                [
                    pilot_item(
                        292,
                        [pilot_candidate("Dana", 0)],
                        source=transcription,
                        body=text,
                    )
                ]
            ),
            [
                official_mention("Dana", 0, item_id=292, source=transcription),
                official_mention("דנה", 0, item_id=292, source=translation),
                official_mention("כהן", 3, item_id=292, source=translation),
            ],
            snapshot_sha=snapshot_sha,
            document_snapshot=snapshot,
            document_snapshot_file_sha256=snapshot_sha,
        )
        validate_artifact(document)
        by_source = {row["evidence_source"]: row for row in document["items"]}
        self.assertTrue(by_source[transcription]["source_pair_comparable"])
        self.assertEqual(
            by_source[transcription]["source_equivalence"], "SOURCE_EQUIVALENT"
        )
        self.assertEqual(by_source[transcription]["official_only_count"], 0)
        self.assertFalse(by_source[translation]["source_pair_comparable"])
        self.assertEqual(
            by_source[translation]["source_equivalence"], "SOURCE_EQUIVALENCE_UNKNOWN"
        )
        self.assertEqual(by_source[translation]["official_only_count"], 0)
        self.assertFalse(by_source[translation]["has_disagreement"])
        self.assertEqual(document["metrics"]["official_only_count"], 0)
        self.assertEqual(
            document["metrics"]["official_noncomparable_source_mention_count"], 2
        )
        self.assertEqual(
            document["metrics"]["pilot_noncomparable_source_mention_count"], 0
        )
        self.assertEqual(document["metrics"]["source_pairs_comparable"], 1)
        self.assertEqual(document["metrics"]["source_pairs_noncomparable"], 1)
        self.assertEqual(document["metrics"]["items_with_any_disagreement"], 0)
        self.assertEqual(document["source_equivalence"], "SOURCE_EQUIVALENT")
        self.assertEqual(
            document["source_equivalence_summary"]["comparable_pairs_status"],
            "ALL_EQUIVALENT",
        )
        self.assertEqual(
            document["source_equivalence_summary"]["noncomparable_source_pair_count"], 1
        )
        self.assertEqual(document["metrics"]["items_source_equivalent"], 1)
        self.assertEqual(document["metrics"]["items_source_equivalence_unknown"], 0)
        categories_seen = [row["category"] for row in document["review_rows"]]
        self.assertEqual(categories_seen.count("OFFICIAL_SOURCE_NONCOMPARABLE"), 2)
        self.assertNotIn(
            "OFFICIAL_ONLY", [row["category"] for row in document["disagreements"]]
        )

    def test_pilot_source_missing_from_official_snapshot_is_not_pilot_only(self):
        missing = "manual_text.body"
        snapshot = {
            "schema_version": "document-text-snapshot-v1",
            "exported_at": "2026-01-01T00:00:00+00:00",
            "items": [
                {
                    "archive_item_id": 10,
                    "item_type": "OCR_DOCUMENT",
                    "ocr_document": {"displayed_transcription": {"text": "other"}},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            snapshot_sha = sha256_file(path)
        document = compare(
            pilot_bundle(
                [
                    pilot_item(
                        10, [pilot_candidate("Dana", 0)], source=missing, body="Dana"
                    )
                ]
            ),
            [],
            snapshot_sha=snapshot_sha,
            document_snapshot=snapshot,
            document_snapshot_file_sha256=snapshot_sha,
        )
        self.assertEqual(document["metrics"]["pilot_only_count"], 0)
        self.assertEqual(
            document["metrics"]["pilot_noncomparable_source_mention_count"], 1
        )
        self.assertEqual(
            document["review_rows"][0]["category"], "PILOT_SOURCE_NONCOMPARABLE"
        )
        self.assertEqual(document["disagreements"], [])

    def test_duplicate_diagnostics_keep_raw_rows_and_one_review_occurrence(self):
        mentions = [official_mention("Dana", 0)]
        official = proposal(mentions)
        note = {
            "archive_item_id": 10,
            "evidence_source": SOURCE,
            "surface": "פארוק",
            "reason": "PLACE_OR_INSTITUTION_CONTEXT",
            "segment_id": "S0006",
            "start_offset": 599,
            "end_offset": 604,
        }
        diagnostics = diagnostics_artifact(
            run_id=RUN,
            mention_proposals_logical_sha256=official["logical_content_sha256"],
            mention_proposals_file_sha256=FILE_SHA,
            rejected=[],
            suppressed=[note, dict(note)],
            conflicts=[],
            created_at=CREATED,
        )
        document = build_mention_run_comparison(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)])]),
            official,
            pilot_file_sha256=FILE_SHA,
            mention_proposals_file_sha256=FILE_SHA,
            diagnostics=diagnostics,
            diagnostics_file_sha256=FILE_SHA,
            created_at=CREATED,
        )
        self.assertEqual(len(document["diagnostic_rows"]), 2)
        self.assertEqual(document["metrics"]["raw_diagnostic_record_count"], 2)
        self.assertEqual(document["metrics"]["unique_diagnostic_occurrence_count"], 1)
        self.assertEqual(document["diagnostic_counts"][0]["count"], 2)
        self.assertEqual(document["unique_diagnostic_counts"][0]["count"], 1)
        diagnostic_reviews = [
            row
            for row in document["disagreements"]
            if row["category"] == "DIAGNOSTIC_ONLY"
        ]
        self.assertEqual(len(diagnostic_reviews), 1)
        self.assertEqual(diagnostic_reviews[0]["official_exact_mention"], "פארוק")

    def test_markdown_review_row_survives_hebrew_newline_pipe_and_quotes(self):
        surface = 'אדם\r\n"לוי" | כהן'
        document = compare(
            pilot_bundle([pilot_item(22, [pilot_candidate(surface, 0)])]),
            [official_mention("אחר", 40, item_id=22)],
        )
        self.assertIn("\r\n", document["review_rows"][0]["pilot_exact_mention"])
        self.assertIn("|", document["review_rows"][0]["pilot_exact_mention"])
        report = render_comparison_report(document)
        review = report.split("## Review rows", 1)[1].split("## Diagnostic context", 1)[
            0
        ]
        table_lines = [line for line in review.splitlines() if line.startswith("|")]
        self.assertEqual(len(table_lines), 2 + len(document["review_rows"]))
        self.assertNotIn("\r", report)
        rendered = "\n".join(table_lines)
        self.assertIn("אדם", rendered)
        self.assertIn("\\|", rendered)
        self.assertIn('"לוי"', rendered)
        self.assertEqual(
            _markdown_columns(table_lines[2]), _markdown_columns(table_lines[0])
        )

    def test_pilot_source_without_official_evidence_or_snapshot_is_noncomparable(self):
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)])]),
            [],
        )
        self.assertFalse(document["items"][0]["source_pair_comparable"])
        self.assertEqual(
            document["review_rows"][0]["category"], "PILOT_SOURCE_NONCOMPARABLE"
        )
        self.assertEqual(document["metrics"]["pilot_only_count"], 0)
        self.assertEqual(
            document["metrics"]["pilot_noncomparable_source_mention_count"], 1
        )
        self.assertEqual(document["disagreements"], [])
        self.assertEqual(document["metrics"]["items_with_any_disagreement"], 0)

    def test_snapshot_hash_mismatch_does_not_make_a_pilot_source_comparable(self):
        snapshot = {
            "schema_version": "document-text-snapshot-v1",
            "exported_at": "2026-01-01T00:00:00+00:00",
            "items": [
                {
                    "archive_item_id": 10,
                    "item_type": "OCR_DOCUMENT",
                    "ocr_document": {"displayed_transcription": {"text": "Dana"}},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            snapshot_sha = sha256_file(path)
        document = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Dana", 0)], body="Dana")]),
            [],
            document_snapshot=snapshot,
            document_snapshot_file_sha256=snapshot_sha,
        )
        self.assertFalse(document["official_snapshot_file_matches"])
        self.assertFalse(document["items"][0]["source_pair_comparable"])
        self.assertEqual(
            document["review_rows"][0]["category"], "PILOT_SOURCE_NONCOMPARABLE"
        )
        self.assertEqual(document["metrics"]["pilot_only_count"], 0)
        self.assertEqual(document["disagreements"], [])

    def test_official_mention_or_diagnostic_evaluates_the_exact_source(self):
        mentioned = compare(
            pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
            [official_mention("Dana", 10)],
        )
        self.assertTrue(mentioned["items"][0]["source_pair_comparable"])
        self.assertIn("PILOT_ONLY", categories(mentioned))
        official = proposal([])
        diagnostics = diagnostics_artifact(
            run_id=RUN,
            mention_proposals_logical_sha256=official["logical_content_sha256"],
            mention_proposals_file_sha256=FILE_SHA,
            rejected=[],
            suppressed=[
                {
                    "archive_item_id": 10,
                    "evidence_source": SOURCE,
                    "surface": "עיר",
                    "reason": "PLACE_OR_INSTITUTION_CONTEXT",
                    "segment_id": "S0001",
                    "start_offset": 4,
                    "end_offset": 7,
                }
            ],
            conflicts=[],
            created_at=CREATED,
        )
        diagnosed = build_mention_run_comparison(
            pilot_bundle([pilot_item(10, [pilot_candidate("Ada", 0)])]),
            official,
            pilot_file_sha256=FILE_SHA,
            mention_proposals_file_sha256=FILE_SHA,
            diagnostics=diagnostics,
            diagnostics_file_sha256=FILE_SHA,
            created_at=CREATED,
        )
        self.assertTrue(diagnosed["items"][0]["source_pair_comparable"])
        self.assertEqual(categories(diagnosed).count("PILOT_ONLY"), 1)
        self.assertEqual(diagnosed["metrics"]["pilot_only_count"], 1)
        self.assertEqual(
            diagnosed["metrics"]["pilot_noncomparable_source_mention_count"], 0
        )

    def test_bound_discovery_proves_zero_output_evaluation(self):
        text = "Dana"
        snapshot = {
            "schema_version": "document-text-snapshot-v1",
            "exported_at": "2026-01-01T00:00:00+00:00",
            "items": [
                {
                    "archive_item_id": 10,
                    "item_type": "OCR_DOCUMENT",
                    "ocr_document": {"displayed_transcription": {"text": text}},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            snapshot_sha = sha256_file(snapshot_path)
            selection = stamp_generated_artifact(
                {
                    "schema_version": "discovery-selection-v1",
                    "run_id": RUN,
                    "document_snapshot_file_sha256": snapshot_sha,
                    "prior_checkpoint_logical_sha256": None,
                    "prior_checkpoint_file_sha256": None,
                    "selected_items": [
                        {
                            "archive_item_id": 10,
                            "displayed_text_sha256": "c" * 64,
                            "reason": "not_previously_processed",
                        }
                    ],
                },
                created_at=CREATED,
            )
            discovery_path = root / "discovery.json"
            write_json_atomic(discovery_path, selection)
            discovery_sha = sha256_file(discovery_path)
            official = proposal(
                [],
                snapshot_sha=snapshot_sha,
                discovery_logical=selection["logical_content_sha256"],
                discovery_file=discovery_sha,
            )
            pilot = pilot_bundle(
                [pilot_item(10, [pilot_candidate("Dana", 0)], body=text)]
            )
            without_discovery = build_mention_run_comparison(
                pilot,
                official,
                pilot_file_sha256=FILE_SHA,
                mention_proposals_file_sha256=FILE_SHA,
                document_snapshot=snapshot,
                document_snapshot_file_sha256=snapshot_sha,
                created_at=CREATED,
            )
            self.assertTrue(without_discovery["official_snapshot_file_matches"])
            self.assertFalse(without_discovery["items"][0]["source_pair_comparable"])
            self.assertEqual(
                without_discovery["review_rows"][0]["category"],
                "PILOT_SOURCE_NONCOMPARABLE",
            )
            self.assertEqual(without_discovery["metrics"]["pilot_only_count"], 0)
            evaluated = build_mention_run_comparison(
                pilot,
                official,
                pilot_file_sha256=FILE_SHA,
                mention_proposals_file_sha256=FILE_SHA,
                document_snapshot=snapshot,
                document_snapshot_file_sha256=snapshot_sha,
                discovery=selection,
                discovery_file_sha256=discovery_sha,
                created_at=CREATED,
            )
            validate_artifact(evaluated)
            self.assertTrue(evaluated["discovery_available"])
            self.assertTrue(evaluated["items"][0]["source_pair_comparable"])
            self.assertEqual(categories(evaluated), ["PILOT_ONLY"])
            self.assertEqual(evaluated["metrics"]["pilot_only_count"], 1)
            unselected = stamp_generated_artifact(
                {
                    "schema_version": "discovery-selection-v1",
                    "run_id": RUN,
                    "document_snapshot_file_sha256": snapshot_sha,
                    "prior_checkpoint_logical_sha256": None,
                    "prior_checkpoint_file_sha256": None,
                    "selected_items": [
                        {
                            "archive_item_id": 99,
                            "displayed_text_sha256": "c" * 64,
                            "reason": "not_previously_processed",
                        }
                    ],
                },
                created_at=CREATED,
            )
            other_path = root / "other-discovery.json"
            write_json_atomic(other_path, unselected)
            other_official = proposal(
                [],
                snapshot_sha=snapshot_sha,
                discovery_logical=unselected["logical_content_sha256"],
                discovery_file=sha256_file(other_path),
            )
            skipped = build_mention_run_comparison(
                pilot,
                other_official,
                pilot_file_sha256=FILE_SHA,
                mention_proposals_file_sha256=FILE_SHA,
                document_snapshot=snapshot,
                document_snapshot_file_sha256=snapshot_sha,
                discovery=unselected,
                discovery_file_sha256=sha256_file(other_path),
                created_at=CREATED,
            )
            self.assertFalse(skipped["items"][0]["source_pair_comparable"])
            self.assertEqual(skipped["metrics"]["pilot_only_count"], 0)
            with self.assertRaises(InputHashChangedError):
                build_mention_run_comparison(
                    pilot,
                    official,
                    pilot_file_sha256=FILE_SHA,
                    mention_proposals_file_sha256=FILE_SHA,
                    document_snapshot=snapshot,
                    document_snapshot_file_sha256=snapshot_sha,
                    discovery=unselected,
                    discovery_file_sha256=sha256_file(other_path),
                    created_at=CREATED,
                )
