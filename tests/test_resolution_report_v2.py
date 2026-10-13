import ast
import inspect
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vs_archive_people.artifacts.io import (
    RESOLUTION_REPORT_SCHEMA,
    RESOLUTION_REPORT_V2_SCHEMA,
    ArtifactError,
    LogicalHashMismatchError,
    RunIdMismatchError,
    UnsupportedSchemaVersionError,
    sha256_file,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import stamp_generated_artifact
from vs_archive_people.artifacts.schema_validate import RUN_STEPS, load_schema
from vs_archive_people.cli import main
from vs_archive_people.resolution.match import (
    CLASS_AMBIGUOUS,
    CLASS_DETERMINISTIC,
    CLASS_NEW,
    CLASS_NOT_PERSON,
    build_resolution_report,
)
from vs_archive_people.resolution.match_v2 import (
    CARRIED_FIELDS,
    build_resolution_report_v2,
)
from vs_archive_people.resolution.snapshot_index import IdentityIndex, build_index

IDENTITY = json.loads(
    (Path(__file__).parent / "fixtures" / "identity_snapshot.json").read_text(
        encoding="utf-8"
    )
)
CREATED = "2026-01-01T00:00:00Z"
SOURCE = "ocr_document.displayed_transcription.text"


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
                    "source_text_sha256": "a" * 64,
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


def report_for(mentions, index=None, *, run_id="run-1", evidence_hash="1" * 64):
    return build_resolution_report_v2(
        evidence(mentions, run_id=run_id),
        build_index(IDENTITY) if index is None else index,
        run_id=run_id,
        evidence_package_file_sha256=evidence_hash,
        identity_snapshot_file_sha256="2" * 64,
        created_at=CREATED,
    )


class RecordingIndex:
    def __init__(self, index: IdentityIndex):
        self._index = index
        self.lookups: list[str] = []

    @property
    def people(self):
        return self._index.people

    @property
    def photo_person_rows_ignored(self):
        return self._index.photo_person_rows_ignored

    def lookup(self, exact_mention: str):
        self.lookups.append(exact_mention)
        return self._index.lookup(exact_mention)

    def alias_id(self, person_id: int, exact_mention: str):
        return self._index.alias_id(person_id, exact_mention)


def identity_with_canonical(name: str, person_id: int = 7):
    snapshot = json.loads(json.dumps(IDENTITY))
    snapshot["models"]["Person"]["rows"].append(
        {"id": person_id, "name": name, "honorific": "", "biography": ""}
    )
    return snapshot


class ResolutionReportV2Tests(unittest.TestCase):
    def test_valid_evidence_resolves_and_binds_parents(self):
        package = evidence([mention("M0001", "Ada Lovelace")])
        report = build_resolution_report_v2(
            package,
            build_index(IDENTITY),
            run_id="run-1",
            evidence_package_file_sha256="ab" * 32,
            identity_snapshot_file_sha256="cd" * 32,
            created_at=CREATED,
        )
        self.assertEqual(report["schema_version"], RESOLUTION_REPORT_V2_SCHEMA)
        self.assertEqual(
            report["evidence_package_logical_sha256"], package["logical_content_sha256"]
        )
        self.assertEqual(report["evidence_package_file_sha256"], "ab" * 32)
        self.assertEqual(report["identity_snapshot_file_sha256"], "cd" * 32)
        self.assertFalse(report["production_write_performed"])
        self.assertFalse(report["photo_person_created"])
        self.assertNotIn("local_candidates_file_sha256", report)
        self.assertEqual(
            report["resolutions"][0]["classification"], CLASS_DETERMINISTIC
        )
        self.assertEqual(report["resolutions"][0]["matched_person_ids"], [1])
        self.assertEqual(
            report["resolutions"][0]["matched_person_name"], "Ada Lovelace"
        )
        self.assertEqual(report["resolutions"][0]["matched_via"], "canonical_name")
        self.assertEqual(report["resolutions"][0]["stable_keys"], ["C:1"])

    def test_file_hashes_are_audit_only(self):
        mentions = [mention("M0001", "Ada Lovelace")]
        first = report_for(mentions, evidence_hash="11" * 32)
        second = build_resolution_report_v2(
            evidence(mentions),
            build_index(IDENTITY),
            run_id="run-1",
            evidence_package_file_sha256="22" * 32,
            identity_snapshot_file_sha256="33" * 32,
            created_at=CREATED,
        )
        self.assertEqual(first["evidence_package_file_sha256"], "11" * 32)
        self.assertEqual(second["identity_snapshot_file_sha256"], "33" * 32)
        self.assertEqual(
            first["logical_content_sha256"], second["logical_content_sha256"]
        )

    def test_malformed_evidence_is_rejected(self):
        package = evidence([mention("M0001", "Ada Lovelace")])
        del package["mentions"][0]["name_surface"]
        with self.assertRaises(ArtifactError) as caught:
            build_resolution_report_v2(
                package,
                build_index(IDENTITY),
                run_id="run-1",
                evidence_package_file_sha256="1" * 64,
                identity_snapshot_file_sha256="2" * 64,
            )
        self.assertEqual(caught.exception.error_code, "MALFORMED_ARTIFACT")

    def test_wrong_schema_is_rejected(self):
        with self.assertRaises(UnsupportedSchemaVersionError) as caught:
            build_resolution_report_v2(
                v1_evidence(),
                build_index(IDENTITY),
                run_id="run-1",
                evidence_package_file_sha256="1" * 64,
                identity_snapshot_file_sha256="2" * 64,
            )
        self.assertIn("evidence-package-v1", str(caught.exception))
        self.assertEqual(caught.exception.error_code, "UNSUPPORTED_SCHEMA_VERSION")

    def test_logical_hash_mismatch_is_rejected(self):
        package = evidence([mention("M0001", "Ada Lovelace")])
        package["logical_content_sha256"] = "f" * 64
        with self.assertRaises(LogicalHashMismatchError):
            build_resolution_report_v2(
                package,
                build_index(IDENTITY),
                run_id="run-1",
                evidence_package_file_sha256="1" * 64,
                identity_snapshot_file_sha256="2" * 64,
            )

    def test_run_id_mismatch_is_rejected(self):
        with self.assertRaises(RunIdMismatchError):
            build_resolution_report_v2(
                evidence([mention("M0001", "Ada Lovelace")], run_id="run-1"),
                build_index(IDENTITY),
                run_id="run-other",
                evidence_package_file_sha256="1" * 64,
                identity_snapshot_file_sha256="2" * 64,
            )

    def test_titled_outer_resolves_the_name_surface_only(self):
        recorder = RecordingIndex(build_index(IDENTITY))
        row = mention(
            "M0001",
            "Colonel Dana",
            name="Dana",
            title_surface="Colonel",
            title_start_offset=0,
            title_end_offset=len("Colonel"),
            name_start_offset=len("Colonel "),
            name_end_offset=len("Colonel Dana"),
            surface_kind="TITLE_AND_NAME",
        )
        built = report_for([row], recorder)
        resolution = built["resolutions"][0]
        self.assertEqual(recorder.lookups, ["Dana"])
        self.assertEqual(resolution["exact_mention"], "Colonel Dana")
        self.assertEqual(resolution["classification"], CLASS_AMBIGUOUS)
        self.assertEqual(resolution["matched_person_ids"], [3, 4])

    def test_hebrew_prefixed_outer_resolves_the_unprefixed_name_surface(self):
        index = build_index(identity_with_canonical("אהרוני"))
        recorder = RecordingIndex(index)
        outer = "לאהרוני"
        row = mention(
            "M0001",
            outer,
            name="אהרוני",
            name_start_offset=1,
            name_end_offset=len(outer),
            known_name_match_quality="PREFIXED",
            detector_ids=["known-name"],
        )
        resolution = report_for([row], recorder)["resolutions"][0]
        self.assertEqual(recorder.lookups, ["אהרוני"])
        self.assertEqual(resolution["exact_mention"], outer)
        self.assertEqual(resolution["classification"], CLASS_DETERMINISTIC)
        self.assertEqual(resolution["matched_person_name"], "אהרוני")

    def test_changing_only_the_title_does_not_change_the_lookup_string(self):
        def titled(title):
            exact = f"{title} Dana"
            return mention(
                "M0001",
                exact,
                name="Dana",
                title_surface=title,
                title_start_offset=0,
                title_end_offset=len(title),
                name_start_offset=len(title) + 1,
                name_end_offset=len(exact),
                surface_kind="TITLE_AND_NAME",
            )

        first_index = RecordingIndex(build_index(IDENTITY))
        second_index = RecordingIndex(build_index(IDENTITY))
        first = report_for([titled("Colonel")], first_index)["resolutions"][0]
        second = report_for([titled("General")], second_index)["resolutions"][0]
        self.assertEqual(first_index.lookups, ["Dana"])
        self.assertEqual(second_index.lookups, ["Dana"])
        self.assertEqual(first["classification"], second["classification"])
        self.assertEqual(first["matched_person_ids"], second["matched_person_ids"])
        self.assertEqual(first["exact_mention"], "Colonel Dana")
        self.assertEqual(second["exact_mention"], "General Dana")

    def test_resolution_does_not_strip_a_prefix_or_parse_a_title(self):
        index = build_index(identity_with_canonical("אהרוני"))
        recorder = RecordingIndex(index)
        prefixed = report_for([mention("M0001", "לאהרוני", name="לאהרוני")], recorder)[
            "resolutions"
        ][0]
        self.assertEqual(recorder.lookups, ["לאהרוני"])
        self.assertEqual(prefixed["classification"], CLASS_NEW)
        titled = RecordingIndex(build_index(IDENTITY))
        unresolved = report_for(
            [mention("M0002", "Colonel Dana", name="Colonel Dana")], titled
        )["resolutions"][0]
        self.assertEqual(titled.lookups, ["Colonel Dana"])
        self.assertEqual(unresolved["classification"], CLASS_NEW)
        self.assertEqual(unresolved["exact_mention"], "Colonel Dana")

    def test_one_production_match_is_deterministic(self):
        row = report_for([mention("M0001", "augusta  king")])["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_DETERMINISTIC)
        self.assertEqual(row["matched_via"], "person_alias")
        self.assertEqual(row["matched_alias_id"], 10)
        self.assertEqual(row["matched_person_name"], "Ada Lovelace")
        self.assertTrue(row["identity_is_production"])

    def test_several_production_matches_are_ambiguous(self):
        row = report_for([mention("M0001", "Dana")])["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_AMBIGUOUS)
        self.assertEqual(row["matched_person_ids"], [3, 4])
        self.assertIsNone(row["matched_person_name"])
        self.assertIsNone(row["matched_via"])
        self.assertFalse(row["identity_is_production"])

    def test_no_production_match_is_a_new_person_candidate(self):
        row = report_for([mention("M0001", "Unknown Person")])["resolutions"][0]
        self.assertEqual(row["classification"], CLASS_NEW)
        self.assertEqual(row["matched_person_ids"], [])
        self.assertFalse(row["identity_is_production"])

    def test_v2_schema_has_no_not_a_person_state(self):
        enum = load_schema(RESOLUTION_REPORT_V2_SCHEMA)["properties"]["resolutions"][
            "items"
        ]["properties"]["classification"]["enum"]
        self.assertEqual(
            enum,
            [
                "DETERMINISTIC_EXISTING_PERSON",
                "AMBIGUOUS",
                "NEW_PERSON_CANDIDATE",
            ],
        )
        self.assertNotIn(CLASS_NOT_PERSON, enum)
        built = report_for(
            [
                mention("M0001", "Ada Lovelace"),
                mention("M0002", "Dana"),
                mention("M0003", "Nobody"),
            ]
        )
        self.assertNotIn(
            CLASS_NOT_PERSON,
            [row["classification"] for row in built["resolutions"]],
        )

    def test_detector_provenance_does_not_assign_identity(self):
        base = mention("M0001", "Nobody")
        known = mention(
            "M0001",
            "Nobody",
            detector_ids=["known-name"],
            known_name_match_quality="EXACT_BOUNDARY",
            surface_kind="NICKNAME",
        )
        first = report_for([base])["resolutions"][0]
        second = report_for([known])["resolutions"][0]
        self.assertEqual(first["classification"], CLASS_NEW)
        self.assertEqual(second["classification"], CLASS_NEW)
        self.assertEqual(first["matched_person_ids"], second["matched_person_ids"])
        shared = mention(
            "M0001",
            "Dana",
            detector_ids=["known-name", "semantic-surface"],
            known_name_match_quality="EXACT_BOUNDARY",
            surface_kind="PARTIAL_NAME",
        )
        plain = report_for([mention("M0001", "Dana")])["resolutions"][0]
        marked = report_for([shared])["resolutions"][0]
        self.assertEqual(plain["classification"], CLASS_AMBIGUOUS)
        self.assertEqual(marked["classification"], CLASS_AMBIGUOUS)
        self.assertEqual(plain["matched_person_ids"], marked["matched_person_ids"])
        self.assertEqual(marked["detector_ids"], ["known-name", "semantic-surface"])
        self.assertEqual(marked["surface_kind"], "PARTIAL_NAME")

    def test_verified_geometry_is_copied_unchanged(self):
        row = mention(
            "M0001",
            "Colonel Dana",
            name="Dana",
            title_surface="Colonel",
            title_start_offset=0,
            title_end_offset=7,
            name_start_offset=8,
            name_end_offset=12,
            surface_kind="TITLE_AND_NAME",
            detector_ids=["semantic-surface"],
            known_name_match_quality=None,
            occurrence_index=2,
            evidence_snippet="saw Colonel Dana there",
        )
        resolution = report_for([row])["resolutions"][0]
        for field in CARRIED_FIELDS:
            self.assertEqual(resolution[field], row[field], field)

    def test_rows_follow_mention_key_when_evidence_order_changes(self):
        mentions = [
            mention("M0003", "Nobody"),
            mention("M0001", "Ada Lovelace"),
            mention("M0002", "Grace Hopper"),
        ]
        shuffled = [mentions[1], mentions[2], mentions[0]]
        first = report_for(mentions)
        second = report_for(shuffled)
        keys = ["M0001", "M0002", "M0003"]
        self.assertEqual([row["mention_key"] for row in first["resolutions"]], keys)
        self.assertEqual([row["mention_key"] for row in second["resolutions"]], keys)
        self.assertEqual(first["resolutions"], second["resolutions"])
        self.assertNotEqual(
            first["evidence_package_logical_sha256"],
            second["evidence_package_logical_sha256"],
        )

    def test_run_steps_remain_the_v1_sequence(self):
        self.assertEqual(
            RUN_STEPS,
            ("discover", "freeze-evidence", "resolve", "review"),
        )
        enum = load_schema("run-checkpoint-v1")["properties"]["completed_steps"][
            "items"
        ]["enum"]
        self.assertNotIn("resolve-v2", enum)

    def test_v1_resolve_still_uses_exact_mention_and_rejects_v2_evidence(self):
        v1 = build_resolution_report(
            v1_evidence("Ada Lovelace"),
            build_index(IDENTITY),
            evidence_package_file_sha256="d" * 64,
            identity_snapshot_file_sha256="e" * 64,
            created_at=CREATED,
        )
        self.assertEqual(v1["schema_version"], RESOLUTION_REPORT_SCHEMA)
        self.assertEqual(v1["resolutions"][0]["classification"], CLASS_DETERMINISTIC)
        with self.assertRaises(Exception) as caught:
            build_resolution_report(
                evidence([mention("M0001", "Ada Lovelace")]),
                build_index(IDENTITY),
                evidence_package_file_sha256="d" * 64,
                identity_snapshot_file_sha256="e" * 64,
            )
        self.assertIn("evidence-package-v1", str(caught.exception))
        self.assertEqual(caught.exception.error_code, "MALFORMED_ARTIFACT")

    def test_cli_keeps_the_generations_apart(self):
        identity_path = Path(__file__).parent / "fixtures" / "identity_snapshot.json"
        before = identity_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v1_path = root / "evidence-v1.json"
            v2_path = root / "evidence-v2.json"
            v1_out = root / "resolution-v1.json"
            v2_out = root / "resolution-v2.json"
            write_json_atomic(v1_path, v1_evidence())
            write_json_atomic(
                v2_path, evidence([mention("M0001", "Colonel Dana", name="Dana")])
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                rejected_v2 = main(
                    [
                        "resolve",
                        "--evidence",
                        str(v2_path),
                        "--identity",
                        str(identity_path),
                        "--output",
                        str(v1_out),
                    ]
                )
            self.assertEqual(rejected_v2, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"], "MALFORMED_ARTIFACT"
            )
            self.assertIn("evidence-package-v1", stderr.getvalue())
            self.assertFalse(v1_out.exists())

            stdout = StringIO()
            with redirect_stdout(stdout):
                allowed = main(
                    [
                        "resolve",
                        "--evidence",
                        str(v1_path),
                        "--identity",
                        str(identity_path),
                        "--output",
                        str(v1_out),
                    ]
                )
            self.assertEqual(allowed, 0)
            self.assertEqual(
                json.loads(v1_out.read_text(encoding="utf-8"))["schema_version"],
                RESOLUTION_REPORT_SCHEMA,
            )

            stderr = StringIO()
            with redirect_stderr(stderr):
                rejected_v1 = main(
                    [
                        "resolve-v2",
                        "--evidence",
                        str(v1_path),
                        "--identity",
                        str(identity_path),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(v2_out),
                    ]
                )
            self.assertEqual(rejected_v1, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"],
                "UNSUPPORTED_SCHEMA_VERSION",
            )
            self.assertFalse(v2_out.exists())

            stdout = StringIO()
            with redirect_stdout(stdout):
                resolved = main(
                    [
                        "resolve-v2",
                        "--evidence",
                        str(v2_path),
                        "--identity",
                        str(identity_path),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(v2_out),
                    ]
                )
            self.assertEqual(resolved, 0)
            published = json.loads(v2_out.read_text(encoding="utf-8"))
            self.assertEqual(published["schema_version"], RESOLUTION_REPORT_V2_SCHEMA)
            self.assertFalse(published["production_write_performed"])
            self.assertEqual(
                published["evidence_package_file_sha256"], sha256_file(v2_path)
            )
            self.assertEqual(
                published["identity_snapshot_file_sha256"], sha256_file(identity_path)
            )
            self.assertEqual(published["resolutions"][0]["name_surface"], "Dana")
            self.assertEqual(
                published["resolutions"][0]["classification"], CLASS_AMBIGUOUS
            )
            review_out = root / "review.json"
            stderr = StringIO()
            with redirect_stderr(stderr):
                review = main(
                    [
                        "review-package",
                        "--resolution",
                        str(v2_out),
                        "--output",
                        str(review_out),
                    ]
                )
            self.assertEqual(review, 2)
            self.assertIn("resolution-report-v1", stderr.getvalue())
            self.assertFalse(review_out.exists())
        self.assertEqual(identity_path.read_bytes(), before)

    def test_resolve_v2_has_no_production_write_path(self):
        source = Path(inspect.getfile(build_resolution_report_v2)).read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertTrue(imported.isdisjoint({"django", "boto3", "psycopg", "sqlite3"}))
        command = inspect.getsource(main.__globals__["command_resolve_v2"])
        for token in (
            "django",
            "boto3",
            "sqlite3",
            "psycopg",
            "Person.objects",
            "apply",
        ):
            self.assertNotIn(token, command)
        report = report_for([mention("M0001", "Ada Lovelace")])
        encoded = json.dumps(report)
        self.assertNotIn("Do not copy this biography.", encoded)
        self.assertFalse(report["production_write_performed"])
        self.assertFalse(report["photo_person_created"])


if __name__ == "__main__":
    unittest.main()
