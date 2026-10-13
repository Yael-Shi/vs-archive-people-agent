import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vs_archive_people.artifacts.io import (
    RESOLUTION_REPORT_SCHEMA,
    ArtifactError,
    LogicalHashMismatchError,
    RunIdMismatchError,
    UnsupportedSchemaVersionError,
    sha256_file,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import stamp_generated_artifact
from vs_archive_people.artifacts.schema_validate import (
    RUN_STEPS,
    load_schema,
    published_schema_versions,
)
from vs_archive_people.cli import main
from vs_archive_people.identity_review.package import build_review_package
from vs_archive_people.identity_review.package_v2 import (
    COPIED_FIELDS,
    build_review_package_v2,
)
from vs_archive_people.resolution.match import (
    CLASS_AMBIGUOUS,
    CLASS_DETERMINISTIC,
    CLASS_NEW,
    build_resolution_report,
)
from vs_archive_people.resolution.match_v2 import build_resolution_report_v2
from vs_archive_people.resolution.snapshot_index import build_index

IDENTITY = json.loads(
    (Path(__file__).parent / "fixtures" / "identity_snapshot.json").read_text(
        encoding="utf-8"
    )
)
CREATED = "2026-01-01T00:00:00Z"
LATER = "2026-06-01T00:00:00Z"
SOURCE = "ocr_document.displayed_transcription.text"
FILE_HASH = "f" * 64


def mention(key, exact, name=None, **extra):
    surface = exact if name is None else name
    row = {
        "mention_key": key,
        "mention_fingerprint": f"{int(key[1:]):064x}",
        "archive_item_id": 10,
        "evidence_source": SOURCE,
        "source_text_sha256": "a" * 64,
        "exact_mention": exact,
        "start_offset": 0,
        "end_offset": len(exact),
        "evidence_snippet": exact,
        "occurrence_index": 0,
        "name_surface": surface,
        "name_start_offset": exact.index(surface),
        "name_end_offset": exact.index(surface) + len(surface),
        "title_surface": None,
        "title_start_offset": None,
        "title_end_offset": None,
        "surface_kind": "FULL_NAME",
        "detector_ids": ["semantic-surface"],
        "known_name_match_quality": None,
    }
    row.update(extra)
    return row


def evidence(mentions, *, run_id="run-1"):
    return stamp_generated_artifact(
        {
            "schema_version": "evidence-package-v2",
            "run_id": run_id,
            "document_snapshot_file_sha256": "a" * 64,
            "discovery_logical_sha256": "b" * 64,
            "discovery_file_sha256": "c" * 64,
            "mention_proposals_logical_sha256": "d" * 64,
            "mention_proposals_file_sha256": "e" * 64,
            "production_write_performed": False,
            "items": [],
            "mentions": mentions,
        },
        created_at=CREATED,
    )


def v1_evidence(exact="Ada Lovelace"):
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
            "mentions": [
                {
                    "mention_key": "M0001",
                    "archive_item_id": 10,
                    "exact_mention": exact,
                    "text_role": SOURCE,
                    "evidence_source": SOURCE,
                    "source_text_sha256": "ab" * 32,
                    "evidence_snippet": exact,
                    "start_offset": 0,
                    "end_offset": len(exact),
                    "occurrence_index": 0,
                    "mention_fingerprint": f"{1:064x}",
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


def report_for(mentions, *, run_id="run-1"):
    return build_resolution_report_v2(
        evidence(mentions, run_id=run_id),
        build_index(IDENTITY),
        run_id=run_id,
        evidence_package_file_sha256="d" * 64,
        identity_snapshot_file_sha256="e" * 64,
        created_at=CREATED,
    )


def package_for(report, *, run_id="run-1", file_hash=FILE_HASH, created_at=CREATED):
    return build_review_package_v2(
        report,
        run_id=run_id,
        resolution_report_file_sha256=file_hash,
        created_at=created_at,
    )


def restamp(report, **changes):
    cloned = copy.deepcopy(report)
    cloned.update(changes)
    return stamp_generated_artifact(cloned, created_at=CREATED)


class IdentityReviewPackageV2Tests(unittest.TestCase):
    def test_every_classification_stays_pending_in_one_queue(self):
        package = package_for(
            report_for(
                [
                    mention("M0001", "Ada Lovelace"),
                    mention(
                        "M0002",
                        "Dana",
                        detector_ids=["known-name"],
                        known_name_match_quality="EXACT_BOUNDARY",
                        surface_kind="PARTIAL_NAME",
                    ),
                    mention("M0003", "Nobody"),
                ]
            )
        )
        self.assertEqual(package["schema_version"], "identity-review-package-v2")
        self.assertEqual(package["kind"], "IDENTITY_REVIEW_NOT_APPLY")
        self.assertFalse(package["production_write_performed"])
        self.assertFalse(package["photo_person_created"])
        self.assertFalse(package["biography_fields_included"])
        self.assertFalse(package["human_approval_performed"])
        self.assertFalse(package["apply_authorized"])
        encoded = json.dumps(package)
        self.assertNotIn("auto_resolution_proposals", encoded)
        self.assertNotIn("proposal_status", encoded)
        self.assertNotIn("PROPOSED_NOT_APPLIED", encoded)
        self.assertNotIn("NOT_A_PERSON", encoded)
        reasons = {
            row["mention_key"]: row["review_reason"] for row in package["review_rows"]
        }
        self.assertEqual(
            reasons,
            {
                "M0001": "deterministic_existing_person_pending_review",
                "M0002": "ambiguous_existing_person",
                "M0003": "new_person_candidate",
            },
        )
        self.assertTrue(
            all(row["review_status"] == "PENDING" for row in package["review_rows"])
        )
        classes = {
            row["mention_key"]: row["classification"] for row in package["review_rows"]
        }
        self.assertEqual(classes["M0001"], CLASS_DETERMINISTIC)
        self.assertEqual(classes["M0002"], CLASS_AMBIGUOUS)
        self.assertEqual(classes["M0003"], CLASS_NEW)

    def test_known_name_deterministic_row_stays_pending(self):
        package = package_for(
            report_for(
                [
                    mention(
                        "M0001",
                        "Ada Lovelace",
                        detector_ids=["known-name"],
                        known_name_match_quality="EXACT_BOUNDARY",
                    )
                ]
            )
        )
        row = package["review_rows"][0]
        self.assertEqual(row["classification"], CLASS_DETERMINISTIC)
        self.assertEqual(
            row["review_reason"], "deterministic_existing_person_pending_review"
        )
        self.assertTrue(row["identity_is_production"])
        self.assertEqual(row["review_status"], "PENDING")
        self.assertEqual(row["detector_ids"], ["known-name"])
        self.assertFalse(package["human_approval_performed"])
        self.assertFalse(package["apply_authorized"])
        self.assertNotIn("approved", json.dumps(row))
        self.assertNotIn("human_decision", json.dumps(row))

    def test_geometry_and_match_fields_are_copied_unchanged(self):
        exact = "Lady Augusta King"
        surface = "Augusta King"
        source = mention(
            "M0004",
            exact,
            name=surface,
            title_surface="Lady",
            title_start_offset=0,
            title_end_offset=4,
            name_start_offset=exact.index(surface),
            name_end_offset=exact.index(surface) + len(surface),
            surface_kind="TITLE_AND_NAME",
            detector_ids=["known-name", "semantic-surface"],
            known_name_match_quality="EXACT_BOUNDARY",
            occurrence_index=3,
            evidence_snippet="saw Lady Augusta King",
        )
        resolution = report_for([source])["resolutions"][0]
        row = package_for(report_for([source]))["review_rows"][0]
        for field in COPIED_FIELDS:
            self.assertEqual(row[field], resolution[field], field)
        self.assertEqual(row["matched_via"], "person_alias")
        self.assertEqual(row["matched_alias_id"], 10)
        self.assertEqual(row["review_status"], "PENDING")

    def test_rows_follow_mention_key_when_resolution_order_changes(self):
        report = report_for(
            [
                mention("M0003", "Nobody"),
                mention("M0001", "Ada Lovelace"),
                mention("M0002", "Grace Hopper"),
            ]
        )
        shuffled = copy.deepcopy(report)
        shuffled["resolutions"] = list(reversed(shuffled["resolutions"]))
        shuffled = stamp_generated_artifact(shuffled, created_at=CREATED)
        first = package_for(report)
        second = package_for(shuffled)
        keys = ["M0001", "M0002", "M0003"]
        self.assertEqual([row["mention_key"] for row in first["review_rows"]], keys)
        self.assertEqual([row["mention_key"] for row in second["review_rows"]], keys)
        self.assertEqual(first["review_rows"], second["review_rows"])
        self.assertNotEqual(
            first["resolution_report_logical_sha256"],
            second["resolution_report_logical_sha256"],
        )

    def test_parent_logical_hash_is_stable_across_audit_hash_and_timestamp(self):
        report = report_for([mention("M0001", "Ada Lovelace")])
        later = stamp_generated_artifact(copy.deepcopy(report), created_at=LATER)
        self.assertEqual(
            report["logical_content_sha256"], later["logical_content_sha256"]
        )
        first = package_for(report, file_hash="a" * 64)
        restamped = package_for(later, file_hash="a" * 64)
        other_file = package_for(report, file_hash="b" * 64)
        self.assertEqual(
            first["resolution_report_logical_sha256"],
            report["logical_content_sha256"],
        )
        self.assertEqual(
            first["logical_content_sha256"], restamped["logical_content_sha256"]
        )
        self.assertEqual(
            first["logical_content_sha256"], other_file["logical_content_sha256"]
        )
        self.assertEqual(other_file["resolution_report_file_sha256"], "b" * 64)
        self.assertNotEqual(
            first["resolution_report_file_sha256"],
            other_file["resolution_report_file_sha256"],
        )

    def test_generations_stay_separate_and_v1_auto_proposals_remain(self):
        v2_report = report_for([mention("M0001", "Ada Lovelace")])
        with self.assertRaises(UnsupportedSchemaVersionError) as rejected_v1:
            package_for(
                build_resolution_report(
                    v1_evidence(),
                    build_index(IDENTITY),
                    evidence_package_file_sha256="d" * 64,
                    identity_snapshot_file_sha256="e" * 64,
                    created_at=CREATED,
                )
            )
        self.assertIn("review-package", str(rejected_v1.exception))
        with self.assertRaises(UnsupportedSchemaVersionError) as rejected_v2:
            build_review_package(v2_report, resolution_report_file_sha256=FILE_HASH)
        self.assertIn("review-v2", str(rejected_v2.exception))
        v1_package = build_review_package(
            build_resolution_report(
                v1_evidence("Ada Lovelace"),
                build_index(IDENTITY),
                evidence_package_file_sha256="d" * 64,
                identity_snapshot_file_sha256="e" * 64,
                created_at=CREATED,
            ),
            resolution_report_file_sha256=FILE_HASH,
        )
        self.assertEqual(v1_package["schema_version"], "identity-review-package-v1")
        self.assertEqual(
            [row["mention_key"] for row in v1_package["auto_resolution_proposals"]],
            ["M0001"],
        )
        self.assertEqual(
            v1_package["auto_resolution_proposals"][0]["proposal_status"],
            "PROPOSED_NOT_APPLIED",
        )
        self.assertEqual(v1_package["review_rows"], [])

    def test_integrity_failures_publish_nothing(self):
        report = report_for(
            [
                mention("M0001", "Ada Lovelace"),
                mention("M0002", "Dana"),
                mention("M0003", "Nobody"),
            ]
        )
        with self.assertRaises(RunIdMismatchError):
            package_for(report, run_id="other-run")
        broken_hash = copy.deepcopy(report)
        broken_hash["resolutions"][0]["exact_mention"] = "Ada Lovelace!"
        with self.assertRaises(LogicalHashMismatchError):
            package_for(broken_hash)
        duplicate_key = copy.deepcopy(report)
        duplicate_key["resolutions"].append(
            copy.deepcopy(duplicate_key["resolutions"][0])
        )
        with self.assertRaises(ArtifactError) as duplicate_keys:
            package_for(stamp_generated_artifact(duplicate_key, created_at=CREATED))
        self.assertIn("duplicate mention_key", str(duplicate_keys.exception))
        duplicate_fingerprint = copy.deepcopy(report)
        extra = copy.deepcopy(duplicate_fingerprint["resolutions"][0])
        extra["mention_key"] = "M0009"
        duplicate_fingerprint["resolutions"].append(extra)
        with self.assertRaises(ArtifactError) as duplicate_prints:
            package_for(
                stamp_generated_artifact(duplicate_fingerprint, created_at=CREATED)
            )
        self.assertIn("duplicate mention_fingerprint", str(duplicate_prints.exception))

        def tamper(index, **fields):
            cloned = copy.deepcopy(report)
            cloned["resolutions"][index].update(fields)
            return stamp_generated_artifact(cloned, created_at=CREATED)

        with self.assertRaises(ArtifactError) as bad_identity:
            package_for(tamper(0, identity_is_production=False))
        self.assertIn("identity_is_production", str(bad_identity.exception))
        with self.assertRaises(ArtifactError) as bad_count:
            package_for(tamper(0, matched_person_ids=[1, 2]))
        self.assertIn("exactly one", str(bad_count.exception))
        with self.assertRaises(ArtifactError) as bad_ambiguous:
            package_for(
                tamper(
                    1,
                    matched_person_name="Dana Cohen",
                    matched_via="canonical_name",
                    matched_alias_id=None,
                )
            )
        self.assertIn("AMBIGUOUS", str(bad_ambiguous.exception))
        with self.assertRaises(ArtifactError) as bad_new:
            package_for(tamper(2, matched_person_ids=[1]))
        self.assertIn("NEW_PERSON_CANDIDATE", str(bad_new.exception))
        malformed = copy.deepcopy(report)
        del malformed["resolutions"][0]["name_surface"]
        with self.assertRaises(ArtifactError) as malformed_report:
            package_for(stamp_generated_artifact(malformed, created_at=CREATED))
        self.assertEqual(malformed_report.exception.error_code, "MALFORMED_ARTIFACT")
        unexpected = restamp(report, schema_version="resolution-report-v9")
        with self.assertRaises(UnsupportedSchemaVersionError):
            package_for(unexpected)

    def test_empty_resolution_list_publishes_an_empty_pending_queue(self):
        package = package_for(report_for([]))
        self.assertEqual(package["review_rows"], [])
        self.assertFalse(package["production_write_performed"])
        self.assertFalse(package["human_approval_performed"])
        self.assertFalse(package["apply_authorized"])
        self.assertFalse(package["photo_person_created"])
        self.assertFalse(package["biography_fields_included"])

    def test_cli_publishes_pending_queue_and_refuses_the_wrong_generation(self):
        identity_path = Path(__file__).parent / "fixtures" / "identity_snapshot.json"
        before = identity_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "resolution-v2.json"
            v1_path = root / "resolution-v1.json"
            output = root / "identity-review-v2.json"
            write_json_atomic(
                report_path,
                report_for(
                    [
                        mention("M0001", "Ada Lovelace"),
                        mention("M0002", "Dana"),
                        mention("M0003", "Nobody"),
                    ]
                ),
            )
            write_json_atomic(
                v1_path,
                build_resolution_report(
                    v1_evidence(),
                    build_index(IDENTITY),
                    evidence_package_file_sha256="d" * 64,
                    identity_snapshot_file_sha256="e" * 64,
                    created_at=CREATED,
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                published = main(
                    [
                        "review-v2",
                        "--resolution",
                        str(report_path),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(published, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload["schema_version"], "identity-review-package-v2")
            self.assertEqual(payload["review_rows"], 3)
            self.assertEqual(payload["deterministic_pending_count"], 1)
            self.assertEqual(payload["ambiguous_pending_count"], 1)
            self.assertEqual(payload["new_person_candidate_pending_count"], 1)
            self.assertFalse(payload["production_write_performed"])
            self.assertFalse(payload["human_approval_performed"])
            self.assertFalse(payload["apply_authorized"])
            self.assertEqual(payload["output_sha256"], sha256_file(output))
            body = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                body["resolution_report_file_sha256"], sha256_file(report_path)
            )
            self.assertNotIn("auto_resolved", stdout.getvalue())
            self.assertNotIn("approved_count", stdout.getvalue())
            original = output.read_bytes()
            stderr = StringIO()
            with redirect_stderr(stderr):
                exists = main(
                    [
                        "review-v2",
                        "--resolution",
                        str(report_path),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(exists, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"], "ARTIFACT_EXISTS"
            )
            self.assertEqual(output.read_bytes(), original)
            rejected_out = root / "from-v1.json"
            stderr = StringIO()
            with redirect_stderr(stderr):
                rejected = main(
                    [
                        "review-v2",
                        "--resolution",
                        str(v1_path),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(rejected_out),
                    ]
                )
            self.assertEqual(rejected, 2)
            blocked = json.loads(stderr.getvalue())
            self.assertEqual(blocked["error_code"], "UNSUPPORTED_SCHEMA_VERSION")
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertFalse(rejected_out.exists())
            self.assertEqual(list(root.glob("*.tmp")), [])
            self.assertEqual(list(root.glob(".*.tmp")), [])
            self.assertEqual(
                json.loads(v1_path.read_text(encoding="utf-8"))["schema_version"],
                RESOLUTION_REPORT_SCHEMA,
            )
        self.assertEqual(identity_path.read_bytes(), before)

    def test_checkpoint_contract_stays_on_v1(self):
        self.assertEqual(
            RUN_STEPS,
            ("discover", "freeze-evidence", "resolve", "review"),
        )
        versions = published_schema_versions()
        self.assertEqual(
            versions["identity_review_package"], "identity-review-package-v1"
        )
        self.assertNotIn("identity_review_package_v2", versions)
        checkpoint = load_schema("run-checkpoint-v1")
        identity_review = checkpoint["properties"]["schema_versions"]["properties"][
            "identity_review_package"
        ]
        self.assertEqual(identity_review["const"], "identity-review-package-v1")
        self.assertEqual(
            checkpoint["properties"]["completed_steps"]["items"]["enum"],
            ["discover", "freeze-evidence", "resolve", "review"],
        )


if __name__ == "__main__":
    unittest.main()
