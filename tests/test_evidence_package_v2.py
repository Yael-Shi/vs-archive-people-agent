import copy
import inspect
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vs_archive_people.artifacts.io import (
    EVIDENCE_PACKAGE_V2_SCHEMA,
    MENTION_PROPOSALS_SCHEMA,
    ArtifactError,
    InputHashChangedError,
    LogicalHashMismatchError,
    RunIdMismatchError,
    UnsupportedSchemaVersionError,
    sha256_file,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import (
    logical_body,
    stamp_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import RUN_STEPS
from vs_archive_people.cli import (
    build_parser,
    command_freeze_evidence_v2,
    main,
)
from vs_archive_people.discovery.select import select_items
from vs_archive_people.evidence.mentions import mention_fingerprint
from vs_archive_people.evidence.package import build_evidence_package
from vs_archive_people.evidence.package_v2 import (
    build_evidence_package_v2,
    recheck_mention_source,
)
from vs_archive_people.mention_proposals.package import package_mention_proposals
from test_mention_evidence import (
    DISCOVERY_HASH,
    DOCUMENT_HASH,
    DOCUMENTS,
    PROPOSALS,
    discovery_for,
)
from test_mention_proposals import CREATED, SOURCE, geometry_row, snapshot_with

PROPOSAL_FILE_HASH = "1" * 64


def _freeze(snapshot, discovery, proposal, **overrides):
    arguments = {
        "run_id": "run-1",
        "document_snapshot_file_sha256": DOCUMENT_HASH,
        "discovery_file_sha256": DISCOVERY_HASH,
        "mention_proposals_file_sha256": PROPOSAL_FILE_HASH,
        "created_at": CREATED,
    }
    arguments.update(overrides)
    return build_evidence_package_v2(snapshot, discovery, proposal, **arguments)


def _pack(text, mentions):
    snapshot = snapshot_with(text)
    discovery = discovery_for(snapshot)
    proposal = package_mention_proposals(
        snapshot,
        discovery,
        {"mentions": mentions},
        run_id="run-1",
        document_snapshot_file_sha256=DOCUMENT_HASH,
        discovery_file_sha256=DISCOVERY_HASH,
        created_at=CREATED,
    )
    return snapshot, discovery, proposal


def _restamp(document):
    stamp_generated_artifact(document, created_at=CREATED)
    return document


def _rebind(snapshot, proposal):
    discovery = discovery_for(snapshot)
    rebound = copy.deepcopy(proposal)
    rebound["discovery_logical_sha256"] = discovery["logical_content_sha256"]
    rebound["discovery_file_sha256"] = DISCOVERY_HASH
    rebound["document_snapshot_file_sha256"] = DOCUMENT_HASH
    rebound["run_id"] = "run-1"
    return discovery, _restamp(rebound)


class EvidencePackageV2Tests(unittest.TestCase):
    def setUp(self):
        text = "Colonel Dana wrote the note."
        self.text = text
        self.snapshot, self.discovery, self.proposal = _pack(
            text,
            [
                geometry_row(
                    text,
                    "Colonel Dana",
                    name="Dana",
                    title="Colonel",
                    include_kind=True,
                    kind="TITLE_AND_NAME",
                )
            ],
        )

    def test_valid_proposal_freezes_verified_geometry(self):
        package = _freeze(self.snapshot, self.discovery, self.proposal)
        self.assertEqual(package["schema_version"], EVIDENCE_PACKAGE_V2_SCHEMA)
        self.assertEqual(package["run_id"], "run-1")
        self.assertEqual(package["document_snapshot_file_sha256"], DOCUMENT_HASH)
        self.assertEqual(
            package["discovery_logical_sha256"],
            self.discovery["logical_content_sha256"],
        )
        self.assertEqual(package["discovery_file_sha256"], DISCOVERY_HASH)
        self.assertEqual(
            package["mention_proposals_logical_sha256"],
            self.proposal["logical_content_sha256"],
        )
        self.assertEqual(package["mention_proposals_file_sha256"], PROPOSAL_FILE_HASH)
        self.assertFalse(package["production_write_performed"])
        self.assertNotIn("identity_snapshot_file_sha256", package)
        self.assertNotIn("detectors", package)
        self.assertNotIn("diagnostics", package)
        body = logical_body(package)
        self.assertNotIn("document_snapshot_file_sha256", body)
        self.assertNotIn("discovery_file_sha256", body)
        self.assertNotIn("mention_proposals_file_sha256", body)
        self.assertIn("discovery_logical_sha256", body)
        self.assertIn("mention_proposals_logical_sha256", body)
        mention = package["mentions"][0]
        self.assertEqual(mention["mention_key"], "M0001")
        self.assertEqual(mention["occurrence_index"], 0)
        self.assertEqual(mention["exact_mention"], "Colonel Dana")
        self.assertEqual(mention["name_surface"], "Dana")
        self.assertEqual(mention["title_surface"], "Colonel")
        self.assertEqual(mention["surface_kind"], "TITLE_AND_NAME")
        self.assertEqual(mention["detector_ids"], ["human-authored"])
        self.assertIsNone(mention["known_name_match_quality"])
        self.assertEqual(
            mention["mention_fingerprint"],
            mention_fingerprint(
                archive_item_id=10,
                evidence_source=SOURCE,
                source_text_sha256=mention["source_text_sha256"],
                start_offset=mention["start_offset"],
                end_offset=mention["end_offset"],
            ),
        )
        for forbidden in (
            "mention_class",
            "propose_alias",
            "proposed_identity_action",
            "confidence",
            "person_id",
            "detector_attributes",
            "text_role",
        ):
            self.assertNotIn(forbidden, mention)

    def test_identity_snapshot_audit_hash_is_not_an_evidence_parent(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["identity_snapshot_file_sha256"] = "d" * 64
        package = _freeze(self.snapshot, self.discovery, proposal)
        untouched = _freeze(self.snapshot, self.discovery, self.proposal)
        self.assertEqual(
            package["logical_content_sha256"], untouched["logical_content_sha256"]
        )
        self.assertNotIn("identity_snapshot_file_sha256", package)

    def test_malformed_proposal_is_rejected(self):
        proposal = copy.deepcopy(self.proposal)
        del proposal["mentions"]
        _restamp(proposal)
        with self.assertRaises(ArtifactError) as caught:
            _freeze(self.snapshot, self.discovery, proposal)
        self.assertEqual(caught.exception.error_code, "MALFORMED_ARTIFACT")

    def test_logical_hash_mismatch_is_rejected(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["logical_content_sha256"] = "0" * 64
        with self.assertRaises(LogicalHashMismatchError):
            _freeze(self.snapshot, self.discovery, proposal)

    def test_wrong_schema_and_loose_list_are_rejected(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["schema_version"] = "evidence-package-v1"
        with self.assertRaises(UnsupportedSchemaVersionError):
            _freeze(self.snapshot, self.discovery, proposal)
        with self.assertRaises(UnsupportedSchemaVersionError):
            _freeze(self.snapshot, self.discovery, {"mentions": PROPOSALS})
        with self.assertRaises(ArtifactError) as caught:
            _freeze(self.snapshot, self.discovery, PROPOSALS)
        self.assertEqual(caught.exception.error_code, "MALFORMED_ARTIFACT")

    def test_proposal_run_id_mismatch_is_rejected(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["run_id"] = "run-other"
        _restamp(proposal)
        with self.assertRaises(RunIdMismatchError):
            _freeze(self.snapshot, self.discovery, proposal)

    def test_parent_hash_mismatches_are_rejected(self):
        cases = {
            "discovery_logical_sha256": "e" * 64,
            "discovery_file_sha256": "e" * 64,
            "document_snapshot_file_sha256": "e" * 64,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                proposal = copy.deepcopy(self.proposal)
                proposal[field] = value
                if field == "discovery_logical_sha256":
                    _restamp(proposal)
                with self.assertRaises(InputHashChangedError):
                    _freeze(self.snapshot, self.discovery, proposal)

    def test_outer_slice_is_rechecked_against_the_displayed_body(self):
        mutated = copy.deepcopy(self.snapshot)
        mutated["items"][0]["ocr_document"]["displayed_transcription"]["text"] = (
            "Colonel Dina wrote the note."
        )
        discovery, proposal = _rebind(mutated, self.proposal)
        with self.assertRaises(ArtifactError) as caught:
            _freeze(mutated, discovery, proposal)
        self.assertIn("displayed body", str(caught.exception))
        self.assertNotIn("Dina", str(caught.exception))
        self.assertNotIn("Dana", str(caught.exception))

    def test_discovery_scope_failures_are_rejected(self):
        outside = copy.deepcopy(self.discovery)
        outside["selected_items"] = []
        _restamp(outside)
        proposal = copy.deepcopy(self.proposal)
        proposal["discovery_logical_sha256"] = outside["logical_content_sha256"]
        _restamp(proposal)
        with self.assertRaises(ArtifactError) as caught:
            _freeze(self.snapshot, outside, proposal)
        self.assertIn("outside the discovery selection", str(caught.exception))

        unknown = copy.deepcopy(self.proposal)
        unknown["mentions"][0]["archive_item_id"] = 99
        _restamp(unknown)
        with self.assertRaises(ArtifactError) as unknown_caught:
            _freeze(self.snapshot, self.discovery, unknown)
        self.assertIn("unknown", str(unknown_caught.exception))

        changed = copy.deepcopy(self.snapshot)
        changed["items"][0]["ocr_document"]["displayed_transcription"]["text"] = (
            "Colonel Dana wrote another note."
        )
        with self.assertRaises(ArtifactError) as changed_caught:
            _freeze(changed, self.discovery, self.proposal)
        self.assertIn("displayed text hash changed", str(changed_caught.exception))

        role = copy.deepcopy(self.proposal)
        role["mentions"][0]["evidence_source"] = "title"
        _restamp(role)
        with self.assertRaises(ArtifactError) as role_caught:
            _freeze(self.snapshot, self.discovery, role)
        self.assertIn("not a displayed text role", str(role_caught.exception))

    def test_duplicate_outer_span_is_rejected(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["mentions"].append(copy.deepcopy(proposal["mentions"][0]))
        _restamp(proposal)
        with self.assertRaises(ArtifactError) as caught:
            _freeze(self.snapshot, self.discovery, proposal)
        self.assertIn("duplicate outer span", str(caught.exception))

    def test_occurrence_identity_ignores_detector_provenance_and_row_order(self):
        text = "Ada met Grace today."
        mentions = [
            geometry_row(text, "Ada", include_kind=True, kind="FULL_NAME"),
            geometry_row(text, "Grace", include_kind=True, kind="FULL_NAME"),
        ]
        forward = _pack(text, mentions)
        backward = _pack(text, list(reversed(mentions)))
        frozen_forward = _freeze(*forward)
        frozen_backward = _freeze(*backward)
        self.assertEqual(
            frozen_forward["logical_content_sha256"],
            frozen_backward["logical_content_sha256"],
        )
        self.assertEqual(
            [row["mention_key"] for row in frozen_forward["mentions"]],
            ["M0001", "M0002"],
        )
        self.assertEqual(
            [row["exact_mention"] for row in frozen_forward["mentions"]],
            ["Ada", "Grace"],
        )
        self.assertEqual(
            [row["occurrence_index"] for row in frozen_forward["mentions"]],
            [0, 1],
        )
        reversed_rows = copy.deepcopy(forward[2])
        reversed_rows["mentions"] = list(reversed(reversed_rows["mentions"]))
        _restamp(reversed_rows)
        frozen_reversed = _freeze(forward[0], forward[1], reversed_rows)
        self.assertEqual(frozen_reversed["mentions"], frozen_forward["mentions"])
        self.assertNotEqual(
            frozen_reversed["mention_proposals_logical_sha256"],
            frozen_forward["mention_proposals_logical_sha256"],
        )

        other = copy.deepcopy(forward[2])
        other["detectors"] = [
            {
                "id": "other-detector",
                "role": "human",
                "implementation": "manual",
                "version": "9",
            }
        ]
        for mention in other["mentions"]:
            mention["detector_ids"] = ["other-detector"]
        _restamp(other)
        frozen_other = _freeze(forward[0], forward[1], other)
        self.assertEqual(
            [row["mention_fingerprint"] for row in frozen_other["mentions"]],
            [row["mention_fingerprint"] for row in frozen_forward["mentions"]],
        )
        self.assertEqual(
            [row["mention_key"] for row in frozen_other["mentions"]],
            [row["mention_key"] for row in frozen_forward["mentions"]],
        )
        self.assertEqual(
            [row["occurrence_index"] for row in frozen_other["mentions"]],
            [row["occurrence_index"] for row in frozen_forward["mentions"]],
        )
        self.assertNotEqual(
            frozen_other["mentions"][0]["detector_ids"],
            frozen_forward["mentions"][0]["detector_ids"],
        )

    def test_source_recheck_rejects_geometry_that_disagrees_with_the_body(self):
        body = self.text
        mention = copy.deepcopy(self.proposal["mentions"][0])
        declared = {"human-authored", "known-name"}
        recheck_mention_source(mention, body, declared)

        def expect(mutator, text):
            row = copy.deepcopy(mention)
            mutator(row)
            with self.assertRaises(ArtifactError) as caught:
                recheck_mention_source(row, body, declared)
            self.assertIn(text, str(caught.exception))
            self.assertNotIn(body, str(caught.exception))

        def name_surface(row):
            row["name_surface"] = "Dina"

        def title_surface(row):
            row["title_surface"] = "General"

        def name_outside(row):
            row["name_start_offset"] = row["end_offset"]
            row["name_end_offset"] = row["end_offset"] + 1

        def title_outside(row):
            row["title_start_offset"] = row["end_offset"]
            row["title_end_offset"] = row["end_offset"] + 1

        def overlap(row):
            row["title_end_offset"] = row["name_end_offset"]

        def half_null(row):
            row["title_end_offset"] = None

        def kind(row):
            row["title_surface"] = None
            row["title_start_offset"] = None
            row["title_end_offset"] = None
            row["surface_kind"] = "TITLE_AND_NAME"
            row["exact_mention"] = "Dana"
            row["start_offset"] = row["name_start_offset"]
            row["end_offset"] = row["name_end_offset"]

        def undeclared(row):
            row["detector_ids"] = ["missing"]

        def missing_quality(row):
            row["detector_ids"] = ["known-name"]
            row["known_name_match_quality"] = None

        def extra_quality(row):
            row["known_name_match_quality"] = "EXACT_BOUNDARY"

        expect(name_surface, "name_surface does not match the displayed body")
        expect(title_surface, "title_surface does not match the displayed body")
        expect(name_outside, "name span is outside the outer mention")
        expect(title_outside, "title span is outside the outer mention")
        expect(overlap, "title span overlaps the name span")
        expect(half_null, "title offsets must both be null or both be integers")
        expect(kind, "TITLE_AND_NAME requires a title slice")
        expect(undeclared, "not declared")
        expect(missing_quality, "known_name_match_quality is required")
        expect(extra_quality, "known_name_match_quality is allowed only")

        broken = "Colonel\nDana\nwrote the note."
        multiline = copy.deepcopy(mention)
        multiline["exact_mention"] = "Colonel\nDana\nwrote"
        multiline["start_offset"] = 0
        multiline["end_offset"] = len(multiline["exact_mention"])
        with self.assertRaises(ArtifactError) as line_caught:
            recheck_mention_source(multiline, broken, declared)
        self.assertIn("at most one line break", str(line_caught.exception))

        prefixed = "האהרוני wrote the note."
        unsafe = copy.deepcopy(mention)
        unsafe["exact_mention"] = "האהרוני"
        unsafe["start_offset"] = 0
        unsafe["end_offset"] = len("האהרוני")
        unsafe["name_surface"] = "אהרוני"
        unsafe["name_start_offset"] = 1
        unsafe["name_end_offset"] = len("האהרוני")
        unsafe["title_surface"] = None
        unsafe["title_start_offset"] = None
        unsafe["title_end_offset"] = None
        unsafe["surface_kind"] = None
        unsafe["evidence_snippet"] = prefixed
        with self.assertRaises(ArtifactError) as residue_caught:
            recheck_mention_source(unsafe, prefixed, declared)
        self.assertIn("unsafe residue", str(residue_caught.exception))

    def test_freeze_rejects_invalid_geometry_on_a_stamped_proposal(self):
        proposal = copy.deepcopy(self.proposal)
        proposal["mentions"][0]["surface_kind"] = "FULL_NAME"
        _restamp(proposal)
        with self.assertRaises(ArtifactError) as caught:
            _freeze(self.snapshot, self.discovery, proposal)
        self.assertIn("contradicts a title slice", str(caught.exception))

    def test_run_steps_remain_the_v1_sequence(self):
        self.assertEqual(
            RUN_STEPS, ("discover", "freeze-evidence", "resolve", "review")
        )


class EvidencePackageV2CliTests(unittest.TestCase):
    def test_v2_succeeds_without_a_diagnostics_file(self):
        source = "\n".join(
            (
                inspect.getsource(command_freeze_evidence_v2),
                inspect.getsource(build_evidence_package_v2),
            )
        )
        self.assertNotIn("diagnostic", source.lower())
        parser = build_parser()
        text = "Ada Lovelace wrote the note."
        snapshot = snapshot_with(text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents = root / "documents.json"
            discovery_path = root / "discovery.json"
            mentions = root / "mention-proposals.json"
            output = root / "evidence.json"
            documents.write_text(
                json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
            )
            selection = select_items(snapshot)
            selection["run_id"] = "run-1"
            selection["document_snapshot_file_sha256"] = sha256_file(documents)
            selection["prior_checkpoint_logical_sha256"] = None
            selection["prior_checkpoint_file_sha256"] = None
            stamp_generated_artifact(selection, created_at=CREATED)
            write_json_atomic(discovery_path, selection)
            proposal = package_mention_proposals(
                snapshot,
                selection,
                {
                    "mentions": [
                        geometry_row(
                            text, "Ada Lovelace", include_kind=True, kind="FULL_NAME"
                        )
                    ]
                },
                run_id="run-1",
                document_snapshot_file_sha256=sha256_file(documents),
                discovery_file_sha256=sha256_file(discovery_path),
                created_at=CREATED,
            )
            write_json_atomic(mentions, proposal)
            self.assertFalse(any(root.glob("*diagnostic*")))
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "freeze-evidence-v2",
                        "--documents",
                        str(documents),
                        "--discovery",
                        str(discovery_path),
                        "--mentions",
                        str(mentions),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["schema_version"], EVIDENCE_PACKAGE_V2_SCHEMA)
            self.assertFalse(any(root.glob("*diagnostic*")))
            with redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(
                        [
                            "freeze-evidence-v2",
                            "--documents",
                            str(documents),
                            "--discovery",
                            str(discovery_path),
                            "--mentions",
                            str(mentions),
                            "--run-id",
                            "run-1",
                            "--output",
                            str(output),
                            "--diagnostics",
                            str(root / "mention-proposal-diagnostics.json"),
                        ]
                    )

    def test_generation_separation(self):
        text = "Ada Lovelace wrote the note."
        snapshot = snapshot_with(text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents = root / "documents.json"
            discovery_path = root / "discovery.json"
            official = root / "official.json"
            loose = root / "loose.json"
            v1_output = root / "v1.json"
            v2_output = root / "v2.json"
            documents.write_text(
                json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
            )
            selection = select_items(snapshot)
            selection["run_id"] = "run-1"
            selection["document_snapshot_file_sha256"] = sha256_file(documents)
            selection["prior_checkpoint_logical_sha256"] = None
            selection["prior_checkpoint_file_sha256"] = None
            stamp_generated_artifact(selection, created_at=CREATED)
            write_json_atomic(discovery_path, selection)
            proposal = package_mention_proposals(
                snapshot,
                selection,
                {
                    "mentions": [
                        geometry_row(
                            text, "Ada Lovelace", include_kind=True, kind="FULL_NAME"
                        )
                    ]
                },
                run_id="run-1",
                document_snapshot_file_sha256=sha256_file(documents),
                discovery_file_sha256=sha256_file(discovery_path),
                created_at=CREATED,
            )
            self.assertEqual(proposal["schema_version"], MENTION_PROPOSALS_SCHEMA)
            write_json_atomic(official, proposal)
            loose.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "archive_item_id": 10,
                                "evidence_source": SOURCE,
                                "start_offset": 0,
                                "end_offset": len("Ada Lovelace"),
                                "exact_mention": "Ada Lovelace",
                                "evidence_snippet": "Ada Lovelace wrote the note.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                blocked = main(
                    [
                        "freeze-evidence",
                        "--documents",
                        str(documents),
                        "--discovery",
                        str(discovery_path),
                        "--mentions",
                        str(official),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(v1_output),
                    ]
                )
            self.assertEqual(blocked, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"],
                "UNSUPPORTED_SCHEMA_VERSION",
            )
            self.assertFalse(v1_output.exists())

            stdout = StringIO()
            with redirect_stdout(stdout):
                allowed = main(
                    [
                        "freeze-evidence",
                        "--documents",
                        str(documents),
                        "--discovery",
                        str(discovery_path),
                        "--mentions",
                        str(loose),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(v1_output),
                    ]
                )
            self.assertEqual(allowed, 0)
            self.assertEqual(
                json.loads(v1_output.read_text(encoding="utf-8"))["schema_version"],
                "evidence-package-v1",
            )

            stderr = StringIO()
            with redirect_stderr(stderr):
                rejected = main(
                    [
                        "freeze-evidence-v2",
                        "--documents",
                        str(documents),
                        "--discovery",
                        str(discovery_path),
                        "--mentions",
                        str(loose),
                        "--run-id",
                        "run-1",
                        "--output",
                        str(v2_output),
                    ]
                )
            self.assertEqual(rejected, 2)
            self.assertEqual(
                json.loads(stderr.getvalue())["error_code"],
                "UNSUPPORTED_SCHEMA_VERSION",
            )
            self.assertFalse(v2_output.exists())

    def test_v1_library_freeze_is_unchanged(self):
        selection = discovery_for(DOCUMENTS)
        package = build_evidence_package(
            DOCUMENTS,
            selection,
            PROPOSALS,
            run_id="run-1",
            document_snapshot_file_sha256=DOCUMENT_HASH,
            discovery_file_sha256=DISCOVERY_HASH,
            mention_proposals_file_sha256=PROPOSAL_FILE_HASH,
            created_at=CREATED,
        )
        self.assertEqual(package["schema_version"], "evidence-package-v1")
        self.assertEqual(
            [row["mention_key"] for row in package["mentions"]], ["M0001", "M0002"]
        )
        self.assertEqual(package["mentions"][0]["mention_class"], "PERSON")
        self.assertNotIn("mention_proposals_logical_sha256", package)


if __name__ == "__main__":
    unittest.main()
