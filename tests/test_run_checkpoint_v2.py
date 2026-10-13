import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vs_archive_people.artifacts.io import (
    RUN_CHECKPOINT_SCHEMA,
    RUN_CHECKPOINT_V2_SCHEMA,
    ArtifactError,
    IncompletePriorCheckpointError,
    UnsupportedSchemaVersionError,
    sha256_file,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import stamp_generated_artifact
from vs_archive_people.artifacts.schema_validate import (
    RUN_STEPS,
    published_schema_versions,
)
from vs_archive_people.cli import main
from vs_archive_people.discovery.select import select_items
from vs_archive_people.runs.checkpoint import append_step, initial_checkpoint

DOCUMENTS = json.loads(
    (Path(__file__).parent / "fixtures" / "document_text_snapshot.json").read_text(
        encoding="utf-8"
    )
)
CREATED = "2026-01-01T00:00:00Z"
HASH = "a" * 64
OTHER = "b" * 64
V2 = RUN_CHECKPOINT_V2_SCHEMA


def stamped(schema: str, *, run_id: str = "run-1", **fields):
    return stamp_generated_artifact(
        {"schema_version": schema, "run_id": run_id, **fields},
        created_at=CREATED,
    )


def evidence(schema: str, *, run_id: str = "run-1"):
    fields = {
        "document_snapshot_file_sha256": "1" * 64,
        "discovery_logical_sha256": "2" * 64,
        "discovery_file_sha256": "3" * 64,
        "mention_proposals_file_sha256": "4" * 64,
        "production_write_performed": False,
        "items": [],
        "mentions": [],
    }
    if schema == "evidence-package-v2":
        fields["mention_proposals_logical_sha256"] = "5" * 64
    return stamped(schema, run_id=run_id, **fields)


def resolution(schema: str, *, run_id: str = "run-1"):
    fields = {
        "evidence_package_logical_sha256": "1" * 64,
        "evidence_package_file_sha256": "2" * 64,
        "identity_snapshot_file_sha256": "3" * 64,
        "production_write_performed": False,
        "photo_person_created": False,
        "resolutions": [],
    }
    if schema == "resolution-report-v1":
        fields["local_candidates_file_sha256"] = None
        fields["confidence_approves_identity"] = False
        fields["local_candidates_are_production_identity"] = False
    return stamped(schema, run_id=run_id, **fields)


def review(schema: str, *, run_id: str = "run-1"):
    fields = {
        "resolution_report_logical_sha256": "1" * 64,
        "resolution_report_file_sha256": "2" * 64,
        "kind": "IDENTITY_REVIEW_NOT_APPLY",
        "production_write_performed": False,
        "photo_person_created": False,
        "biography_fields_included": False,
        "review_rows": [],
    }
    if schema == "identity-review-package-v1":
        fields["auto_resolution_proposals"] = []
    else:
        fields["human_approval_performed"] = False
        fields["apply_authorized"] = False
    return stamped(schema, run_id=run_id, **fields)


def start(schema: str = RUN_CHECKPOINT_SCHEMA, *, run_id: str = "run-1"):
    return initial_checkpoint(
        run_id=run_id,
        step="discover",
        input_hashes={"document_text_snapshot": HASH},
        output_hashes={"discovery_selection": OTHER},
        checkpoint_schema=schema,
        created_at=CREATED,
    )


def advance(checkpoint, step, artifact, role, *, processed_items=None):
    artifact = dict(artifact)
    if artifact.get("run_id") != checkpoint["run_id"]:
        artifact = stamp_generated_artifact(
            {**artifact, "run_id": checkpoint["run_id"]},
            created_at=CREATED,
        )
    return append_step(
        checkpoint,
        step=step,
        input_hashes=dict(checkpoint["input_file_hashes"]),
        output_hashes={role: HASH},
        processed_items=processed_items,
        created_at=CREATED,
        checkpoint_schema=checkpoint["schema_version"],
        bound_artifacts={role: artifact},
    )


def complete_v2(*, run_id: str = "run-1", processed_items=None):
    current = start(V2, run_id=run_id)
    current = advance(
        current,
        "freeze-evidence",
        evidence("evidence-package-v2", run_id=run_id),
        "evidence_package",
        processed_items=processed_items,
    )
    current = advance(
        current,
        "resolve",
        resolution("resolution-report-v2", run_id=run_id),
        "resolution_report",
    )
    return advance(
        current,
        "review",
        review("identity-review-package-v2", run_id=run_id),
        "identity_review_package",
    )


class RunCheckpointV2Tests(unittest.TestCase):
    def test_v2_checkpoint_uses_v2_contracts_and_v1_step_names(self):
        checkpoint = start(V2)
        self.assertEqual(checkpoint["schema_version"], V2)
        versions = checkpoint["schema_versions"]
        self.assertEqual(versions["evidence_package"], "evidence-package-v2")
        self.assertEqual(versions["resolution_report"], "resolution-report-v2")
        self.assertEqual(
            versions["identity_review_package"], "identity-review-package-v2"
        )
        self.assertEqual(versions["run_checkpoint"], V2)
        self.assertEqual(versions["discovery_selection"], "discovery-selection-v1")
        self.assertNotIn("mention_proposals", versions)
        self.assertEqual(checkpoint["completed_steps"], ["discover"])
        self.assertEqual(
            RUN_STEPS, ("discover", "freeze-evidence", "resolve", "review")
        )

    def test_default_schema_map_stays_v1(self):
        self.assertEqual(
            published_schema_versions(),
            published_schema_versions(RUN_CHECKPOINT_SCHEMA),
        )
        self.assertEqual(
            published_schema_versions()["evidence_package"], "evidence-package-v1"
        )
        self.assertEqual(
            published_schema_versions()["identity_review_package"],
            "identity-review-package-v1",
        )
        v1 = start()
        self.assertEqual(v1["schema_version"], RUN_CHECKPOINT_SCHEMA)
        self.assertEqual(v1["schema_versions"], published_schema_versions())

    def test_each_generation_resumes_only_itself(self):
        v1 = start()
        resumed_v1 = append_step(
            v1,
            step="freeze-evidence",
            input_hashes=dict(v1["input_file_hashes"]),
            output_hashes={"evidence_package": HASH},
            created_at=CREATED,
        )
        self.assertEqual(resumed_v1["schema_version"], RUN_CHECKPOINT_SCHEMA)
        v2 = start(V2)
        resumed_v2 = advance(
            v2, "freeze-evidence", evidence("evidence-package-v2"), "evidence_package"
        )
        self.assertEqual(resumed_v2["schema_version"], V2)
        self.assertEqual(resumed_v2["completed_steps"], ["discover", "freeze-evidence"])

    def test_generation_mixing_is_rejected(self):
        v1 = start()
        v2 = start(V2)
        cases = [
            (
                v1,
                "freeze-evidence",
                evidence("evidence-package-v2"),
                "evidence_package",
                RUN_CHECKPOINT_SCHEMA,
            ),
            (
                v2,
                "freeze-evidence",
                evidence("evidence-package-v1"),
                "evidence_package",
                V2,
            ),
        ]
        frozen_v1 = append_step(
            v1,
            step="freeze-evidence",
            input_hashes=dict(v1["input_file_hashes"]),
            output_hashes={"evidence_package": OTHER},
            created_at=CREATED,
        )
        frozen_v2 = advance(
            v2, "freeze-evidence", evidence("evidence-package-v2"), "evidence_package"
        )
        cases.extend(
            [
                (
                    frozen_v1,
                    "resolve",
                    resolution("resolution-report-v2"),
                    "resolution_report",
                    RUN_CHECKPOINT_SCHEMA,
                ),
                (
                    frozen_v2,
                    "resolve",
                    resolution("resolution-report-v1"),
                    "resolution_report",
                    V2,
                ),
            ]
        )
        resolved_v1 = append_step(
            frozen_v1,
            step="resolve",
            input_hashes=dict(frozen_v1["input_file_hashes"]),
            output_hashes={"resolution_report": HASH},
            created_at=CREATED,
        )
        resolved_v2 = advance(
            frozen_v2,
            "resolve",
            resolution("resolution-report-v2"),
            "resolution_report",
        )
        cases.extend(
            [
                (
                    resolved_v1,
                    "review",
                    review("identity-review-package-v2"),
                    "identity_review_package",
                    RUN_CHECKPOINT_SCHEMA,
                ),
                (
                    resolved_v2,
                    "review",
                    review("identity-review-package-v1"),
                    "identity_review_package",
                    V2,
                ),
            ]
        )
        for prior, step, artifact, role, schema in cases:
            with self.subTest(
                step=step, schema=schema, artifact=artifact["schema_version"]
            ):
                with self.assertRaises(UnsupportedSchemaVersionError):
                    append_step(
                        prior,
                        step=step,
                        input_hashes=dict(prior["input_file_hashes"]),
                        output_hashes={role: HASH},
                        created_at=CREATED,
                        checkpoint_schema=schema,
                        bound_artifacts={role: artifact},
                    )
        with self.assertRaises(UnsupportedSchemaVersionError):
            append_step(
                v1,
                step="freeze-evidence",
                input_hashes=dict(v1["input_file_hashes"]),
                output_hashes={"evidence_package": HASH},
                checkpoint_schema=V2,
                bound_artifacts={"evidence_package": evidence("evidence-package-v2")},
            )
        self.assertEqual(v1["schema_version"], RUN_CHECKPOINT_SCHEMA)
        with self.assertRaises(UnsupportedSchemaVersionError):
            append_step(
                v2,
                step="freeze-evidence",
                input_hashes=dict(v2["input_file_hashes"]),
                output_hashes={"evidence_package": HASH},
                checkpoint_schema=RUN_CHECKPOINT_SCHEMA,
                bound_artifacts={"evidence_package": evidence("evidence-package-v1")},
            )
        self.assertEqual(v2["schema_version"], V2)

    def test_schema_map_and_unknown_checkpoint_schema_fail_closed(self):
        prior = start(V2)
        drifted = copy.deepcopy(prior)
        drifted["schema_versions"]["evidence_package"] = "evidence-package-v1"
        with self.assertRaises(UnsupportedSchemaVersionError):
            advance(
                drifted,
                "freeze-evidence",
                evidence("evidence-package-v2"),
                "evidence_package",
            )
        unknown = copy.deepcopy(prior)
        unknown["schema_version"] = "run-checkpoint-v9"
        with self.assertRaises(UnsupportedSchemaVersionError):
            append_step(
                unknown,
                step="freeze-evidence",
                input_hashes=dict(prior["input_file_hashes"]),
                output_hashes={"evidence_package": HASH},
                checkpoint_schema=V2,
                bound_artifacts={"evidence_package": evidence("evidence-package-v2")},
            )
        with self.assertRaises(UnsupportedSchemaVersionError):
            initial_checkpoint(
                run_id="run-1",
                step="discover",
                input_hashes={"document_text_snapshot": HASH},
                output_hashes={"discovery_selection": OTHER},
                checkpoint_schema="run-checkpoint-v9",
            )

    def test_completed_checkpoints_seed_a_new_discovery_run(self):
        seen = select_items(DOCUMENTS)["selected_items"]
        processed = [
            {
                "archive_item_id": row["archive_item_id"],
                "displayed_text_sha256": row["displayed_text_sha256"],
            }
            for row in seen
        ]
        v1 = start(run_id="run-previous")
        for step in ("freeze-evidence", "resolve", "review"):
            v1 = append_step(
                v1,
                step=step,
                input_hashes=dict(v1["input_file_hashes"]),
                output_hashes={},
                processed_items=processed if step == "freeze-evidence" else None,
                created_at=CREATED,
            )
        v2 = complete_v2(run_id="run-older", processed_items=processed)
        self.assertNotEqual(v1["run_id"], "run-new")
        self.assertNotEqual(v2["run_id"], "run-new")
        for checkpoint in (v1, v2):
            seeded = select_items(DOCUMENTS, checkpoint)
            self.assertEqual(seeded["selected_items"], [])
        partial_v2 = start(V2, run_id="run-older")
        for partial in (start(run_id="run-previous"), partial_v2):
            with self.assertRaises(IncompletePriorCheckpointError) as caught:
                select_items(DOCUMENTS, partial)
            self.assertEqual(caught.exception.error_code, "INCOMPLETE_PRIOR_CHECKPOINT")

    def test_v2_progression_rejects_skips_and_repeats(self):
        prior = start(V2)
        with self.assertRaises(ArtifactError):
            advance(
                prior,
                "resolve",
                resolution("resolution-report-v2"),
                "resolution_report",
            )
        with self.assertRaises(ArtifactError):
            advance(
                prior, "discover", evidence("evidence-package-v2"), "evidence_package"
            )
        frozen = advance(
            prior,
            "freeze-evidence",
            evidence("evidence-package-v2"),
            "evidence_package",
        )
        with self.assertRaises(ArtifactError):
            advance(
                frozen,
                "freeze-evidence",
                evidence("evidence-package-v2"),
                "evidence_package",
            )
        finished = complete_v2()
        self.assertEqual(list(finished["completed_steps"]), list(RUN_STEPS))
        encoded = json.dumps(finished)
        for marker in (
            "human_approval_performed",
            "apply_authorized",
            "production_write_performed",
            "APPROVED",
        ):
            self.assertNotIn(marker, encoded)
        self.assertEqual(
            finished["schema_versions"]["identity_review_package"],
            "identity-review-package-v2",
        )

    def test_cli_v2_checkpoint_is_explicit_and_atomic(self):
        documents = Path(__file__).parent / "fixtures" / "document_text_snapshot.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "documents.json"
            snapshot.write_bytes(documents.read_bytes())
            discover_out = root / "checkpoint.discover.json"
            stdout = StringIO()
            with redirect_stdout(stdout):
                created = main(
                    [
                        "checkpoint-v2",
                        "--run-id",
                        "run-1",
                        "--step",
                        "discover",
                        "--input",
                        f"document_text_snapshot={snapshot}",
                        "--record",
                        f"discovery_selection={snapshot}",
                        "--output",
                        str(discover_out),
                    ]
                )
            self.assertEqual(created, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["schema_version"], V2)
            self.assertEqual(payload["completed_steps"], ["discover"])
            published = json.loads(discover_out.read_text(encoding="utf-8"))
            self.assertEqual(
                published["schema_versions"]["evidence_package"], "evidence-package-v2"
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                exists = main(
                    [
                        "checkpoint-v2",
                        "--run-id",
                        "run-1",
                        "--step",
                        "discover",
                        "--input",
                        f"document_text_snapshot={snapshot}",
                        "--output",
                        str(discover_out),
                    ]
                )
            self.assertEqual(exists, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"], "ARTIFACT_EXISTS"
            )
            self.assertEqual(
                json.loads(discover_out.read_text(encoding="utf-8"))["schema_version"],
                V2,
            )

            evidence_path = root / "evidence-v1.json"
            write_json_atomic(evidence_path, evidence("evidence-package-v1"))
            mixed_out = root / "mixed.json"
            stderr = StringIO()
            with redirect_stderr(stderr):
                mixed = main(
                    [
                        "checkpoint-v2",
                        "--run-id",
                        "run-1",
                        "--step",
                        "freeze-evidence",
                        "--prior",
                        str(discover_out),
                        "--input",
                        f"document_text_snapshot={snapshot}",
                        "--record",
                        f"evidence_package={evidence_path}",
                        "--output",
                        str(mixed_out),
                    ]
                )
            self.assertEqual(mixed, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"],
                "UNSUPPORTED_SCHEMA_VERSION",
            )
            self.assertFalse(mixed_out.exists())
            self.assertEqual(list(root.glob("*.tmp")), [])
            self.assertEqual(list(root.glob(".*.tmp")), [])

            malformed_path = root / "evidence-bad.json"
            malformed_path.write_text(
                json.dumps(
                    {"schema_version": "evidence-package-v2", "run_id": "run-1"}
                ),
                encoding="utf-8",
            )
            bad_out = root / "bad.json"
            stderr = StringIO()
            with redirect_stderr(stderr):
                bad = main(
                    [
                        "checkpoint-v2",
                        "--run-id",
                        "run-1",
                        "--step",
                        "freeze-evidence",
                        "--prior",
                        str(discover_out),
                        "--input",
                        f"document_text_snapshot={snapshot}",
                        "--record",
                        f"evidence_package={malformed_path}",
                        "--output",
                        str(bad_out),
                    ]
                )
            self.assertEqual(bad, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"], "MALFORMED_ARTIFACT"
            )
            self.assertFalse(bad_out.exists())

            stdout = StringIO()
            with redirect_stdout(stdout):
                v1_created = main(
                    [
                        "checkpoint",
                        "--run-id",
                        "run-1",
                        "--step",
                        "discover",
                        "--input",
                        f"document_text_snapshot={snapshot}",
                        "--output",
                        str(root / "checkpoint-v1.json"),
                    ]
                )
            self.assertEqual(v1_created, 0)
            self.assertEqual(
                json.loads(stdout.getvalue())["schema_version"], RUN_CHECKPOINT_SCHEMA
            )

            evidence_v2 = root / "evidence-v2.json"
            write_json_atomic(evidence_v2, evidence("evidence-package-v2"))
            rejected_out = root / "v1-mixed.json"
            stderr = StringIO()
            v1_discover = root / "checkpoint-v1.json"
            with redirect_stderr(stderr):
                rejected = main(
                    [
                        "checkpoint",
                        "--run-id",
                        "run-1",
                        "--step",
                        "freeze-evidence",
                        "--prior",
                        str(v1_discover),
                        "--input",
                        f"document_text_snapshot={snapshot}",
                        "--record",
                        f"evidence_package={evidence_v2}",
                        "--output",
                        str(rejected_out),
                    ]
                )
            self.assertEqual(rejected, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"],
                "UNSUPPORTED_SCHEMA_VERSION",
            )
            self.assertFalse(rejected_out.exists())

            finished = complete_v2(
                run_id="run-older",
                processed_items=[
                    {
                        "archive_item_id": row["archive_item_id"],
                        "displayed_text_sha256": row["displayed_text_sha256"],
                    }
                    for row in select_items(DOCUMENTS)["selected_items"]
                ],
            )
            prior_path = root / "prior-v2.json"
            write_json_atomic(prior_path, finished)
            discovery_out = root / "discovery.json"
            stdout = StringIO()
            with redirect_stdout(stdout):
                discovered = main(
                    [
                        "discover",
                        "--documents",
                        str(documents),
                        "--run-id",
                        "run-new",
                        "--prior-checkpoint",
                        str(prior_path),
                        "--output",
                        str(discovery_out),
                    ]
                )
            self.assertEqual(discovered, 0)
            selection = json.loads(discovery_out.read_text(encoding="utf-8"))
            self.assertEqual(selection["run_id"], "run-new")
            self.assertEqual(selection["schema_version"], "discovery-selection-v1")
            self.assertEqual(selection["selected_items"], [])
            self.assertNotEqual(
                json.loads(prior_path.read_text(encoding="utf-8"))["run_id"], "run-new"
            )

    def test_v2_processed_from_must_be_the_recorded_evidence_file(self):
        def package(item_id: int):
            body = evidence("evidence-package-v2")
            body["items"] = [
                {
                    "archive_item_id": item_id,
                    "item_type": "MANUAL_TEXT",
                    "displayed_text_sha256": f"{item_id:064x}",
                    "output_metadata_only": {"title": "Item"},
                    "texts": [],
                }
            ]
            return stamp_generated_artifact(body, created_at=CREATED)

        documents = Path(__file__).parent / "fixtures" / "document_text_snapshot.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / "documents.json"
            snapshot.write_bytes(documents.read_bytes())
            discover_out = root / "checkpoint.discover.json"
            self.assertEqual(
                main(
                    [
                        "checkpoint-v2",
                        "--run-id",
                        "run-1",
                        "--step",
                        "discover",
                        "--input",
                        f"document_text_snapshot={snapshot}",
                        "--output",
                        str(discover_out),
                    ]
                ),
                0,
            )
            evidence_a = root / "evidence-a.json"
            evidence_b = root / "evidence-b.json"
            write_json_atomic(evidence_a, package(10))
            write_json_atomic(evidence_b, package(11))
            self.assertNotEqual(sha256_file(evidence_a), sha256_file(evidence_b))
            mismatched = root / "mismatched.json"
            stderr = StringIO()
            with redirect_stderr(stderr):
                rejected = main(
                    [
                        "checkpoint-v2",
                        "--run-id",
                        "run-1",
                        "--step",
                        "freeze-evidence",
                        "--prior",
                        str(discover_out),
                        "--input",
                        f"document_text_snapshot={snapshot}",
                        "--record",
                        f"evidence_package={evidence_a}",
                        "--processed-from",
                        str(evidence_b),
                        "--output",
                        str(mismatched),
                    ]
                )
            self.assertEqual(rejected, 2)
            blocked = json.loads(stderr.getvalue())
            self.assertEqual(blocked["error_code"], "INPUT_HASH_CHANGED")
            self.assertIn("processed-from", blocked["reason"])
            self.assertFalse(mismatched.exists())
            self.assertEqual(list(root.glob("*.tmp")), [])
            self.assertEqual(list(root.glob(".*.tmp")), [])

            matched = root / "matched.json"
            stdout = StringIO()
            with redirect_stdout(stdout):
                accepted = main(
                    [
                        "checkpoint-v2",
                        "--run-id",
                        "run-1",
                        "--step",
                        "freeze-evidence",
                        "--prior",
                        str(discover_out),
                        "--input",
                        f"document_text_snapshot={snapshot}",
                        "--record",
                        f"evidence_package={evidence_a}",
                        "--processed-from",
                        str(evidence_a),
                        "--output",
                        str(matched),
                    ]
                )
            self.assertEqual(accepted, 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "OK")
            published = json.loads(matched.read_text(encoding="utf-8"))
            self.assertEqual(
                published["output_file_hashes"]["evidence_package"],
                sha256_file(evidence_a),
            )
            self.assertEqual(
                published["processed_items"],
                [
                    {
                        "archive_item_id": 10,
                        "displayed_text_sha256": f"{10:064x}",
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
