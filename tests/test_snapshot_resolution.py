import json
import unittest
from pathlib import Path

from vs_archive_people.artifacts.io import ArtifactError
from vs_archive_people.artifacts.provenance import stamp_generated_artifact
from vs_archive_people.resolution.match import (
    CLASS_AMBIGUOUS,
    CLASS_DETERMINISTIC,
    CLASS_NEW,
    CLASS_NOT_PERSON,
    build_resolution_report,
)
from vs_archive_people.resolution.snapshot_index import build_index

IDENTITY = json.loads(
    (Path(__file__).parent / "fixtures" / "identity_snapshot.json").read_text(
        encoding="utf-8"
    )
)


def mention(key, exact, **extra):
    row = {
        "mention_key": key,
        "archive_item_id": 10,
        "exact_mention": exact,
        "text_role": "ocr_document.displayed_transcription.text",
        "evidence_source": "ocr_document.displayed_transcription.text",
        "source_text_sha256": "a" * 64,
        "evidence_snippet": exact,
        "start_offset": 0,
        "end_offset": len(exact),
        "occurrence_index": 0,
        "mention_fingerprint": f"{int(key[1:]):064x}",
        "review_status": "PENDING",
        "mention_class": "PERSON",
        "propose_alias": False,
        "proposed_identity_action": None,
        "confidence": None,
    }
    row.update(extra)
    return row


def evidence(mentions):
    return stamp_generated_artifact(
        {
            "schema_version": "evidence-package-v1",
            "run_id": "run-1",
            "document_snapshot_file_sha256": "a" * 64,
            "discovery_logical_sha256": "b" * 64,
            "discovery_file_sha256": "c" * 64,
            "mention_proposals_file_sha256": "1" * 64,
            "production_write_performed": False,
            "items": [],
            "mentions": mentions,
        },
        created_at="2026-01-01T00:00:00Z",
    )


def report_for(mentions, local=None):
    return build_resolution_report(
        evidence(mentions),
        build_index(IDENTITY),
        evidence_package_file_sha256="d" * 64,
        identity_snapshot_file_sha256="e" * 64,
        local_candidates=local,
        local_candidates_file_sha256=None if local is None else "9" * 64,
    )


