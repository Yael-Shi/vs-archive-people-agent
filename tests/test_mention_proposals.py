import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vs_archive_people.artifacts.io import (
    MENTION_PROPOSALS_SCHEMA,
    ArtifactError,
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
    verify_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import SCHEMA_FILES, validate_artifact
from vs_archive_people.cli import main
from vs_archive_people.discovery.select import select_items
from vs_archive_people.mention_proposals.geometry import evidence_line_snippet
from vs_archive_people.mention_proposals.package import (
    HUMAN_DETECTOR,
    package_mention_proposals,
)
from test_mention_evidence import DOCUMENT_HASH, discovery_for

CREATED = "2026-01-01T00:00:00Z"
DISCOVERY_HASH = "b" * 64
SOURCE = "ocr_document.displayed_transcription.text"
FORBIDDEN_FIELDS = (
    "person_id",
    "mention_key",
    "mention_fingerprint",
    "occurrence_index",
    "mention_class",
    "propose_alias",
    "proposed_identity_action",
    "confidence",
    "detector_attributes",
)


def snapshot_with(*texts: str) -> dict:
    items = []
    for index, text in enumerate(texts, start=10):
        items.append(
            {
                "archive_item_id": index,
                "item_type": "OCR_DOCUMENT",
                "title": "metadata",
                "ocr_document": {"displayed_transcription": {"text": text}},
            }
        )
    return {
        "schema_version": "document-text-snapshot-v1",
        "exported_at": "2026-01-01T00:00:00+00:00",
        "items": items,
    }


def geometry_row(
    text: str,
    surface: str,
    *,
    name: str | None = None,
    title: str | None = None,
    kind: str | None = None,
    include_kind: bool = False,
    item_id: int = 10,
) -> dict:
    start = text.index(surface)
    end = start + len(surface)
    name_text = surface if name is None else name
    name_start = start + surface.index(name_text)
    row = {
        "archive_item_id": item_id,
        "evidence_source": SOURCE,
        "start_offset": start,
        "end_offset": end,
        "name_start_offset": name_start,
        "name_end_offset": name_start + len(name_text),
        "title_start_offset": None,
        "title_end_offset": None,
    }
    if title is not None:
        title_start = start + surface.index(title)
        row["title_start_offset"] = title_start
        row["title_end_offset"] = title_start + len(title)
    if include_kind:
        row["surface_kind"] = kind
    return row


def package_text(text: str, mentions: list[dict], *, run_id: str = "run-1") -> dict:
    snapshot = snapshot_with(text)
    return package_mention_proposals(
        snapshot,
        discovery_for(snapshot),
        {"mentions": mentions},
        run_id=run_id,
        document_snapshot_file_sha256=DOCUMENT_HASH,
        discovery_file_sha256=DISCOVERY_HASH,
        created_at=CREATED,
    )


def _detector(detector_id: str = "human-authored", **overrides) -> dict:
    row = {
        "id": detector_id,
        "role": "human",
        "implementation": "manual",
        "version": "1",
    }
    row.update(overrides)
    return row


def _mention(**overrides) -> dict:
    row = {
        "archive_item_id": 10,
        "evidence_source": SOURCE,
        "exact_mention": "Ada",
        "start_offset": 0,
        "end_offset": 3,
        "evidence_snippet": "Ada wrote.",
        "name_surface": "Ada",
        "name_start_offset": 0,
        "name_end_offset": 3,
        "title_surface": None,
        "title_start_offset": None,
        "title_end_offset": None,
        "surface_kind": None,
        "detector_ids": ["human-authored"],
        "known_name_match_quality": None,
    }
    row.update(overrides)
    return row


def _artifact(**overrides) -> dict:
    document = {
        "schema_version": MENTION_PROPOSALS_SCHEMA,
        "run_id": "run-1",
        "document_snapshot_file_sha256": "a" * 64,
        "discovery_logical_sha256": "b" * 64,
        "discovery_file_sha256": "c" * 64,
        "identity_snapshot_file_sha256": None,
        "production_write_performed": False,
        "detectors": [],
        "mentions": [],
    }
    document.update(overrides)
    return stamp_generated_artifact(document, created_at=CREATED)


class MentionProposalSchemaTests(unittest.TestCase):
    def test_empty_artifact_is_valid(self):
        document = _artifact()
        validate_artifact(document)
        verified = verify_generated_artifact(
            document, MENTION_PROPOSALS_SCHEMA, "mention proposals"
        )
        self.assertEqual(verified["mentions"], [])
        self.assertEqual(verified["detectors"], [])
        self.assertNotIn("diagnostics", verified)
        self.assertEqual(
            SCHEMA_FILES[MENTION_PROPOSALS_SCHEMA], "mention_proposals.v1.json"
        )

    def test_detector_references_must_be_declared(self):
        missing = _artifact(
            detectors=[_detector(role="custom-role")],
            mentions=[_mention(detector_ids=["missing"])],
        )
        with self.assertRaises(ArtifactError) as caught:
            validate_artifact(missing)
        self.assertIn("missing", str(caught.exception))
        present = _artifact(
            detectors=[_detector(role="custom-role")],
            mentions=[_mention()],
        )
        validate_artifact(present)
        self.assertEqual(present["detectors"][0]["role"], "custom-role")
        scored = _artifact(
            detectors=[_detector()],
            mentions=[_mention(known_name_match_quality="EXACT_BOUNDARY")],
        )
        with self.assertRaises(ArtifactError) as scored_caught:
            validate_artifact(scored)
        self.assertIn("known-name", str(scored_caught.exception))
        known = _artifact(
            detectors=[
                _detector(
                    "known-name",
                    role="deterministic",
                    implementation="exact-literal",
                )
            ],
            mentions=[
                _mention(
                    detector_ids=["known-name"],
                    known_name_match_quality="PREFIXED",
                )
            ],
        )
        validate_artifact(known)
        omitted = _artifact(
            detectors=[
                _detector(
                    "known-name",
                    role="deterministic",
                    implementation="exact-literal",
                )
            ],
            mentions=[
                _mention(
                    detector_ids=["known-name"],
                    known_name_match_quality=None,
                )
            ],
        )
        with self.assertRaises(ArtifactError):
            validate_artifact(omitted)

    def test_duplicate_detector_ids_are_rejected(self):
        document = _artifact(
            detectors=[_detector(), _detector(role="semantic", implementation="other")]
        )
        with self.assertRaises(ArtifactError) as caught:
            validate_artifact(document)
        self.assertIn("duplicate detector id", str(caught.exception))

    def test_forbidden_identity_fields_are_rejected(self):
        for field in FORBIDDEN_FIELDS:
            with self.subTest(field=field):
                mention = _mention()
                mention[field] = None
                document = _artifact(
                    detectors=[_detector()],
                    mentions=[mention],
                )
                with self.assertRaises(ArtifactError) as caught:
                    validate_artifact(document)
                self.assertIn(field, str(caught.exception))

    def test_undeclared_fields_are_rejected(self):
        top = _artifact(diagnostics=[])
        with self.assertRaises(ArtifactError) as caught:
            validate_artifact(top)
        self.assertIn("diagnostics", str(caught.exception))
        mention = _mention(note="extra")
        nested = _artifact(detectors=[_detector()], mentions=[mention])
        with self.assertRaises(ArtifactError) as caught:
            validate_artifact(nested)
        self.assertIn("note", str(caught.exception))

    def test_logical_hash_verification(self):
        document = _artifact(
            detectors=[_detector()],
            mentions=[_mention()],
        )
        verify_generated_artifact(
            document, MENTION_PROPOSALS_SCHEMA, "mention proposals"
        )
        self.assertNotIn("identity_snapshot_file_sha256", logical_body(document))
        self.assertNotIn("document_snapshot_file_sha256", logical_body(document))
        self.assertIn("discovery_logical_sha256", logical_body(document))
        self.assertEqual(
            document["logical_content_sha256"], sha256_json(logical_body(document))
        )
        retimed = json.loads(json.dumps(document))
        retimed["artifact_created_at"] = "2026-01-02T00:00:00Z"
        self.assertEqual(
            verify_generated_artifact(
                retimed, MENTION_PROPOSALS_SCHEMA, "mention proposals"
            )["logical_content_sha256"],
            document["logical_content_sha256"],
        )
        changed = json.loads(json.dumps(document))
        changed["discovery_logical_sha256"] = "d" * 64
        with self.assertRaises(LogicalHashMismatchError):
            verify_generated_artifact(
                changed, MENTION_PROPOSALS_SCHEMA, "mention proposals"
            )

    def test_identity_snapshot_hash_is_nullable(self):
        empty = _artifact()
        validate_artifact(empty)
        self.assertIsNone(empty["identity_snapshot_file_sha256"])
        stored = empty["logical_content_sha256"]
        empty["identity_snapshot_file_sha256"] = "e" * 64
        validate_artifact(empty)
        self.assertEqual(empty["logical_content_sha256"], stored)
        self.assertEqual(sha256_json(logical_body(empty)), stored)
        empty["identity_snapshot_file_sha256"] = "nope"
        with self.assertRaises(ArtifactError):
            validate_artifact(empty)


class PackageMentionProposalTests(unittest.TestCase):
    def test_full_name(self):
        text = "Ada Lovelace wrote the note."
        row = geometry_row(text, "Ada Lovelace", kind="FULL_NAME", include_kind=True)
        packaged = package_text(text, [row])
        mention = packaged["mentions"][0]
        self.assertEqual(packaged["schema_version"], MENTION_PROPOSALS_SCHEMA)
        self.assertEqual(packaged["document_snapshot_file_sha256"], DOCUMENT_HASH)
        self.assertEqual(packaged["discovery_file_sha256"], DISCOVERY_HASH)
        self.assertEqual(
            packaged["discovery_logical_sha256"],
            discovery_for(snapshot_with(text))["logical_content_sha256"],
        )
        self.assertIsNone(packaged["identity_snapshot_file_sha256"])
        self.assertFalse(packaged["production_write_performed"])
        self.assertEqual(packaged["detectors"], [HUMAN_DETECTOR])
        self.assertEqual(mention["exact_mention"], "Ada Lovelace")
        self.assertEqual(mention["name_surface"], "Ada Lovelace")
        self.assertIsNone(mention["title_surface"])
        self.assertEqual(mention["surface_kind"], "FULL_NAME")
        self.assertEqual(mention["detector_ids"], ["human-authored"])
        self.assertIsNone(mention["known_name_match_quality"])
        self.assertNotIn("person_id", mention)

    def test_partial_name(self):
        text = "Ada Lovelace wrote the note."
        packaged = package_text(
            text,
            [geometry_row(text, "Ada", kind="PARTIAL_NAME", include_kind=True)],
        )
        mention = packaged["mentions"][0]
        self.assertEqual(mention["exact_mention"], "Ada")
        self.assertEqual(mention["name_surface"], "Ada")
        self.assertEqual(mention["surface_kind"], "PARTIAL_NAME")
        self.assertIsNone(mention["title_surface"])

    def test_title_and_name(self):
        text = "Colonel Dana wrote."
        packaged = package_text(
            text,
            [
                geometry_row(
                    text,
                    "Colonel Dana",
                    name="Dana",
                    title="Colonel",
                    kind="TITLE_AND_NAME",
                    include_kind=True,
                )
            ],
        )
        mention = packaged["mentions"][0]
        self.assertEqual(mention["exact_mention"], "Colonel Dana")
        self.assertEqual(mention["title_surface"], "Colonel")
        self.assertEqual(mention["name_surface"], "Dana")
        self.assertEqual(mention["surface_kind"], "TITLE_AND_NAME")
        self.assertLess(mention["title_end_offset"], mention["name_start_offset"])

    def test_null_title_offsets(self):
        text = "Ada Lovelace wrote the note."
        row = geometry_row(text, "Ada Lovelace")
        self.assertIsNone(row["title_start_offset"])
        self.assertIsNone(row["title_end_offset"])
        mention = package_text(text, [row])["mentions"][0]
        self.assertIsNone(mention["title_surface"])
        self.assertIsNone(mention["title_start_offset"])
        self.assertIsNone(mention["title_end_offset"])
        self.assertIsNone(mention["surface_kind"])

    def test_punctuation_gap_between_title_and_name(self):
        text = "Colonel, Dana wrote."
        mention = package_text(
            text,
            [geometry_row(text, "Colonel, Dana", name="Dana", title="Colonel")],
        )["mentions"][0]
        self.assertEqual(mention["exact_mention"], "Colonel, Dana")
        self.assertEqual(mention["title_surface"], "Colonel")
        self.assertEqual(mention["name_surface"], "Dana")

    def test_letter_or_digit_between_title_and_name_is_rejected(self):
        text = "Colonel2 Dana wrote."
        with self.assertRaises(ArtifactError) as caught:
            package_text(
                text,
                [geometry_row(text, "Colonel2 Dana", name="Dana", title="Colonel")],
            )
        self.assertIn("unsafe residue", str(caught.exception))

    def test_safe_hebrew_proclitic_is_kept_out_of_the_name(self):
        text = "לאהרוני wrote the note."
        mention = package_text(text, [geometry_row(text, "לאהרוני", name="אהרוני")])[
            "mentions"
        ][0]
        self.assertEqual(mention["exact_mention"], "לאהרוני")
        self.assertEqual(mention["name_surface"], "אהרוני")
        unsafe = "האהרוני wrote the note."
        with self.assertRaises(ArtifactError) as caught:
            package_text(unsafe, [geometry_row(unsafe, "האהרוני", name="אהרוני")])
        self.assertIn("unsafe residue", str(caught.exception))

    def test_cross_line_span(self):
        text = "Hello Dana\nCohen today\nNEXT"
        mention = package_text(text, [geometry_row(text, "Dana\nCohen")])["mentions"][0]
        self.assertEqual(mention["exact_mention"], "Dana\nCohen")
        self.assertEqual(mention["name_surface"], "Dana\nCohen")
        self.assertEqual(mention["evidence_snippet"], "Hello Dana\nCohen today")
        self.assertNotIn("NEXT", mention["evidence_snippet"])

    def test_crlf_is_preserved(self):
        text = "Hello  Dana\r\nCohen today"
        mention = package_text(text, [geometry_row(text, "Dana\r\nCohen")])["mentions"][
            0
        ]
        self.assertEqual(mention["exact_mention"], "Dana\r\nCohen")
        self.assertIn("\r\n", mention["exact_mention"])
        self.assertEqual(mention["evidence_snippet"], "Hello  Dana\r\nCohen today")
        self.assertIn("\r\n", mention["evidence_snippet"])
        self.assertIn("  ", mention["evidence_snippet"])

    def test_name_outside_outer_is_rejected(self):
        text = "Ada Lovelace wrote the note."
        row = geometry_row(text, "Ada Lovelace")
        wrote = text.index("wrote")
        row["name_start_offset"] = wrote
        row["name_end_offset"] = wrote + len("wrote")
        with self.assertRaises(ArtifactError) as caught:
            package_text(text, [row])
        self.assertIn("outside the outer", str(caught.exception))

    def test_title_name_overlap_is_rejected(self):
        text = "Colonel Dana wrote."
        row = geometry_row(text, "Colonel Dana", name="Dana", title="Colonel")
        row["title_end_offset"] = row["name_start_offset"] + 1
        with self.assertRaises(ArtifactError) as caught:
            package_text(text, [row])
        self.assertIn("overlaps", str(caught.exception))

    def test_half_null_title_offsets_are_rejected(self):
        text = "Ada Lovelace wrote the note."
        row = geometry_row(text, "Ada Lovelace")
        row["title_start_offset"] = 0
        row["title_end_offset"] = None
        with self.assertRaises(ArtifactError) as caught:
            package_text(text, [row])
        self.assertIn("both be null or both be integers", str(caught.exception))

    def test_invalid_evidence_source_is_rejected(self):
        text = "Ada Lovelace wrote the note."
        row = geometry_row(text, "Ada Lovelace")
        row["evidence_source"] = "manual_text.body"
        with self.assertRaises(ArtifactError) as caught:
            package_text(text, [row])
        self.assertIn("not a displayed text role", str(caught.exception))

    def test_item_not_selected_by_discovery_is_rejected(self):
        snapshot = snapshot_with("Ada Lovelace wrote the note.", "Other Person wrote.")
        selection = discovery_for(snapshot)
        selection["selected_items"] = [
            row for row in selection["selected_items"] if row["archive_item_id"] == 10
        ]
        stamp_generated_artifact(selection, created_at=CREATED)
        row = geometry_row("Other Person wrote.", "Other Person", item_id=11)
        with self.assertRaises(ArtifactError) as caught:
            package_mention_proposals(
                snapshot,
                selection,
                {"mentions": [row]},
                run_id="run-1",
                document_snapshot_file_sha256=DOCUMENT_HASH,
                discovery_file_sha256=DISCOVERY_HASH,
                created_at=CREATED,
            )
        self.assertIn("outside the discovery selection", str(caught.exception))

    def test_run_id_mismatch_is_rejected(self):
        text = "Ada Lovelace wrote the note."
        with self.assertRaises(RunIdMismatchError) as caught:
            package_text(text, [geometry_row(text, "Ada")], run_id="run-other")
        self.assertEqual(caught.exception.error_code, "RUN_ID_MISMATCH")

    def test_forbidden_geometry_properties_are_rejected(self):
        text = "Ada Lovelace wrote the note."
        for field, value in (
            ("exact_mention", "Ada Lovelace"),
            ("evidence_snippet", "Ada Lovelace wrote the note."),
            ("person_id", 1),
            ("mention_class", "PERSON"),
            ("detector_attributes", {}),
        ):
            with self.subTest(field=field):
                row = geometry_row(text, "Ada Lovelace")
                row[field] = value
                with self.assertRaises(ArtifactError) as caught:
                    package_text(text, [row])
                self.assertIn(field, str(caught.exception))
                self.assertIn("forbidden geometry property", str(caught.exception))

    def test_source_strings_and_snippet_are_derived_locally(self):
        text = "Ada  Lovelace wrote.\r\nnext"
        row = geometry_row(text, "Ada  Lovelace")
        self.assertNotIn("exact_mention", row)
        self.assertNotIn("evidence_snippet", row)
        self.assertNotIn("name_surface", row)
        mention = package_text(text, [row])["mentions"][0]
        start = row["start_offset"]
        end = row["end_offset"]
        self.assertEqual(mention["exact_mention"], text[start:end])
        self.assertEqual(mention["name_surface"], text[start:end])
        self.assertIsNone(mention["title_surface"])
        self.assertEqual(
            mention["evidence_snippet"], evidence_line_snippet(text, start, end)
        )
        self.assertEqual(mention["evidence_snippet"], "Ada  Lovelace wrote.")
        self.assertNotIn("\r", mention["evidence_snippet"])
        self.assertIn("  ", mention["exact_mention"])

    def test_output_already_exists(self):
        text = "Ada Lovelace wrote the note."
        snapshot = snapshot_with(text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents = root / "documents.json"
            discovery_path = root / "discovery.json"
            geometry_path = root / "geometry.json"
            output = root / "proposals.json"
            documents.write_text(
                json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
            )
            file_hash = sha256_file(documents)
            selection = select_items(snapshot)
            selection["run_id"] = "run-1"
            selection["document_snapshot_file_sha256"] = file_hash
            selection["prior_checkpoint_logical_sha256"] = None
            selection["prior_checkpoint_file_sha256"] = None
            stamp_generated_artifact(selection, created_at=CREATED)
            write_json_atomic(discovery_path, selection)
            geometry_path.write_text(
                json.dumps(
                    {"mentions": [geometry_row(text, "Ada Lovelace")]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            argv = [
                "package-mention-proposals",
                "--documents",
                str(documents),
                "--discovery",
                str(discovery_path),
                "--geometry",
                str(geometry_path),
                "--run-id",
                "run-1",
                "--output",
                str(output),
            ]
            stdout = StringIO()
            with redirect_stdout(stdout):
                code = main(argv)
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            published = read_json(output)
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload["output_sha256"], sha256_file(output))
            self.assertEqual(
                payload["logical_content_sha256"], published["logical_content_sha256"]
            )
            self.assertEqual(payload["schema_version"], MENTION_PROPOSALS_SCHEMA)
            self.assertEqual(payload["mention_count"], 1)
            self.assertIsNone(published["identity_snapshot_file_sha256"])
            original = output.read_bytes()
            stderr = StringIO()
            with redirect_stderr(stderr):
                again = main(argv)
            self.assertEqual(again, 2)
            blocked = json.loads(stderr.getvalue())
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["error_code"], "ARTIFACT_EXISTS")
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(root.glob("*.tmp")), [])


def _span_mention(text: str, **overrides) -> dict:
    row = _mention(
        exact_mention=text,
        start_offset=0,
        end_offset=len(text),
        evidence_snippet=text,
        name_surface=text,
        name_start_offset=0,
        name_end_offset=len(text),
        title_surface=None,
        title_start_offset=None,
        title_end_offset=None,
        surface_kind=None,
    )
    row.update(overrides)
    return row


def _titled_mention(**overrides) -> dict:
    exact = "Colonel Dana"
    row = _mention(
        exact_mention=exact,
        start_offset=0,
        end_offset=len(exact),
        evidence_snippet=exact,
        name_surface="Dana",
        name_start_offset=8,
        name_end_offset=12,
        title_surface="Colonel",
        title_start_offset=0,
        title_end_offset=7,
        surface_kind=None,
    )
    row.update(overrides)
    return row


class MentionProposalContractTests(unittest.TestCase):
    def test_duplicate_outer_spans_fail_artifact_validation(self):
        first = _mention()
        second = _mention(evidence_snippet="Ada wrote again.")
        document = _artifact(detectors=[_detector()], mentions=[first, second])
        with self.assertRaises(ArtifactError) as caught:
            validate_artifact(document)
        self.assertIn("duplicate outer span", str(caught.exception))

    def test_line_break_limit_is_an_artifact_invariant(self):
        validate_artifact(
            _artifact(detectors=[_detector()], mentions=[_span_mention("Dana\nCohen")])
        )
        validate_artifact(
            _artifact(
                detectors=[_detector()], mentions=[_span_mention("Dana\r\nCohen")]
            )
        )
        for text in ("name\n\nname", "name\nmiddle\nname"):
            with self.subTest(text=text):
                document = _artifact(
                    detectors=[_detector()], mentions=[_span_mention(text)]
                )
                with self.assertRaises(ArtifactError) as caught:
                    validate_artifact(document)
                self.assertIn("at most one line break", str(caught.exception))

    def test_surface_kind_must_agree_with_the_title_slice(self):
        validate_artifact(
            _artifact(
                detectors=[_detector()],
                mentions=[_titled_mention(surface_kind="TITLE_AND_NAME")],
            )
        )
        validate_artifact(
            _artifact(
                detectors=[_detector()],
                mentions=[_titled_mention(surface_kind=None)],
            )
        )
        validate_artifact(
            _artifact(
                detectors=[_detector()],
                mentions=[_mention(surface_kind="FULL_NAME")],
            )
        )
        with self.assertRaises(ArtifactError) as missing_title:
            validate_artifact(
                _artifact(
                    detectors=[_detector()],
                    mentions=[_mention(surface_kind="TITLE_AND_NAME")],
                )
            )
        self.assertIn(
            "TITLE_AND_NAME requires a title slice", str(missing_title.exception)
        )
        for kind in ("FULL_NAME", "PARTIAL_NAME", "NICKNAME", "UNCERTAIN_PERSON_NAME"):
            with self.subTest(kind=kind):
                document = _artifact(
                    detectors=[_detector()],
                    mentions=[_titled_mention(surface_kind=kind)],
                )
                with self.assertRaises(ArtifactError) as caught:
                    validate_artifact(document)
                self.assertIn("contradicts a title slice", str(caught.exception))


class PackageMentionOrderAndKindTests(unittest.TestCase):
    def test_geometry_order_does_not_change_logical_identity(self):
        text = "Ada Lovelace wrote the note."
        ada = geometry_row(text, "Ada", kind="PARTIAL_NAME", include_kind=True)
        lovelace = geometry_row(
            text, "Lovelace", kind="PARTIAL_NAME", include_kind=True
        )
        forward = package_text(text, [ada, lovelace])
        backward = package_text(text, [lovelace, ada])
        self.assertEqual(
            [row["exact_mention"] for row in forward["mentions"]],
            ["Ada", "Lovelace"],
        )
        self.assertEqual(forward["mentions"], backward["mentions"])
        self.assertEqual(
            forward["logical_content_sha256"], backward["logical_content_sha256"]
        )
        self.assertEqual(
            forward["artifact_created_at"], backward["artifact_created_at"]
        )

    def test_cross_line_boundary(self):
        one_break = "Hello Dana\nCohen today"
        accepted = package_text(one_break, [geometry_row(one_break, "Dana\nCohen")])
        self.assertEqual(accepted["mentions"][0]["exact_mention"], "Dana\nCohen")
        crlf = "Hello Dana\r\nCohen today"
        preserved = package_text(crlf, [geometry_row(crlf, "Dana\r\nCohen")])
        self.assertEqual(preserved["mentions"][0]["exact_mention"], "Dana\r\nCohen")
        for text in ("name\n\nname", "name\nmiddle\nname"):
            with self.subTest(text=text):
                with self.assertRaises(ArtifactError) as caught:
                    package_text(text, [geometry_row(text, text)])
                self.assertIn("at most one line break", str(caught.exception))

    def test_surface_kind_follows_title_geometry(self):
        plain = "Ada Lovelace wrote the note."
        with self.assertRaises(ArtifactError) as missing:
            package_text(
                plain,
                [
                    geometry_row(
                        plain,
                        "Ada Lovelace",
                        kind="TITLE_AND_NAME",
                        include_kind=True,
                    )
                ],
            )
        self.assertIn("TITLE_AND_NAME requires a title slice", str(missing.exception))
        titled = "Colonel Dana wrote."
        unlabeled = package_text(
            titled,
            [geometry_row(titled, "Colonel Dana", name="Dana", title="Colonel")],
        )
        self.assertIsNone(unlabeled["mentions"][0]["surface_kind"])
        self.assertEqual(unlabeled["mentions"][0]["title_surface"], "Colonel")
        for kind in ("FULL_NAME", "PARTIAL_NAME", "NICKNAME", "UNCERTAIN_PERSON_NAME"):
            with self.subTest(kind=kind):
                with self.assertRaises(ArtifactError) as caught:
                    package_text(
                        titled,
                        [
                            geometry_row(
                                titled,
                                "Colonel Dana",
                                name="Dana",
                                title="Colonel",
                                kind=kind,
                                include_kind=True,
                            )
                        ],
                    )
                self.assertIn("contradicts a title slice", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
