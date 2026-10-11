import json
import unittest
from pathlib import Path

from vs_archive_people.artifacts.provenance import stamp_generated_artifact
from vs_archive_people.identity_review.package import build_review_package
from vs_archive_people.resolution.match import build_resolution_report
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
        "source_text_sha256": "ab" * 32,
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


def package_for(mentions):
    report = build_resolution_report(
        stamp_generated_artifact(
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
        ),
        build_index(IDENTITY),
        evidence_package_file_sha256="d" * 64,
        identity_snapshot_file_sha256="e" * 64,
    )
    return build_review_package(report, resolution_report_file_sha256="f" * 64)


class ReviewPackageTests(unittest.TestCase):
    def test_review_rows_cover_ambiguous_new_alias_and_identity_actions(self):
        package = package_for(
            [
                mention("M0001", "Ada Lovelace"),
                mention("M0002", "Dana"),
                mention("M0003", "Unknown Person"),
                mention("M0004", "Another Unknown", propose_alias=True),
                mention(
                    "M0005",
                    "Grace Hopper",
                    proposed_identity_action="CANONICAL_NAME_CHANGE",
                ),
            ]
        )
        self.assertEqual(package["kind"], "IDENTITY_REVIEW_NOT_APPLY")
        self.assertFalse(package["production_write_performed"])
        self.assertFalse(package["photo_person_created"])
        self.assertFalse(package["biography_fields_included"])
        reasons = {
            row["mention_key"]: row["review_reason"] for row in package["review_rows"]
        }
        self.assertNotIn("M0001", reasons)
        self.assertIn("ambiguous_existing_person", reasons["M0002"])
        self.assertIn("new_person_candidate", reasons["M0003"])
        self.assertIn("inferred_alias", reasons["M0004"])
        self.assertIn("canonical_name_change", reasons["M0005"])
        auto_keys = [row["mention_key"] for row in package["auto_resolution_proposals"]]
        self.assertEqual(auto_keys, ["M0001", "M0005"])
        self.assertTrue(
            all(
                row["proposal_status"] == "PROPOSED_NOT_APPLIED"
                for row in package["auto_resolution_proposals"]
            )
        )
        review = next(
            row for row in package["review_rows"] if row["mention_key"] == "M0003"
        )
        auto = next(
            row
            for row in package["auto_resolution_proposals"]
            if row["mention_key"] == "M0001"
        )
        for row in (review, auto):
            self.assertEqual(
                row["evidence_source"], "ocr_document.displayed_transcription.text"
            )
            self.assertEqual(row["source_text_sha256"], "ab" * 32)
            self.assertEqual(row["occurrence_index"], 0)
            self.assertEqual(len(row["mention_fingerprint"]), 64)
            self.assertTrue(row["evidence_snippet"])
        self.assertEqual(auto["evidence_snippet"], "Ada Lovelace")
        self.assertNotIn("photo_content_id", json.dumps(package))
        forbidden = set()

        def collect(value):
            if isinstance(value, dict):
                forbidden.update(value)
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(package)
        self.assertTrue(
            forbidden.isdisjoint(
                {
                    "biography",
                    "existing_biography",
                    "proposed_biography",
                    "PhotoPerson",
                    "photo_person_id",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
