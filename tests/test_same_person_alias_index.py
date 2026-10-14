import json
import unittest
from pathlib import Path

from vs_archive_people.find_mentions.known_names import known_name_mentions
from vs_archive_people.find_mentions.segment import segment_body
from vs_archive_people.resolution.match import build_resolution_report
from vs_archive_people.resolution.match_v2 import build_resolution_report_v2
from vs_archive_people.resolution.snapshot_index import (
    build_index,
    project_to_identity_snapshot_v1,
)

IDENTITY = json.loads(
    (Path(__file__).parent / "fixtures" / "identity_snapshot.json").read_text(
        encoding="utf-8"
    )
)
CREATED = "2026-01-01T00:00:00Z"
SOURCE = "ocr_document.displayed_transcription.text"


def _with_aliases(*rows: dict) -> dict:
    snapshot = json.loads(json.dumps(IDENTITY))
    snapshot["models"]["PersonAlias"]["rows"].extend(rows)
    return snapshot


def _v1_report(snapshot: dict, exact: str) -> dict:
    from vs_archive_people.artifacts.provenance import stamp_generated_artifact

    evidence = stamp_generated_artifact(
        {
            "schema_version": "evidence-package-v1",
            "run_id": "run-1",
            "document_snapshot_file_sha256": "a" * 64,
            "discovery_logical_sha256": "b" * 64,
            "discovery_file_sha256": "c" * 64,
            "mention_proposals_file_sha256": "1" * 64,
            "production_write_performed": False,
            "items": [],
            "mentions": [
                {
                    "mention_key": "M0001",
                    "mention_fingerprint": "1" * 64,
                    "archive_item_id": 10,
                    "text_role": SOURCE,
                    "evidence_source": SOURCE,
                    "source_text_sha256": "a" * 64,
                    "exact_mention": exact,
                    "evidence_snippet": exact,
                    "start_offset": 0,
                    "end_offset": len(exact),
                    "occurrence_index": 0,
                    "review_status": "PENDING",
                    "mention_class": "PERSON",
                    "propose_alias": False,
                    "proposed_identity_action": None,
                    "confidence": None,
                }
            ],
        },
        created_at=CREATED,
    )
    return build_resolution_report(
        evidence,
        build_index(snapshot),
        evidence_package_file_sha256="d" * 64,
        identity_snapshot_file_sha256="e" * 64,
        created_at=CREATED,
    )


def _v2_report(snapshot: dict, exact: str) -> dict:
    from vs_archive_people.artifacts.provenance import stamp_generated_artifact

    evidence = stamp_generated_artifact(
        {
            "schema_version": "evidence-package-v2",
            "run_id": "run-1",
            "document_snapshot_file_sha256": "a" * 64,
            "discovery_logical_sha256": "b" * 64,
            "discovery_file_sha256": "c" * 64,
            "mention_proposals_logical_sha256": "d" * 64,
            "mention_proposals_file_sha256": "e" * 64,
            "production_write_performed": False,
            "items": [],
            "mentions": [
                {
                    "mention_key": "M0001",
                    "mention_fingerprint": "1" * 64,
                    "archive_item_id": 10,
                    "evidence_source": SOURCE,
                    "source_text_sha256": "a" * 64,
                    "exact_mention": exact,
                    "start_offset": 0,
                    "end_offset": len(exact),
                    "evidence_snippet": exact,
                    "occurrence_index": 0,
                    "name_surface": exact,
                    "name_start_offset": 0,
                    "name_end_offset": len(exact),
                    "title_surface": None,
                    "title_start_offset": None,
                    "title_end_offset": None,
                    "surface_kind": "FULL_NAME",
                    "detector_ids": ["semantic-surface"],
                    "known_name_match_quality": None,
                }
            ],
        },
        created_at=CREATED,
    )
    return build_resolution_report_v2(
        evidence,
        build_index(snapshot),
        run_id="run-1",
        evidence_package_file_sha256="f" * 64,
        identity_snapshot_file_sha256="9" * 64,
        created_at=CREATED,
    )


