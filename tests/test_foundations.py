import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from vs_archive_people import __version__
from vs_archive_people.artifacts.io import (
    ArtifactError,
    ArtifactReadError,
    LogicalHashMismatchError,
    RunIdMismatchError,
    read_json,
    sha256_file,
    sha256_json,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import (
    logical_body,
    stamp_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import (
    SCHEMA_FILES,
    index_schema_documents,
    iter_schema_documents,
    load_schema,
    require_valid_schema,
    schema_resources,
    step_chain,
)
from vs_archive_people.cli import main
from vs_archive_people.evidence.mentions import mention_fingerprint
from vs_archive_people.evidence.package import build_evidence_package
from vs_archive_people.resolution.match import build_resolution_report
from vs_archive_people.resolution.snapshot_index import build_index
from vs_archive_people.runs.checkpoint import (
    UnsupportedSchemaVersionError,
    append_step,
    initial_checkpoint,
)
from test_mention_evidence import (
    DISCOVERY_HASH,
    DOCUMENT_HASH,
    DOCUMENTS,
    PROPOSALS,
    discovery_for,
)

FIXTURES = Path(__file__).parent / "fixtures"
IDENTITY = json.loads((FIXTURES / "identity_snapshot.json").read_text(encoding="utf-8"))
CREATED = "2026-01-01T00:00:00Z"
LATER = "2026-01-02T00:00:00Z"


def cloned_identity():
    return json.loads(json.dumps(IDENTITY))


class FoundationTests(unittest.TestCase):
    def test_schemas_load_from_package_resources(self):
        packaged = (
            schema_resources()
            .joinpath("run_checkpoint.v1.json")
            .read_text(encoding="utf-8")
        )
        self.assertIn("run-checkpoint-v1", packaged)
        previous = os.getcwd()
        os.chdir("/")
        try:
            schema = load_schema("evidence-package-v1")
        finally:
            os.chdir(previous)
        self.assertEqual(
            schema["properties"]["schema_version"]["const"], "evidence-package-v1"
        )

    def test_every_shipped_schema_is_draft_2020_12(self):
        documents = iter_schema_documents()
        self.assertGreaterEqual(len(documents), 8)
        for schema_id, document in documents:
            require_valid_schema(document, schema_id)
        with self.assertRaises(ArtifactError) as caught:
            require_valid_schema({"type": "not-a-json-schema-type"}, "broken.json")
        self.assertIn("Draft 2020-12", str(caught.exception))

    def test_malformed_artifact_names_the_failing_path(self):
        with self.assertRaises(ArtifactError) as caught:
            from vs_archive_people.artifacts.schema_validate import validate_artifact

            validate_artifact(
                {
                    "schema_version": "document-text-snapshot-v1",
                    "exported_at": "2026-01-01T00:00:00+00:00",
                }
            )
        self.assertIn("$", str(caught.exception))

    def test_version_constant_matches_pyproject(self):
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'version = "{__version__}"', text)

    def test_fingerprint_uses_source_offsets_and_ignores_caller_ids(self):
        source = "ocr_document.displayed_transcription.text"
        first = mention_fingerprint(
            archive_item_id=10,
            evidence_source=source,
            source_text_sha256="AB" * 32,
            start_offset=0,
            end_offset=12,
        )
        second = mention_fingerprint(
            archive_item_id=10,
            evidence_source=source,
            source_text_sha256="ab" * 32,
            start_offset=0,
            end_offset=12,
        )
        self.assertEqual(first, second)
        moved = mention_fingerprint(
            archive_item_id=10,
            evidence_source=source,
            source_text_sha256="ab" * 32,
            start_offset=13,
            end_offset=18,
        )
        self.assertNotEqual(first, moved)
        wrote = {
            "archive_item_id": 10,
            "evidence_source": source,
            "start_offset": 13,
            "end_offset": 18,
            "exact_mention": "wrote",
            "evidence_snippet": "Ada Lovelace wrote",
            "occurrence_index": 9,
            "mention_fingerprint": "ab" * 32,
            "mention_key": "B15-M0001",
        }
        ada = dict(PROPOSALS[1])
        alone = build_evidence_package(
            DOCUMENTS,
            discovery_for(DOCUMENTS),
            [wrote],
            run_id="run-1",
            document_snapshot_file_sha256=DOCUMENT_HASH,
            discovery_file_sha256=DISCOVERY_HASH,
            mention_proposals_file_sha256="1" * 64,
            created_at=CREATED,
        )
        with_earlier = build_evidence_package(
            DOCUMENTS,
            discovery_for(DOCUMENTS),
            [ada, wrote],
            run_id="run-1",
            document_snapshot_file_sha256=DOCUMENT_HASH,
            discovery_file_sha256=DISCOVERY_HASH,
            mention_proposals_file_sha256="1" * 64,
            created_at=CREATED,
        )
        wrote_alone = alone["mentions"][0]
        wrote_after = next(
            row for row in with_earlier["mentions"] if row["exact_mention"] == "wrote"
        )
        self.assertEqual(
            wrote_alone["mention_fingerprint"], wrote_after["mention_fingerprint"]
        )
        self.assertNotEqual(wrote_alone["mention_fingerprint"], "ab" * 32)
        self.assertEqual(wrote_alone["occurrence_index"], 0)
        self.assertEqual(wrote_after["occurrence_index"], 1)
        self.assertNotEqual(wrote_alone["mention_key"], "B15-M0001")
        self.assertEqual(
            wrote_alone["mention_fingerprint"],
            mention_fingerprint(
                archive_item_id=10,
                evidence_source=source,
                source_text_sha256=wrote_alone["source_text_sha256"],
                start_offset=13,
                end_offset=18,
            ),
        )

    def test_incorrect_source_surface_fails(self):
        bad = dict(PROPOSALS[1])
        # "Ada Lovelace" stays in the snippet and the body. These offsets
        # select "wrote" instead, so only the source-surface check fails.
        bad["start_offset"] = 13
        bad["end_offset"] = 18
        with self.assertRaises(ArtifactError) as caught:
            build_evidence_package(
                DOCUMENTS,
                discovery_for(DOCUMENTS),
                [bad],
                run_id="run-1",
                document_snapshot_file_sha256=DOCUMENT_HASH,
                discovery_file_sha256=DISCOVERY_HASH,
                mention_proposals_file_sha256="1" * 64,
                created_at=CREATED,
            )
        self.assertIn("supplied offsets", str(caught.exception))

    def test_discovery_from_another_run_is_rejected(self):
        with self.assertRaises(RunIdMismatchError) as caught:
            build_evidence_package(
                DOCUMENTS,
                discovery_for(DOCUMENTS),
                PROPOSALS,
                run_id="run-other",
                document_snapshot_file_sha256=DOCUMENT_HASH,
                discovery_file_sha256=DISCOVERY_HASH,
                mention_proposals_file_sha256="1" * 64,
                created_at=CREATED,
            )
        self.assertEqual(caught.exception.error_code, "RUN_ID_MISMATCH")

    def test_duplicate_fingerprint_is_rejected(self):
        proposals = [dict(PROPOSALS[0]), dict(PROPOSALS[0])]
        with self.assertRaises(ArtifactError) as caught:
            build_evidence_package(
                DOCUMENTS,
                discovery_for(DOCUMENTS),
                proposals,
                run_id="run-1",
                document_snapshot_file_sha256=DOCUMENT_HASH,
                discovery_file_sha256=DISCOVERY_HASH,
                mention_proposals_file_sha256="1" * 64,
                created_at=CREATED,
            )
        self.assertIn("mention_fingerprint", str(caught.exception))

    def test_proposal_order_does_not_change_the_logical_hash(self):
        selection = discovery_for(DOCUMENTS)
        forward = build_evidence_package(
            DOCUMENTS,
            selection,
            list(PROPOSALS),
            run_id="run-1",
            document_snapshot_file_sha256=DOCUMENT_HASH,
            discovery_file_sha256=DISCOVERY_HASH,
            mention_proposals_file_sha256="1" * 64,
            created_at=CREATED,
        )
        backward = build_evidence_package(
            DOCUMENTS,
            selection,
            list(reversed(PROPOSALS)),
            run_id="run-1",
            document_snapshot_file_sha256=DOCUMENT_HASH,
            discovery_file_sha256=DISCOVERY_HASH,
            mention_proposals_file_sha256="1" * 64,
            created_at=LATER,
        )
        self.assertEqual(
            forward["logical_content_sha256"], backward["logical_content_sha256"]
        )
        self.assertNotEqual(
            forward["artifact_created_at"], backward["artifact_created_at"]
        )
        self.assertEqual(
            forward["logical_content_sha256"], sha256_json(logical_body(forward))
        )

    def test_logical_hash_and_physical_file_hash_differ_when_only_time_changes(self):
        selection = discovery_for(DOCUMENTS)
        kwargs = {
            "run_id": "run-1",
            "document_snapshot_file_sha256": DOCUMENT_HASH,
            "discovery_file_sha256": DISCOVERY_HASH,
            "mention_proposals_file_sha256": "1" * 64,
        }
        first = build_evidence_package(
            DOCUMENTS, selection, PROPOSALS, created_at=CREATED, **kwargs
        )
        second = build_evidence_package(
            DOCUMENTS, selection, PROPOSALS, created_at=LATER, **kwargs
        )
        self.assertEqual(
            first["logical_content_sha256"], second["logical_content_sha256"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.json"
            second_path = Path(tmp) / "second.json"
            first_file = write_json_atomic(first_path, first)
            second_file = write_json_atomic(second_path, second)
            self.assertEqual(first_file, sha256_file(first_path))
            self.assertNotEqual(first_file, second_file)

    def test_parent_file_hash_does_not_change_child_logical_hash(self):
        early = discovery_for(DOCUMENTS)
        late = json.loads(json.dumps(early))
        stamp_generated_artifact(late, created_at=LATER)
        self.assertEqual(
            early["logical_content_sha256"], late["logical_content_sha256"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            early_path = Path(tmp) / "early.json"
            late_path = Path(tmp) / "late.json"
            early_file = write_json_atomic(early_path, early)
            late_file = write_json_atomic(late_path, late)
            self.assertNotEqual(early_file, late_file)
        shared = {
            "run_id": "run-1",
            "document_snapshot_file_sha256": DOCUMENT_HASH,
            "mention_proposals_file_sha256": "1" * 64,
            "created_at": CREATED,
        }
        from_early = build_evidence_package(
            DOCUMENTS, early, PROPOSALS, discovery_file_sha256=early_file, **shared
        )
        from_late = build_evidence_package(
            DOCUMENTS, late, PROPOSALS, discovery_file_sha256=late_file, **shared
        )
        self.assertEqual(
            from_early["logical_content_sha256"], from_late["logical_content_sha256"]
        )
        self.assertEqual(
            from_early["discovery_logical_sha256"],
            early["logical_content_sha256"],
        )
        self.assertNotEqual(
            from_early["discovery_file_sha256"], from_late["discovery_file_sha256"]
        )

    def test_tampered_logical_hash_fails_on_consumption(self):
        package = build_evidence_package(
            DOCUMENTS,
            discovery_for(DOCUMENTS),
            PROPOSALS,
            run_id="run-1",
            document_snapshot_file_sha256=DOCUMENT_HASH,
            discovery_file_sha256=DISCOVERY_HASH,
            mention_proposals_file_sha256="1" * 64,
            created_at=CREATED,
        )
        package["run_id"] = "tampered"
        with self.assertRaises(LogicalHashMismatchError) as caught:
            build_resolution_report(
                package,
                build_index(IDENTITY),
                evidence_package_file_sha256="d" * 64,
                identity_snapshot_file_sha256="e" * 64,
                created_at=CREATED,
            )
        self.assertEqual(caught.exception.error_code, "LOGICAL_HASH_MISMATCH")

    def test_impossible_timestamp_is_rejected(self):
        with self.assertRaises(ArtifactError):
            stamp_generated_artifact({}, created_at="2026-02-31T00:00:00Z")

    def test_identity_invariants_fail_closed(self):
        duplicate_person = cloned_identity()
        duplicate_person["models"]["Person"]["rows"].append(
            dict(duplicate_person["models"]["Person"]["rows"][0])
        )
        with self.assertRaises(ArtifactError) as caught:
            build_index(duplicate_person)
        self.assertIn("duplicate Person id", str(caught.exception))

        duplicate_alias = cloned_identity()
        duplicate_alias["models"]["PersonAlias"]["rows"].append(
            dict(duplicate_alias["models"]["PersonAlias"]["rows"][0])
        )
        with self.assertRaises(ArtifactError) as caught:
            build_index(duplicate_alias)
        self.assertIn("duplicate PersonAlias id", str(caught.exception))

        dangling_alias = cloned_identity()
        dangling_alias["models"]["PersonAlias"]["rows"][0]["person_id"] = 999
        with self.assertRaises(ArtifactError) as caught:
            build_index(dangling_alias)
        self.assertIn("not in the production index", str(caught.exception))

        dangling_binding = cloned_identity()
        dangling_binding["models"]["PersonRegistryImportBinding"]["rows"][0][
            "person_id"
        ] = 999
        with self.assertRaises(ArtifactError) as caught:
            build_index(dangling_binding)
        self.assertIn("not in the production index", str(caught.exception))

        conflicting_key = cloned_identity()
        conflicting_key["models"]["PersonRegistryImportBinding"]["rows"].append(
            {"id": 99, "person_id": 2, "source": "fixture", "stable_key": "C:1"}
        )
        with self.assertRaises(ArtifactError) as caught:
            build_index(conflicting_key)
        self.assertIn("stable_key", str(caught.exception))

        blank = cloned_identity()
        blank["models"]["Person"]["rows"][0]["name"] = "   "
        with self.assertRaises(ArtifactError):
            build_index(blank)

    def test_shared_forms_and_stable_keys_have_defined_results(self):
        snapshot = cloned_identity()
        snapshot["models"]["PersonAlias"]["rows"].append(
            {"id": 99, "person_id": 1, "name": "Ada Lovelace", "kind": "birth_name"}
        )
        snapshot["models"]["PersonRegistryImportBinding"]["rows"].append(
            {"id": 23, "person_id": 1, "source": "fixture", "stable_key": "C:9"}
        )
        index = build_index(snapshot)
        self.assertEqual(index.people[1].stable_keys, ("C:1", "C:9"))
        self.assertEqual(index.alias_id(1, "Ada Lovelace"), 99)
        report = build_resolution_report(
            _evidence([_mention("M0001", "Ada Lovelace")]),
            index,
            evidence_package_file_sha256="d" * 64,
            identity_snapshot_file_sha256="e" * 64,
            created_at=CREATED,
        )
        row = report["resolutions"][0]
        self.assertEqual(row["matched_via"], "canonical_name")
        self.assertEqual(row["matched_person_ids"], [1])
        shared = build_resolution_report(
            _evidence([_mention("M0001", "Dana")]),
            build_index(IDENTITY),
            evidence_package_file_sha256="d" * 64,
            identity_snapshot_file_sha256="e" * 64,
            created_at=CREATED,
        )
        self.assertEqual(shared["resolutions"][0]["classification"], "AMBIGUOUS")
        self.assertEqual(shared["resolutions"][0]["matched_person_ids"], [3, 4])

    def test_identity_row_order_does_not_change_lookup(self):
        reversed_snapshot = cloned_identity()
        for model in reversed_snapshot["models"].values():
            model["rows"].reverse()
        original = build_index(IDENTITY)
        flipped = build_index(reversed_snapshot)
        self.assertEqual(original.lookup("Dana"), flipped.lookup("Dana"))
        self.assertEqual(original.people[1].stable_keys, flipped.people[1].stable_keys)
        self.assertEqual(
            original.lookup("Ada Lovelace"), flipped.lookup("Ada Lovelace")
        )

    def test_old_checkpoint_schema_is_unsupported_not_malformed(self):
        prior = initial_checkpoint(
            run_id="run-1",
            step="discover",
            input_hashes={"document_text_snapshot": "a" * 64},
            output_hashes={"discovery_selection": "b" * 64},
            created_at=CREATED,
        )
        old = json.loads(json.dumps(prior))
        old["schema_version"] = "run-checkpoint-v0"
        with self.assertRaises(UnsupportedSchemaVersionError) as caught:
            append_step(
                old,
                step="freeze-evidence",
                input_hashes={"document_text_snapshot": "a" * 64},
                output_hashes={"evidence_package": "c" * 64},
            )
        self.assertEqual(caught.exception.error_code, "UNSUPPORTED_SCHEMA_VERSION")

    def test_cli_blocks_a_contract_failure_without_a_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            documents = Path(tmp) / "documents.json"
            output = Path(tmp) / "discovery.json"
            documents.write_text(
                json.dumps(
                    {
                        "schema_version": "document-text-snapshot-v1",
                        "exported_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "discover",
                        "--documents",
                        str(documents),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 2)
            payload = json.loads(stderr.getvalue())
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertEqual(payload["error_code"], "MALFORMED_ARTIFACT")
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])
            with self.assertRaises(ArtifactError):
                write_json_atomic(
                    output,
                    {"schema_version": "document-text-snapshot-v1"},
                )
            self.assertFalse(output.exists())

    def test_publication_refuses_overwrite_even_if_exists_reports_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.json"
            write_json_atomic(path, {"ok": True})
            original = path.read_bytes()
            with patch.object(Path, "exists", return_value=False):
                with self.assertRaises(ArtifactError) as caught:
                    write_json_atomic(path, {"ok": False})
            self.assertEqual(caught.exception.error_code, "ARTIFACT_EXISTS")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_duplicate_cli_binding_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.json"
            second = Path(tmp) / "b.json"
            output = Path(tmp) / "checkpoint.json"
            first.write_text("{}", encoding="utf-8")
            second.write_text("{}", encoding="utf-8")
            cases = (
                ["--input", f"snapshot={first}", "--input", f"snapshot={second}"],
                ["--record", f"discovery={first}", "--record", f"discovery={second}"],
            )
            for extra in cases:
                with self.subTest(extra=extra[0]):
                    stderr = StringIO()
                    with redirect_stderr(stderr):
                        code = main(
                            [
                                "checkpoint",
                                "--run-id",
                                "run-1",
                                "--step",
                                "discover",
                                *extra,
                                "--output",
                                str(output),
                            ]
                        )
                    self.assertEqual(code, 2)
                    payload = json.loads(stderr.getvalue())
                    self.assertEqual(payload["status"], "BLOCKED")
                    self.assertEqual(payload["error_code"], "MALFORMED_ARTIFACT")
                    self.assertIn("duplicate binding name", payload["reason"])
                    self.assertFalse(output.exists())

    def test_schema_index_rejects_duplicate_and_mismatched_maps(self):
        files = {
            resource.name: json.loads(resource.read_text(encoding="utf-8"))
            for resource in schema_resources().iterdir()
            if resource.name.endswith(".json")
        }
        self.assertEqual(set(SCHEMA_FILES.values()) - set(files), set())
        duplicate = json.loads(json.dumps(files["producer.v1.json"]))
        files["producer-copy.v1.json"] = duplicate
        with self.assertRaises(ArtifactError) as caught:
            index_schema_documents(files)
        self.assertIn("duplicate schema $id", str(caught.exception))
        del files["producer-copy.v1.json"]

        missing = dict(files)
        del missing["run_checkpoint.v1.json"]
        with self.assertRaises(ArtifactError) as missing_caught:
            index_schema_documents(missing)
        self.assertIn("run_checkpoint.v1.json", str(missing_caught.exception))
        self.assertIn("missing", str(missing_caught.exception))

        mismatched = json.loads(json.dumps(files))
        mismatched["run_checkpoint.v1.json"]["properties"]["schema_version"][
            "const"
        ] = "run-checkpoint-v0"
        with self.assertRaises(ArtifactError) as mismatch_caught:
            index_schema_documents(mismatched)
        self.assertIn("run-checkpoint-v1", str(mismatch_caught.exception))
        self.assertIn("run_checkpoint.v1.json", str(mismatch_caught.exception))

    def test_step_order_is_owned_by_python_not_schema_enum_order(self):
        enum = load_schema("run-checkpoint-v1")["properties"]["completed_steps"][
            "items"
        ]["enum"]
        self.assertEqual(
            step_chain(),
            ("discover", "freeze-evidence", "resolve", "review"),
        )
        self.assertEqual(set(enum), set(step_chain()))

    def test_cli_malformed_json_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            documents = Path(tmp) / "documents.json"
            output = Path(tmp) / "discovery.json"
            documents.write_text("{", encoding="utf-8")
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "discover",
                        "--documents",
                        str(documents),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 2)
            payload = json.loads(stderr.getvalue())
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertEqual(payload["error_code"], "MALFORMED_ARTIFACT")
            self.assertIn("not valid JSON", payload["reason"])
            self.assertFalse(output.exists())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_missing_input_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "discovery.json"
            missing = Path(tmp) / "missing.json"
            stderr = StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "discover",
                        "--documents",
                        str(missing),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 2)
            payload = json.loads(stderr.getvalue())
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertEqual(payload["error_code"], "ARTIFACT_READ_ERROR")
            self.assertFalse(output.exists())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_read_json_rejects_invalid_text_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_bytes(b"\xff\xfe")
            with self.assertRaises(ArtifactReadError) as caught:
                read_json(path)
            self.assertEqual(caught.exception.error_code, "ARTIFACT_READ_ERROR")
            self.assertIn("UTF-8", str(caught.exception))

    def test_stamp_records_producer_without_a_git_commit(self):
        stamped = stamp_generated_artifact(
            {"schema_version": "discovery-selection-v1"}, created_at=CREATED
        )
        self.assertEqual(
            stamped["producer"], {"name": "vs-archive-people", "version": __version__}
        )
        self.assertNotIn("git_commit", stamped["producer"])
        self.assertEqual(stamped["artifact_created_at"], CREATED)


def _mention(key, exact):
    return {
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


def _evidence(mentions):
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
        created_at=CREATED,
    )


if __name__ == "__main__":
    unittest.main()
