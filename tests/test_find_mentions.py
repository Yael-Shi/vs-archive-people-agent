"""Official find-mentions. No live Gemini calls."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vs_archive_people.artifacts.io import (
    ArtifactError,
    read_json,
    sha256_file,
    sha256_json,
    write_json_atomic,
)
from vs_archive_people.artifacts.provenance import (
    logical_body,
    stamp_generated_artifact,
)
from vs_archive_people.artifacts.schema_validate import RUN_STEPS, validate_artifact
from vs_archive_people.cli import main
from vs_archive_people.discovery.select import select_items
from vs_archive_people.find_mentions.errors import (
    FindMentionsConfigurationError,
    FindMentionsProviderError,
)
from vs_archive_people.find_mentions.gemini import (
    GeminiSemanticDetector,
    build_client,
    build_generate_config,
    prompt_sha256,
)
from vs_archive_people.find_mentions.proposals import SemanticProposal
from vs_archive_people.find_mentions.run import (
    build_mention_findings,
    publish_mention_findings,
)
from test_mention_evidence import DOCUMENT_HASH, DISCOVERY_HASH, discovery_for

FIXTURES = Path(__file__).parent / "fixtures"
IDENTITY = json.loads((FIXTURES / "identity_snapshot.json").read_text(encoding="utf-8"))
CREATED = "2026-01-01T00:00:00Z"
SOURCE = "ocr_document.displayed_transcription.text"
IDENTITY_HASH = "c" * 64


def cloned_identity():
    return json.loads(json.dumps(IDENTITY))


def add_person(identity: dict, person_id: int, name: str) -> dict:
    identity["models"]["Person"]["rows"].append(
        {"id": person_id, "name": name, "honorific": "", "biography": ""}
    )
    return identity


class ScriptedDetector:
    def __init__(self, batches):
        self.batches = [list(batch) for batch in batches]
        self.calls = 0

    def detect(self, segments):
        self.calls += 1
        if not self.batches:
            raise AssertionError("unexpected detect call")
        batch = self.batches.pop(0)
        if batch and isinstance(batch[0], BaseException):
            raise batch[0]
        return batch


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


def propose(surface, kind="FULL_NAME", **kwargs):
    return SemanticProposal(
        "S0001", surface, kind, kwargs.pop("occurrence", None), **kwargs
    )


def findings(*texts, proposals=None, batches=None, identity=None):
    if batches is None:
        batches = [proposals or []]
        if len(texts) > 1:
            batches = list(batches) + [[] for _ in range(len(texts) - len(batches))]
    snapshot = snapshot_with(*texts)
    proposal, diagnostics = build_mention_findings(
        snapshot,
        discovery_for(snapshot),
        identity or cloned_identity(),
        run_id="run-1",
        document_snapshot_file_sha256=DOCUMENT_HASH,
        discovery_file_sha256=DISCOVERY_HASH,
        identity_snapshot_file_sha256=IDENTITY_HASH,
        detector=ScriptedDetector(batches),
        created_at=CREATED,
    )
    return proposal, diagnostics


def mentions_of(proposal):
    return proposal["mentions"]


def by_surface(proposal):
    return {row["exact_mention"]: row for row in proposal["mentions"]}


class OfficialFinderTests(unittest.TestCase):
    def test_semantic_only_person_mention(self):
        text = "Noam Levi wrote the note."
        proposal, diagnostics = findings(text, proposals=[propose("Noam Levi")])
        row = by_surface(proposal)["Noam Levi"]
        self.assertEqual(row["detector_ids"], ["semantic-surface"])
        self.assertEqual(row["surface_kind"], "FULL_NAME")
        self.assertIsNone(row["known_name_match_quality"])
        self.assertIsNone(row["title_surface"])
        self.assertEqual(text[row["start_offset"] : row["end_offset"]], "Noam Levi")
        self.assertEqual(row["name_surface"], "Noam Levi")
        self.assertNotIn("person_id", row)
        self.assertEqual(diagnostics["rejected"], [])
        validate_artifact(proposal)

    def test_known_name_only_mention(self):
        text = "Ada Lovelace wrote the note."
        proposal, _diagnostics = findings(text, proposals=[])
        row = by_surface(proposal)["Ada Lovelace"]
        self.assertEqual(row["detector_ids"], ["known-name"])
        self.assertIsNone(row["surface_kind"])
        self.assertEqual(row["known_name_match_quality"], "EXACT_BOUNDARY")
        self.assertNotIn("person_id", row)

    def test_same_span_unions_detector_ids(self):
        text = "Ada Lovelace wrote the note."
        proposal, _diagnostics = findings(text, proposals=[propose("Ada Lovelace")])
        row = by_surface(proposal)["Ada Lovelace"]
        self.assertEqual(row["detector_ids"], ["semantic-surface", "known-name"])
        self.assertEqual(row["surface_kind"], "FULL_NAME")
        self.assertEqual(row["known_name_match_quality"], "EXACT_BOUNDARY")

    def test_overlapping_distinct_spans_are_kept(self):
        text = "Dana Cohen arrived."
        proposal, _diagnostics = findings(text, proposals=[])
        self.assertEqual(
            [row["exact_mention"] for row in mentions_of(proposal)],
            ["Dana", "Dana Cohen"],
        )
        self.assertEqual(
            [row["known_name_match_quality"] for row in mentions_of(proposal)],
            ["EXACT_BOUNDARY", "EXACT_BOUNDARY"],
        )

    def test_repeated_surface_requires_occurrence(self):
        text = "Noam met Noam."
        first = text.index("Noam")
        second = text.index("Noam", first + 1)
        missing, rejected = findings(text, proposals=[propose("Noam")])
        self.assertEqual(mentions_of(missing), [])
        self.assertEqual(
            rejected["rejected"][0]["reason"], "AMBIGUOUS_REPEATED_SURFACE"
        )
        self.assertNotIn("start_offset", rejected["rejected"][0])
        found, clear = findings(text, proposals=[propose("Noam", occurrence=2)])
        self.assertEqual(mentions_of(found)[0]["start_offset"], second)
        self.assertEqual(clear["rejected"], [])

    def test_cross_line_mention_keeps_the_break(self):
        text = "והאלוף\nבנימין גיבלי"
        proposal, diagnostics = findings(
            text,
            proposals=[
                propose(
                    text,
                    "TITLE_AND_NAME",
                    name_surface="בנימין גיבלי",
                    title_surface="והאלוף",
                    end_segment_id="S0002",
                )
            ],
        )
        row = mentions_of(proposal)[0]
        self.assertEqual(row["exact_mention"], text)
        self.assertIn("\n", row["exact_mention"])
        self.assertEqual(row["title_surface"], "והאלוף")
        self.assertEqual(row["name_surface"], "בנימין גיבלי")
        self.assertEqual(diagnostics["rejected"], [])

    def test_crlf_is_preserved(self):
        text = "שמואל\r\nעזר"
        proposal, _diagnostics = findings(
            text,
            proposals=[
                propose(
                    text,
                    name_surface=None,
                    end_segment_id="S0002",
                )
            ],
        )
        row = mentions_of(proposal)[0]
        self.assertEqual(row["exact_mention"], text)
        self.assertIn("\r\n", row["exact_mention"])
        self.assertIn("\r\n", row["evidence_snippet"])

    def test_hebrew_safe_prefix_from_semantic_and_known_name(self):
        semantic, _diagnostics = findings(
            "לאהרוני wrote.",
            proposals=[propose("לאהרוני", "PARTIAL_NAME", name_surface="אהרוני")],
        )
        peeled = mentions_of(semantic)[0]
        self.assertEqual(peeled["exact_mention"], "לאהרוני")
        self.assertEqual(peeled["name_surface"], "אהרוני")
        identity = add_person(cloned_identity(), 6, "משה")
        known, notes = findings("ומשה,", proposals=[], identity=identity)
        row = mentions_of(known)[0]
        self.assertEqual(row["exact_mention"], "ומשה")
        self.assertEqual(row["name_surface"], "משה")
        self.assertEqual(row["known_name_match_quality"], "PREFIXED")
        unsafe, unsafe_notes = findings("המשה", proposals=[], identity=identity)
        self.assertEqual(mentions_of(unsafe), [])
        self.assertEqual(unsafe_notes["rejected"][0]["reason"], "UNSAFE_NAME_SPLIT")
        vocalized, vocalized_notes = findings("בְמשה", proposals=[], identity=identity)
        self.assertEqual(mentions_of(vocalized), [])
        self.assertEqual(vocalized_notes["rejected"][0]["reason"], "UNSAFE_NAME_SPLIT")

    def test_title_and_name_and_punctuation_gap(self):
        titled = 'ד"ר משה מרזוק'
        proposal, _diagnostics = findings(
            titled,
            proposals=[
                propose(
                    titled,
                    "TITLE_AND_NAME",
                    name_surface="משה מרזוק",
                    title_surface='ד"ר',
                )
            ],
        )
        row = mentions_of(proposal)[0]
        self.assertEqual(row["surface_kind"], "TITLE_AND_NAME")
        self.assertEqual(row["title_surface"], 'ד"ר')
        self.assertEqual(row["name_surface"], "משה מרזוק")
        gapped = 'ד"ר, משה מרזוק'
        gap, _notes = findings(
            gapped,
            proposals=[
                propose(
                    gapped,
                    "TITLE_AND_NAME",
                    name_surface="משה מרזוק",
                    title_surface='ד"ר',
                )
            ],
        )
        self.assertEqual(mentions_of(gap)[0]["exact_mention"], gapped)
        self.assertEqual(mentions_of(gap)[0]["name_surface"], "משה מרזוק")

    def test_invalid_title_split_is_diagnostic_not_a_mention(self):
        full = 'ד"ר משה מרזוק'
        proposal, diagnostics = findings(
            full,
            proposals=[propose(full, "TITLE_AND_NAME")],
        )
        self.assertEqual(mentions_of(proposal), [])
        self.assertEqual(diagnostics["rejected"][0]["reason"], "TITLE_SPLIT_UNRESOLVED")
        self.assertEqual(diagnostics["rejected"][0]["surface"], full)
        self.assertNotIn(full, [row["exact_mention"] for row in mentions_of(proposal)])

    def test_bare_title_is_rejected_without_a_vocabulary(self):
        proposal, diagnostics = findings("הנשיא דיבר.", proposals=[propose("הנשיא")])
        self.assertEqual(mentions_of(proposal), [])
        self.assertEqual(
            diagnostics["suppressed"][0]["reason"], "TITLE_OR_ROLE_WITHOUT_NAME"
        )

    def test_non_person_suppresses_one_occurrence_and_a_contained_known_name(self):
        text = "אוניברסיטת פארוק נפתחה. פארוק נאם."
        identity = add_person(cloned_identity(), 6, "פארוק")
        phrase = "אוניברסיטת פארוק"
        contained = text.index("פארוק")
        person = text.index("פארוק נאם")
        proposal, diagnostics = findings(
            text,
            proposals=[
                propose(phrase, None, referent="NON_PERSON"),
                propose("פארוק", occurrence=2, referent="PERSON"),
            ],
            identity=identity,
        )
        self.assertEqual(
            [row["start_offset"] for row in mentions_of(proposal)],
            [person],
        )
        self.assertNotIn(
            phrase, [row["exact_mention"] for row in mentions_of(proposal)]
        )
        self.assertNotIn(
            contained, [row["start_offset"] for row in mentions_of(proposal)]
        )
        reasons = [row["reason"] for row in diagnostics["suppressed"]]
        self.assertIn("PLACE_OR_INSTITUTION_CONTEXT", reasons)
        self.assertNotIn(
            "פארוק",
            [
                row["exact_mention"]
                for row in mentions_of(proposal)
                if row["start_offset"] == contained
            ],
        )

    def test_embedded_known_name_is_diagnostic_only(self):
        proposal, diagnostics = findings("ADana", proposals=[])
        self.assertEqual(mentions_of(proposal), [])
        self.assertEqual(diagnostics["suppressed"][0]["reason"], "EMBEDDED_KNOWN_NAME")
        self.assertEqual(diagnostics["suppressed"][0]["surface"], "Dana")

    def test_titled_outer_absorbs_one_exact_inner_name(self):
        full = 'ד"ר משה מרזוק'
        name = "משה מרזוק"
        text = f"{full} בא. {name} הלך."
        identity = add_person(cloned_identity(), 6, name)
        proposal, diagnostics = findings(
            text,
            proposals=[
                propose(
                    full,
                    "TITLE_AND_NAME",
                    name_surface=name,
                    title_surface='ד"ר',
                )
            ],
            identity=identity,
        )
        outer = by_surface(proposal)[full]
        self.assertEqual(outer["name_surface"], name)
        self.assertEqual(outer["detector_ids"], ["semantic-surface", "known-name"])
        self.assertEqual(outer["known_name_match_quality"], "EXACT_BOUNDARY")
        published_name_offsets = {
            (row["start_offset"], row["end_offset"])
            for row in mentions_of(proposal)
            if row["exact_mention"] == name
        }
        self.assertNotIn(
            (outer["name_start_offset"], outer["name_end_offset"]),
            published_name_offsets,
        )
        self.assertIn(
            text.index(f"{name} הלך"),
            [row["start_offset"] for row in mentions_of(proposal)],
        )
        self.assertEqual(diagnostics["conflicts"], [])

    def test_ambiguous_titled_outers_keep_spans(self):
        text = "Sir Colonel Dana"
        proposal, diagnostics = findings(
            text,
            proposals=[
                propose(
                    text,
                    "TITLE_AND_NAME",
                    name_surface="Dana",
                    title_surface="Sir Colonel",
                ),
                propose(
                    "Colonel Dana",
                    "TITLE_AND_NAME",
                    name_surface="Dana",
                    title_surface="Colonel",
                ),
                propose("Dana", "PARTIAL_NAME"),
            ],
        )
        self.assertEqual(
            [row["exact_mention"] for row in mentions_of(proposal)],
            ["Sir Colonel Dana", "Colonel Dana", "Dana"],
        )
        self.assertEqual(diagnostics["conflicts"][0]["reason"], "ABSORB_AMBIGUOUS")
        self.assertEqual(diagnostics["conflicts"][0]["surface"], "Dana")

    def test_conflicting_splits_publish_neither(self):
        text = "לאהרוני wrote."
        proposal, diagnostics = findings(
            text,
            proposals=[
                propose("לאהרוני", "FULL_NAME"),
                propose("לאהרוני", "PARTIAL_NAME", name_surface="אהרוני"),
            ],
        )
        self.assertEqual(mentions_of(proposal), [])
        self.assertEqual(diagnostics["conflicts"][0]["reason"], "SPLIT_CONFLICT")
        self.assertEqual(diagnostics["conflicts"][0]["surface"], "לאהרוני")

    def test_mentions_follow_source_order_and_validate(self):
        text = "Yael met Noam."
        proposal, _diagnostics = findings(
            text,
            proposals=[propose("Noam"), propose("Yael")],
        )
        self.assertEqual(
            [row["exact_mention"] for row in mentions_of(proposal)],
            ["Yael", "Noam"],
        )
        ordered, _notes = findings(
            "Noam wrote.",
            "Yael wrote.",
            batches=[[propose("Noam")], [propose("Yael")]],
        )
        self.assertEqual(
            [row["archive_item_id"] for row in mentions_of(ordered)],
            [10, 11],
        )
        validate_artifact(ordered)
        self.assertEqual(
            [row["id"] for row in ordered["detectors"]],
            ["semantic-surface", "known-name"],
        )
        self.assertEqual(
            ordered["detectors"][0]["reproducibility"]["prompt_sha256"],
            prompt_sha256(),
        )
        self.assertEqual(ordered["detectors"][1]["implementation"], "exact-literal")

    def test_registry_has_no_provider_payload_on_mention_rows(self):
        proposal, _diagnostics = findings(
            "Noam Levi wrote.", proposals=[propose("Noam Levi")]
        )
        row = mentions_of(proposal)[0]
        self.assertEqual(
            set(row),
            {
                "archive_item_id",
                "evidence_source",
                "exact_mention",
                "start_offset",
                "end_offset",
                "evidence_snippet",
                "name_surface",
                "name_start_offset",
                "name_end_offset",
                "title_surface",
                "title_start_offset",
                "title_end_offset",
                "surface_kind",
                "detector_ids",
                "known_name_match_quality",
            },
        )


class WholeSelectionTests(unittest.TestCase):
    def test_one_empty_body_does_not_fail(self):
        proposal, _diagnostics = findings(
            "Noam Levi wrote.",
            "nothing to find here",
            batches=[[propose("Noam Levi")], []],
        )
        self.assertEqual(len(mentions_of(proposal)), 1)
        self.assertEqual(mentions_of(proposal)[0]["archive_item_id"], 10)
        empty, _notes = findings("nothing to find here", proposals=[])
        self.assertEqual(mentions_of(empty), [])
        self.assertEqual(len(empty["detectors"]), 2)
        validate_artifact(empty)

    def test_provider_failure_publishes_no_proposal(self):
        snapshot = snapshot_with("Noam Levi wrote.", "Yael wrote.")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "proposals.json"
            diagnostics = Path(tmp) / "diagnostics.json"
            detector = ScriptedDetector(
                [[propose("Noam Levi")], [FindMentionsProviderError("provider down")]]
            )
            with self.assertRaises(FindMentionsProviderError):
                publish_mention_findings(
                    snapshot,
                    discovery_for(snapshot),
                    cloned_identity(),
                    run_id="run-1",
                    document_snapshot_file_sha256=DOCUMENT_HASH,
                    discovery_file_sha256=DISCOVERY_HASH,
                    identity_snapshot_file_sha256=IDENTITY_HASH,
                    detector=detector,
                    proposal_output=output,
                    diagnostics_output=diagnostics,
                    created_at=CREATED,
                )
            self.assertEqual(detector.calls, 2)
            self.assertFalse(output.exists())
            self.assertFalse(diagnostics.exists())

    def test_missing_api_key_fails_before_client_construction(self):
        env = {
            key: value for key, value in os.environ.items() if key != "GEMINI_API_KEY"
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "vs_archive_people.find_mentions.gemini.build_client",
                side_effect=AssertionError("client must not be built"),
            ):
                with self.assertRaises(FindMentionsConfigurationError):
                    GeminiSemanticDetector()
                with self.assertRaises(FindMentionsConfigurationError):
                    GeminiSemanticDetector(api_key="  ")

    def test_sdk_is_configured_for_three_attempts_and_finder_does_not_retry(self):
        client = build_client("test-key")
        options = client._api_client._http_options
        self.assertEqual(options.timeout, 120_000)
        self.assertEqual(options.retry_options.attempts, 3)
        config = build_generate_config()
        self.assertIsNone(config.temperature)
        self.assertIsNone(config.tools)
        root = Path(__file__).parents[1]
        source = (root / "src/vs_archive_people/find_mentions/gemini.py").read_text(
            encoding="utf-8"
        )
        finder = (root / "src/vs_archive_people/find_mentions/run.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(source.count("client.models.generate_content("), 1)
        self.assertNotIn("time.sleep", source)
        self.assertNotIn("generate_content", finder)
        self.assertNotIn("time.sleep", finder)
        detector = ScriptedDetector([[FindMentionsProviderError("once")]])
        with self.assertRaises(FindMentionsProviderError):
            findings("Noam Levi wrote.", batches=[[FindMentionsProviderError("once")]])
        self.assertEqual(
            ScriptedDetector([[FindMentionsProviderError("once")]])
            .batches[0][0]
            .error_code,
            "FIND_MENTIONS_PROVIDER",
        )
        self.assertEqual(detector.calls, 0)

    def test_unacceptable_finish_reason_fails_the_run(self):
        models = _FakeModels(
            text=json.dumps({"candidates": []}),
            finish_reason="MAX_TOKENS",
        )
        from vs_archive_people.find_mentions.segment import segment_body

        detector = GeminiSemanticDetector(client=SimpleNamespace(models=models))
        with self.assertRaises(FindMentionsProviderError) as raised:
            detector.detect(segment_body("Noam"))
        self.assertEqual(raised.exception.error_code, "FIND_MENTIONS_PROVIDER")
        self.assertIn("MAX_TOKENS", str(raised.exception))
        self.assertNotIn("Noam", str(raised.exception))
        self.assertEqual(models.calls, 1)

    def test_programmer_errors_propagate(self):
        from vs_archive_people.find_mentions.segment import segment_body

        detector = GeminiSemanticDetector(
            client=SimpleNamespace(models=_RaisingModels(RuntimeError("bug")))
        )
        with self.assertRaises(RuntimeError):
            detector.detect(segment_body("Noam"))
        snapshot = snapshot_with("Noam")
        with self.assertRaises(RuntimeError):
            build_mention_findings(
                snapshot,
                discovery_for(snapshot),
                cloned_identity(),
                run_id="run-1",
                document_snapshot_file_sha256=DOCUMENT_HASH,
                discovery_file_sha256=DISCOVERY_HASH,
                identity_snapshot_file_sha256=IDENTITY_HASH,
                detector=_Boom(),
                created_at=CREATED,
            )


class _RaisingModels:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        raise self.exc


class _FakeModels:
    def __init__(self, text, finish_reason="STOP"):
        self.text = text
        self.finish_reason = finish_reason
        self.calls = 0

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        return SimpleNamespace(
            text=self.text,
            candidates=[SimpleNamespace(finish_reason=self.finish_reason)],
            prompt_feedback=None,
        )


class _Boom:
    def detect(self, segments):
        raise RuntimeError("bug")


class DiagnosticsTests(unittest.TestCase):
    def test_diagnostic_rows_are_absent_from_mentions_and_hashes_bind(self):
        text = "ADana met Ada Lovelace. הנשיא."
        snapshot = snapshot_with(text)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents, discovery_path, identity_path = _write_inputs(
                root, snapshot, cloned_identity()
            )
            output = root / "proposals.json"
            diagnostics_path = root / "diagnostics.json"
            published = publish_mention_findings(
                read_json(documents),
                read_json(discovery_path),
                read_json(identity_path),
                run_id="run-1",
                document_snapshot_file_sha256=sha256_file(documents),
                discovery_file_sha256=sha256_file(discovery_path),
                identity_snapshot_file_sha256=sha256_file(identity_path),
                detector=ScriptedDetector(
                    [[propose("הנשיא"), propose("Ada Lovelace")]]
                ),
                proposal_output=output,
                diagnostics_output=diagnostics_path,
                created_at=CREATED,
            )
            proposal = read_json(output)
            diagnostics = read_json(diagnostics_path)
            surfaces = [row["exact_mention"] for row in proposal["mentions"]]
            self.assertIn("Ada Lovelace", surfaces)
            self.assertNotIn("Dana", surfaces)
            self.assertNotIn("הנשיא", surfaces)
            self.assertEqual(
                diagnostics["mention_proposals_logical_sha256"],
                proposal["logical_content_sha256"],
            )
            self.assertEqual(
                diagnostics["mention_proposals_file_sha256"],
                sha256_file(output),
            )
            self.assertEqual(
                published.proposal_logical_sha256, proposal["logical_content_sha256"]
            )
            self.assertNotIn("mention_proposals_file_sha256", logical_body(diagnostics))
            self.assertIn("mention_proposals_logical_sha256", logical_body(diagnostics))
            original = proposal["logical_content_sha256"]
            diagnostics["rejected"].append(
                {
                    "archive_item_id": 10,
                    "evidence_source": SOURCE,
                    "surface": "extra",
                    "reason": "EMPTY_SURFACE",
                    "segment_id": None,
                }
            )
            self.assertEqual(proposal["logical_content_sha256"], original)
            self.assertEqual(sha256_json(logical_body(proposal)), original)
            self.assertNotEqual(
                sha256_json(logical_body(diagnostics)),
                diagnostics["logical_content_sha256"],
            )

    def test_freeze_is_not_called(self):
        snapshot = snapshot_with("Noam Levi wrote.")
        with patch(
            "vs_archive_people.evidence.package.build_evidence_package"
        ) as freeze:
            findings("Noam Levi wrote.", proposals=[propose("Noam Levi")])
            freeze.assert_not_called()
        self.assertEqual(
            RUN_STEPS, ("discover", "freeze-evidence", "resolve", "review")
        )
        self.assertIs(snapshot["schema_version"], "document-text-snapshot-v1")

    def test_existing_proposal_fails_before_publication(self):
        snapshot = snapshot_with("Noam Levi wrote.")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents, discovery_path, identity_path = _write_inputs(
                root, snapshot, cloned_identity()
            )
            output = root / "proposals.json"
            diagnostics_path = root / "diagnostics.json"
            output.write_text("already", encoding="utf-8")
            code, stdout, stderr = _invoke(
                documents, discovery_path, identity_path, output, diagnostics_path
            )
            self.assertEqual(code, 2)
            blocked = json.loads(stderr)
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["error_code"], "ARTIFACT_EXISTS")
            self.assertEqual(output.read_text(encoding="utf-8"), "already")
            self.assertFalse(diagnostics_path.exists())
            self.assertEqual(stdout, "")

    def test_existing_diagnostics_keeps_the_proposal(self):
        snapshot = snapshot_with("Noam Levi wrote.")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents, discovery_path, identity_path = _write_inputs(
                root, snapshot, cloned_identity()
            )
            output = root / "proposals.json"
            diagnostics_path = root / "diagnostics.json"
            diagnostics_path.write_text("sidecar-already", encoding="utf-8")
            code, stdout, stderr = _invoke(
                documents, discovery_path, identity_path, output, diagnostics_path
            )
            self.assertEqual(code, 2)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "PROPOSAL_PUBLISHED")
            self.assertEqual(payload["error_code"], "DIAGNOSTICS_PUBLICATION_FAILED")
            self.assertFalse(payload["diagnostics_published"])
            self.assertEqual(payload["proposal_sha256"], sha256_file(output))
            self.assertEqual(
                payload["proposal_logical_sha256"],
                read_json(output)["logical_content_sha256"],
            )
            self.assertEqual(
                diagnostics_path.read_text(encoding="utf-8"), "sidecar-already"
            )
            validate_artifact(read_json(output))

    def test_diagnostics_build_failure_after_publication_keeps_the_proposal(self):
        snapshot = snapshot_with("Noam Levi wrote.")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents, discovery_path, identity_path = _write_inputs(
                root, snapshot, cloned_identity()
            )
            output = root / "proposals.json"
            diagnostics_path = root / "diagnostics.json"
            with patch(
                "vs_archive_people.find_mentions.run.diagnostics_artifact",
                side_effect=ArtifactError("diagnostics schema failed"),
            ):
                code, stdout, stderr = _invoke(
                    documents, discovery_path, identity_path, output, diagnostics_path
                )
            self.assertEqual(code, 2)
            self.assertEqual(stderr, "")
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "PROPOSAL_PUBLISHED")
            self.assertEqual(payload["error_code"], "DIAGNOSTICS_PUBLICATION_FAILED")
            self.assertFalse(payload["diagnostics_published"])
            self.assertEqual(payload["proposal_output"], str(output))
            self.assertTrue(output.is_file())
            self.assertFalse(diagnostics_path.exists())
            proposal = read_json(output)
            validate_artifact(proposal)
            self.assertEqual(payload["proposal_sha256"], sha256_file(output))
            self.assertEqual(
                payload["proposal_logical_sha256"], proposal["logical_content_sha256"]
            )


def _write_inputs(root: Path, snapshot: dict, identity: dict):
    documents = root / "documents.json"
    identity_path = root / "identity.json"
    discovery_path = root / "discovery.json"
    documents.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    identity_path.write_text(json.dumps(identity, ensure_ascii=False), encoding="utf-8")
    selection = select_items(snapshot)
    selection["run_id"] = "run-1"
    selection["document_snapshot_file_sha256"] = sha256_file(documents)
    selection["prior_checkpoint_logical_sha256"] = None
    selection["prior_checkpoint_file_sha256"] = None
    stamp_generated_artifact(selection, created_at=CREATED)
    write_json_atomic(discovery_path, selection)
    return documents, discovery_path, identity_path


def _invoke(documents, discovery_path, identity_path, output, diagnostics_path):
    stdout = StringIO()
    stderr = StringIO()
    with patch(
        "vs_archive_people.cli.GeminiSemanticDetector",
        return_value=ScriptedDetector([[propose("Noam Levi")]]),
    ):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                [
                    "find-mentions",
                    "--documents",
                    str(documents),
                    "--discovery",
                    str(discovery_path),
                    "--identity",
                    str(identity_path),
                    "--run-id",
                    "run-1",
                    "--output",
                    str(output),
                    "--diagnostics",
                    str(diagnostics_path),
                ]
            )
    return code, stdout.getvalue(), stderr.getvalue()


class FinishReasonCallTests(unittest.TestCase):
    def test_detector_parses_without_a_network_call(self):
        models = _FakeModels(
            text=json.dumps(
                {"candidates": [{"segment_id": "S0001", "surface": "Noam"}]}
            )
        )
        # Empty segment list performs no call. A real segment does.
        from vs_archive_people.find_mentions.segment import segment_body

        detector = GeminiSemanticDetector(client=SimpleNamespace(models=models))
        found = detector.detect(segment_body("Noam"))
        self.assertEqual(found[0].surface, "Noam")
        self.assertEqual(models.calls, 1)
        self.assertNotIn("api_key", str(found[0]))


_BOUNDARY = "שמואל\nעזר"


class _SequenceModels:
    def __init__(self, responder):
        self.responder = responder
        self.calls = 0
        self.contents: list[str] = []

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        self.contents.append(contents)
        result = self.responder(contents)
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(
            text=result,
            candidates=[SimpleNamespace(finish_reason="STOP")],
            prompt_feedback=None,
        )


def _segment_ids(contents: str) -> list[str]:
    payload = json.loads(contents.split("\n", 1)[1])
    return [row["segment_id"] for row in payload]


class FinderIndependenceTests(unittest.TestCase):
    def test_find_mentions_does_not_import_the_pilot(self):
        root = Path(__file__).parents[1] / "src/vs_archive_people/find_mentions"
        for path in sorted(root.rglob("*.py")):
            self.assertNotIn(
                "mention_pilot",
                path.read_text(encoding="utf-8"),
                str(path),
            )


class SemanticBatchBoundaryTests(unittest.TestCase):
    def test_cross_line_mention_is_detected_across_the_batch_boundary(self):
        def respond(contents: str) -> str:
            if _segment_ids(contents) == ["S0001", "S0002"]:
                return json.dumps(
                    {
                        "candidates": [
                            {
                                "segment_id": "S0001",
                                "end_segment_id": "S0002",
                                "surface": _BOUNDARY,
                                "kind": "FULL_NAME",
                            }
                        ]
                    }
                )
            return json.dumps({"candidates": []})

        models = _SequenceModels(respond)
        proposal, diagnostics = _batched(_BOUNDARY, models)
        self.assertEqual(_segment_ids(models.contents[0]), ["S0001"])
        self.assertEqual(_segment_ids(models.contents[1]), ["S0001", "S0002"])
        self.assertEqual(mentions_of(proposal)[0]["exact_mention"], _BOUNDARY)
        self.assertIn("\n", mentions_of(proposal)[0]["exact_mention"])
        self.assertEqual(diagnostics["rejected"], [])

    def test_overlap_does_not_duplicate_a_proposal_row(self):
        emitted = {"count": 0}

        def respond(contents: str) -> str:
            if "S0001" not in _segment_ids(contents):
                return json.dumps({"candidates": []})
            emitted["count"] += 1
            return json.dumps(
                {
                    "candidates": [
                        {
                            "segment_id": "S0001",
                            "surface": "שמואל",
                            "kind": "FULL_NAME",
                        }
                    ]
                }
            )

        models = _SequenceModels(respond)
        proposal, _diagnostics = _batched(_BOUNDARY, models)
        self.assertEqual(emitted["count"], 2)
        self.assertEqual(models.calls, 2)
        rows = mentions_of(proposal)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exact_mention"], "שמואל")
        self.assertEqual(rows[0]["detector_ids"], ["semantic-surface"])

    def test_later_overlapped_batch_failure_publishes_no_proposal(self):
        from google.genai.errors import ClientError

        def respond(contents: str):
            if _segment_ids(contents) == ["S0001", "S0002"]:
                return ClientError(
                    503,
                    {"error": {"status": "UNAVAILABLE", "message": "later batch"}},
                )
            return json.dumps({"candidates": []})

        models = _SequenceModels(respond)
        snapshot = snapshot_with(_BOUNDARY)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "proposals.json"
            diagnostics = Path(tmp) / "diagnostics.json"
            with patch(
                "vs_archive_people.find_mentions.gemini.MAX_BATCH_CODE_POINTS",
                len("שמואל"),
            ):
                with self.assertRaises(FindMentionsProviderError) as raised:
                    publish_mention_findings(
                        snapshot,
                        discovery_for(snapshot),
                        cloned_identity(),
                        run_id="run-1",
                        document_snapshot_file_sha256=DOCUMENT_HASH,
                        discovery_file_sha256=DISCOVERY_HASH,
                        identity_snapshot_file_sha256=IDENTITY_HASH,
                        detector=GeminiSemanticDetector(
                            client=SimpleNamespace(models=models)
                        ),
                        proposal_output=output,
                        diagnostics_output=diagnostics,
                        created_at=CREATED,
                    )
            self.assertEqual(raised.exception.error_code, "FIND_MENTIONS_PROVIDER")
            self.assertNotIn("שמואל", str(raised.exception))
            self.assertEqual(_segment_ids(models.contents[1]), ["S0001", "S0002"])
            self.assertFalse(output.exists())
            self.assertFalse(diagnostics.exists())


def _batched(text: str, models: _SequenceModels):
    snapshot = snapshot_with(text)
    with patch(
        "vs_archive_people.find_mentions.gemini.MAX_BATCH_CODE_POINTS",
        len("שמואל"),
    ):
        return build_mention_findings(
            snapshot,
            discovery_for(snapshot),
            cloned_identity(),
            run_id="run-1",
            document_snapshot_file_sha256=DOCUMENT_HASH,
            discovery_file_sha256=DISCOVERY_HASH,
            identity_snapshot_file_sha256=IDENTITY_HASH,
            detector=GeminiSemanticDetector(client=SimpleNamespace(models=models)),
            created_at=CREATED,
        )


if __name__ == "__main__":
    unittest.main()