class SamePersonAliasIndexTests(unittest.TestCase):
    def test_same_person_normalized_aliases_are_one_candidate(self):
        snapshot = _with_aliases(
            {"id": 265, "person_id": 1, "name": "Nadav\nCohen"},
            {"id": 179, "person_id": 1, "name": "Nadav Cohen"},
        )
        index = build_index(snapshot)
        self.assertEqual(index.lookup("Nadav Cohen"), [(1, "person_alias")])
        self.assertEqual(index.alias_id(1, "Nadav\nCohen"), 179)
        self.assertIn("Nadav Cohen", index.exact_names)
        self.assertIn("Nadav\nCohen", index.exact_names)
        self.assertEqual(index.lookup("Ada Lovelace"), [(1, "canonical_name")])

    def test_newline_form_matches_the_person_3_shape(self):
        snapshot = _with_aliases(
            {"id": 179, "person_id": 2, "name": "Youssef Aslan Cattaoui Pacha"},
            {"id": 265, "person_id": 2, "name": "Youssef\nAslan Cattaoui Pacha"},
        )
        index = build_index(snapshot)
        self.assertEqual(
            index.lookup("Youssef Aslan Cattaoui Pacha"), [(2, "person_alias")]
        )
        self.assertEqual(index.alias_id(2, "youssef  aslan\ncattaoui   pacha"), 179)

    def test_cross_person_collision_stays_ambiguous(self):
        snapshot = _with_aliases(
            {"id": 179, "person_id": 1, "name": "Shared Name"},
            {"id": 265, "person_id": 2, "name": "Shared\nName"},
        )
        index = build_index(snapshot)
        self.assertEqual(
            index.lookup("Shared Name"),
            [(1, "person_alias"), (2, "person_alias")],
        )
        report = _v2_report(snapshot, "Shared Name")
        row = report["resolutions"][0]
        self.assertEqual(row["classification"], "AMBIGUOUS")
        self.assertEqual(row["matched_person_ids"], [1, 2])
        self.assertIsNone(row["matched_alias_id"])

    def test_alias_id_is_the_lowest_id_regardless_of_row_order(self):
        first = _with_aliases(
            {"id": 265, "person_id": 1, "name": "Nadav\nCohen"},
            {"id": 179, "person_id": 1, "name": "Nadav Cohen"},
        )
        second = _with_aliases(
            {"id": 179, "person_id": 1, "name": "Nadav Cohen"},
            {"id": 265, "person_id": 1, "name": "Nadav\nCohen"},
        )
        forward = _v2_report(first, "Nadav Cohen")["resolutions"][0]
        backward = _v2_report(second, "Nadav Cohen")["resolutions"][0]
        self.assertEqual(forward["classification"], "DETERMINISTIC_EXISTING_PERSON")
        self.assertEqual(forward["matched_via"], "person_alias")
        self.assertEqual(forward["matched_alias_id"], 179)
        self.assertEqual(forward["matched_person_ids"], [1])
        self.assertEqual(backward["matched_alias_id"], 179)
        self.assertEqual(backward["matched_person_ids"], forward["matched_person_ids"])
        v1 = _v1_report(second, "Nadav Cohen")["resolutions"][0]
        self.assertEqual(v1["matched_alias_id"], 179)
        self.assertEqual(v1["matched_via"], "person_alias")

    def test_known_name_search_keeps_both_literal_surfaces(self):
        snapshot = _with_aliases(
            {"id": 265, "person_id": 1, "name": "Nadav\nCohen"},
            {"id": 179, "person_id": 1, "name": "Nadav Cohen"},
        )
        flipped = _with_aliases(
            {"id": 179, "person_id": 1, "name": "Nadav Cohen"},
            {"id": 265, "person_id": 1, "name": "Nadav\nCohen"},
        )
        body = "Nadav Cohen appears once."
        first = known_name_mentions(
            body, build_index(snapshot).exact_names, segment_body(body)
        )
        second = known_name_mentions(
            body, build_index(flipped).exact_names, segment_body(body)
        )
        first_spans = sorted((row.start_offset, row.end_offset) for row in first[0])
        second_spans = sorted((row.start_offset, row.end_offset) for row in second[0])
        self.assertEqual(first_spans, second_spans)
        self.assertEqual(first_spans, [(0, len("Nadav Cohen"))])
        self.assertIn("Nadav\nCohen", build_index(snapshot).exact_names)

    def test_projection_keeps_alias_rows_and_only_renames_the_schema(self):
        source = json.loads(json.dumps(IDENTITY))
        source["schema_version"] = (
            "vs-archive-biography-production-identity-snapshot-v1"
        )
        source["models"]["PersonAlias"]["rows"].append(
            {"id": 265, "person_id": 1, "name": "Augusta\nKing"}
        )
        before = len(source["models"]["PersonAlias"]["rows"])
        projected = project_to_identity_snapshot_v1(source)
        self.assertEqual(projected["schema_version"], "identity-snapshot-v1")
        self.assertEqual(
            source["schema_version"],
            "vs-archive-biography-production-identity-snapshot-v1",
        )
        self.assertEqual(len(projected["models"]["PersonAlias"]["rows"]), before)
        self.assertEqual(build_index(projected).alias_id(1, "Augusta King"), 10)
        self.assertEqual(build_index(projected).alias_id(1, "Augusta\nKing"), 10)
