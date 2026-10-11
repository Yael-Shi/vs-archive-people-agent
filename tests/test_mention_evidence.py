import json
import tempfile
import unittest
from pathlib import Path

from vs_archive_people.artifacts.io import (
    ArtifactError,
    ArtifactExistsError,
    read_json,
    sha256_file,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import stamp_generated_artifact
from vs_archive_people.artifacts.schema_validate import validate_artifact
from vs_archive_people.discovery.select import select_items
from vs_archive_people.evidence.mentions import (
    assign_mention_keys,
    validate_mention_evidence,
)
from vs_archive_people.evidence.package import build_evidence_package

FIXTURES = Path(__file__).parent / "fixtures"
DOCUMENTS = json.loads(
    (FIXTURES / "document_text_snapshot.json").read_text(encoding="utf-8")
)
PROPOSALS = json.loads(
    (FIXTURES / "mention_proposals.json").read_text(encoding="utf-8")
)["mentions"]


DOCUMENT_HASH = "a" * 64
DISCOVERY_HASH = "b" * 64


def discovery_for(snapshot):
    selection = select_items(snapshot)
    selection["run_id"] = "run-1"
    selection["document_snapshot_file_sha256"] = DOCUMENT_HASH
    selection["prior_checkpoint_logical_sha256"] = None
    selection["prior_checkpoint_file_sha256"] = None
    return stamp_generated_artifact(selection, created_at="2026-01-01T00:00:00Z")


class MentionEvidenceTests(unittest.TestCase):
    def test_crlf_and_repeated_spaces_are_whitespace_only(self):
        body = "Ada Lovelace\r\nwrote   the note."
        validate_mention_evidence(
            "Ada Lovelace",
            "Ada Lovelace wrote the note.",
            body,
        )

    def test_word_change_is_rejected(self):
        with self.assertRaises(ArtifactError):
            validate_mention_evidence(
                "Ada Lovelace",
                "Ida Lovelace wrote the note.",
                "Ada Lovelace wrote the note.",
            )

    def test_exact_mention_must_sit_inside_the_snippet(self):
        with self.assertRaises(ArtifactError):
            validate_mention_evidence(
                "Grace Hopper",
                "Ada Lovelace wrote the note.",
                "Ada Lovelace wrote the note. Grace Hopper served.",
            )

    def test_mention_keys_are_central_and_ignore_caller_keys(self):
        assigned = assign_mention_keys(
            [
                {
                    "archive_item_id": 11,
                    "evidence_source": "manual_text.body",
                    "start_offset": 0,
                    "end_offset": 12,
                    "exact_mention": "Grace Hopper",
                    "mention_key": "B15-M9999",
                },
                {
                    "archive_item_id": 10,
                    "evidence_source": "ocr_document.displayed_transcription.text",
                    "start_offset": 0,
                    "end_offset": 12,
                    "exact_mention": "Ada Lovelace",
                    "mention_key": "batch-key",
                },
            ]
        )
        self.assertEqual(
            [row["mention_key"] for row in assigned],
            ["M0001", "M0002"],
        )
        self.assertEqual(assigned[0]["archive_item_id"], 10)
        self.assertTrue(all(not row["mention_key"].startswith("B") for row in assigned))

    def test_package_binds_hashes_and_assigns_keys(self):
        selection = discovery_for(DOCUMENTS)
        for row in selection["selected_items"]:
            from vs_archive_people.evidence.text_bodies import displayed_text_sha256

            item = next(
                item
                for item in DOCUMENTS["items"]
                if item["archive_item_id"] == row["archive_item_id"]
            )
            self.assertEqual(row["displayed_text_sha256"], displayed_text_sha256(item))
        package = build_evidence_package(
            DOCUMENTS,
            selection,
            PROPOSALS,
            run_id="run-1",
            document_snapshot_file_sha256=DOCUMENT_HASH,
            discovery_file_sha256=DISCOVERY_HASH,
            mention_proposals_file_sha256="1" * 64,
        )
        self.assertEqual(package["schema_version"], "evidence-package-v1")
        self.assertEqual(package["document_snapshot_file_sha256"], DOCUMENT_HASH)
        self.assertFalse(package["production_write_performed"])
        self.assertEqual(
            [row["mention_key"] for row in package["mentions"]], ["M0001", "M0002"]
        )
        self.assertEqual(len(package["mentions"][0]["mention_fingerprint"]), 64)
        self.assertTrue(package["mentions"][0]["source_text_sha256"])
        titles = [item["output_metadata_only"]["title"] for item in package["items"]]
        self.assertIn("Metadata title must not be evidence", titles)
        for item in package["items"]:
            for body in item["texts"]:
                self.assertNotIn(item["output_metadata_only"]["title"], body["text"])

    def test_package_rejects_a_stale_discovery_hash(self):
        selection = discovery_for(DOCUMENTS)
        selection["document_snapshot_file_sha256"] = "c" * 64
        stamp_generated_artifact(selection, created_at="2026-01-01T00:00:00Z")
        with self.assertRaises(ArtifactError):
            build_evidence_package(
                DOCUMENTS,
                selection,
                PROPOSALS,
                run_id="run-1",
                document_snapshot_file_sha256=DOCUMENT_HASH,
                discovery_file_sha256=DISCOVERY_HASH,
                mention_proposals_file_sha256="1" * 64,
            )

    def test_malformed_artifact_with_a_known_schema_version_fails(self):
        with self.assertRaises(ArtifactError):
            validate_artifact(
                {
                    "schema_version": "document-text-snapshot-v1",
                    "exported_at": "2026-01-01T00:00:00+00:00",
                }
            )
        with self.assertRaises(ArtifactError):
            validate_artifact(
                {
                    "schema_version": "discovery-selection-v1",
                    "document_snapshot_file_sha256": "not-a-sha256",
                    "prior_checkpoint_logical_sha256": None,
                    "prior_checkpoint_file_sha256": None,
                    "selected_items": [],
                }
            )

    def test_atomic_write_refuses_overwrite_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            first = write_json_atomic(path, {"ok": True})
            self.assertEqual(first, sha256_file(path))
            self.assertEqual(read_json(path), {"ok": True})
            with self.assertRaises(ArtifactExistsError):
                write_json_atomic(path, {"ok": False})
            self.assertEqual(read_json(path), {"ok": True})
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_proposal_file_hash_does_not_change_logical_identity(self):
        selection = discovery_for(DOCUMENTS)
        with tempfile.TemporaryDirectory() as tmp:
            compact = Path(tmp) / "compact.json"
            pretty = Path(tmp) / "pretty.json"
            payload = {"mentions": PROPOSALS}
            compact.write_text(
                json.dumps(payload, separators=(",", ":")), encoding="utf-8"
            )
            pretty.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            compact_hash = sha256_file(compact)
            pretty_hash = sha256_file(pretty)
            self.assertNotEqual(compact_hash, pretty_hash)
            shared = {
                "run_id": "run-1",
                "document_snapshot_file_sha256": DOCUMENT_HASH,
                "discovery_file_sha256": DISCOVERY_HASH,
                "created_at": "2026-01-01T00:00:00Z",
            }
            from_compact = build_evidence_package(
                DOCUMENTS,
                selection,
                PROPOSALS,
                mention_proposals_file_sha256=compact_hash,
                **shared,
            )
            from_pretty = build_evidence_package(
                DOCUMENTS,
                selection,
                PROPOSALS,
                mention_proposals_file_sha256=pretty_hash,
                **shared,
            )
        self.assertEqual(from_compact["mentions"], from_pretty["mentions"])
        self.assertEqual(
            from_compact["logical_content_sha256"],
            from_pretty["logical_content_sha256"],
        )
        self.assertEqual(from_compact["mention_proposals_file_sha256"], compact_hash)
        self.assertEqual(from_pretty["mention_proposals_file_sha256"], pretty_hash)


class NormalizeWhitespaceTests(unittest.TestCase):
    def test_normalizes_empty_and_unicode_whitespace(self):
        from vs_archive_people.evidence.mentions import normalize_ws

        self.assertEqual(normalize_ws(None), "")
        self.assertEqual(normalize_ws("\u00a0Ada\tLovelace\n"), "Ada Lovelace")


if __name__ == "__main__":
    unittest.main()