class SnapshotResolutionTests(unittest.TestCase):
    def test_absent_local_candidates_record_a_null_file_hash(self):
        report = report_for([mention("M0001", "Ada Lovelace")])
        self.assertIsNone(report["local_candidates_file_sha256"])
        self.assertFalse(report["local_candidates_are_production_identity"])

    def test_local_candidates_file_hash_is_audit_only(self):
        mentions = [mention("M0001", "Ada Lovelace")]
        candidates = [{"name": "Not In Production"}]
        created = "2026-01-01T00:00:00Z"
        first = build_resolution_report(
            evidence(mentions),
            build_index(IDENTITY),
            evidence_package_file_sha256="d" * 64,
            identity_snapshot_file_sha256="e" * 64,
            local_candidates=candidates,
            local_candidates_file_sha256="a" * 64,
            created_at=created,
        )
        second = build_resolution_report(
            evidence(mentions),
            build_index(IDENTITY),
            evidence_package_file_sha256="d" * 64,
            identity_snapshot_file_sha256="e" * 64,
            local_candidates=candidates,
            local_candidates_file_sha256="b" * 64,
            created_at=created,
        )
        self.assertEqual(first["local_candidates_file_sha256"], "a" * 64)
        self.assertEqual(second["local_candidates_file_sha256"], "b" * 64)
        self.assertEqual(
            first["logical_content_sha256"], second["logical_content_sha256"]
        )
        self.assertFalse(first["local_candidates_are_production_identity"])
        self.assertFalse(second["local_candidates_are_production_identity"])

    def test_local_candidates_and_file_hash_must_be_paired(self):
        mentions = [mention("M0001", "Ada Lovelace")]
        index = build_index(IDENTITY)
        created = "2026-01-01T00:00:00Z"
        candidates = [{"name": "Local Only", "review_status": "PENDING"}]

        def build(local, digest):
            return build_resolution_report(
                evidence(mentions),
                index,
                evidence_package_file_sha256="d" * 64,
                identity_snapshot_file_sha256="e" * 64,
                local_candidates=local,
                local_candidates_file_sha256=digest,
                created_at=created,
            )

        absent = build(None, None)
        self.assertIsNone(absent["local_candidates_file_sha256"])
        present = build(candidates, "a" * 64)
        self.assertEqual(present["local_candidates_file_sha256"], "a" * 64)
        empty = build([], "b" * 64)
        self.assertEqual(empty["local_candidates_file_sha256"], "b" * 64)
        self.assertFalse(empty["resolutions"][0]["local_candidate_overlap"])
        for local, digest in (
            (candidates, None),
            ([], None),
            (None, "c" * 64),
        ):
            with self.subTest(local=local, digest=digest):
                with self.assertRaises(ArtifactError) as caught:
                    build(local, digest)
                self.assertIn("local_candidates_file_sha256", str(caught.exception))
                self.assertEqual(caught.exception.error_code, "MALFORMED_ARTIFACT")

    def test_unique_canonical_name_is_deterministic(self):
        row = report_for([mention("M0001", "Ada Lovelace")])["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_DETERMINISTIC)
        self.assertEqual(row["matched_person_ids"], [1])
        self.assertEqual(row["matched_via"], "canonical_name")
        self.assertEqual(row["stable_keys"], ["C:1"])
        self.assertEqual(row["evidence_snippet"], "Ada Lovelace")
        self.assertEqual(row["mention_fingerprint"], f"{1:064x}")
        self.assertNotIn("biography", row)
        self.assertFalse(row["inferred_alias_proposal"])

    def test_unique_alias_is_an_existing_alias_not_an_inferred_one(self):
        row = report_for([mention("M0001", "augusta  king")])["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_DETERMINISTIC)
        self.assertEqual(row["matched_via"], "person_alias")
        self.assertEqual(row["matched_alias_id"], 10)
        self.assertFalse(row["inferred_alias_proposal"])

    def test_shared_alias_is_ambiguous(self):
        row = report_for([mention("M0001", "Dana")])["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_AMBIGUOUS)
        self.assertEqual(row["matched_person_ids"], [3, 4])
        self.assertIsNone(row["matched_via"])
        self.assertFalse(row["identity_is_production"])

    def test_unknown_name_stays_a_candidate_when_confidence_is_100(self):
        row = report_for([mention("M0001", "Unknown Person", confidence=100)])[
            "resolutions"
        ][0]
        self.assertEqual(row["classification"], CLASS_NEW)
        self.assertEqual(row["confidence"], 100)
        self.assertFalse(row["confidence_used_for_classification"])
        self.assertFalse(row["identity_is_production"])

    def test_explicit_not_a_person_is_not_matched(self):
        row = report_for(
            [mention("M0001", "Ada Lovelace", mention_class="NOT_A_PERSON")]
        )["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_NOT_PERSON)
        self.assertEqual(row["matched_person_ids"], [])

    def test_pending_snapshot_person_is_not_production_identity(self):
        row = report_for([mention("M0001", "Pending Person")])["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_NEW)

    def test_local_pending_candidate_does_not_become_deterministic(self):
        row = report_for(
            [mention("M0001", "Local Only")],
            local=[{"name": "Local Only", "review_status": "PENDING"}],
        )["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_NEW)
        self.assertTrue(row["local_candidate_overlap"])
        self.assertFalse(row["identity_is_production"])

    def test_photo_person_rows_are_ignored_and_not_copied(self):
        built = report_for([mention("M0001", "Grace Hopper")])
        self.assertEqual(built["photo_person_rows_ignored"], 1)
        self.assertFalse(built["photo_person_created"])
        encoded = json.dumps(built)
        self.assertNotIn("photo_content_id", encoded)
        self.assertNotIn("Do not copy this biography.", encoded)

    def test_one_resolution_per_mention_key(self):
        built = report_for(
            [
                mention("M0001", "Ada Lovelace", confidence=0),
                mention("M0002", "Grace Hopper", confidence=100),
            ]
        )
        self.assertEqual(
            [row["mention_key"] for row in built["resolutions"]],
            ["M0001", "M0002"],
        )
        self.assertEqual(built["resolutions"][0]["classification"], CLASS_DETERMINISTIC)
        self.assertEqual(built["resolutions"][1]["classification"], CLASS_DETERMINISTIC)

    def test_explicit_alias_and_merge_flags_do_not_change_classification(self):
        row = report_for(
            [
                mention(
                    "M0001",
                    "New Alias",
                    propose_alias=True,
                    proposed_identity_action="MERGE",
                    confidence=99,
                )
            ]
        )["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_NEW)
        self.assertTrue(row["inferred_alias_proposal"])
        self.assertEqual(row["proposed_identity_action"], "MERGE")


if __name__ == "__main__":
    unittest.main()
