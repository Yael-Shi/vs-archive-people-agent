"""Tests for the experimental Gemini mention pilot. No live provider calls."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vs_archive_people.artifacts.io import ArtifactError
from vs_archive_people.cli import main
from vs_archive_people.mention_pilot.align import align_proposals
from vs_archive_people.mention_pilot.consolidate import consolidate
from vs_archive_people.mention_pilot.errors import (
    PilotConfigurationError,
    PilotProviderError,
    PilotSplitConflictError,
)
from vs_archive_people.mention_pilot import gemini as gemini_module
from vs_archive_people.mention_pilot.gemini import (
    GeminiMentionDetector,
    build_client,
    build_generate_config,
    require_gemini_api_key,
)
from vs_archive_people.mention_pilot.known_names import find_known_name_spans
from vs_archive_people.mention_pilot.run import (
    displayed_body_for_item,
    run_mention_pilot,
)
from vs_archive_people.mention_pilot.schema import (
    CANDIDATE_RESPONSE_SCHEMA,
    GeminiProposal,
    parse_candidate_payload,
)
from vs_archive_people.mention_pilot.segment import (
    MAX_SEGMENT_CODE_POINTS,
    segment_body,
)
from vs_archive_people.resolution.snapshot_index import build_index

FIXTURES = Path(__file__).parent / "fixtures"
IDENTITY = json.loads((FIXTURES / "identity_snapshot.json").read_text(encoding="utf-8"))
DOCUMENTS = json.loads(
    (FIXTURES / "document_text_snapshot.json").read_text(encoding="utf-8")
)


class ScriptedDetector:
    def __init__(self, proposals):
        self.proposals = proposals
        self.seen_segments = None

    def detect(self, segments):
        self.seen_segments = segments
        return SimpleNamespace(proposals=list(self.proposals), calls=[])


def cloned_identity():
    return json.loads(json.dumps(IDENTITY))


class SegmentTests(unittest.TestCase):
    def test_newlines_stay_outside_segments_and_text_is_exact(self):
        body = "אב\n\ncd\r\n"
        segments = segment_body(body)
        self.assertEqual(
            [segment.segment_id for segment in segments], ["S0001", "S0002"]
        )
        covered = set()
        for segment in segments:
            self.assertEqual(
                body[segment.start_offset : segment.end_offset], segment.text
            )
            covered.update(range(segment.start_offset, segment.end_offset))
        for index, character in enumerate(body):
            if character == "\n":
                self.assertNotIn(index, covered)
            else:
                self.assertIn(index, covered)
        self.assertEqual(segments[0].text, "אב")
        self.assertEqual(segments[1].text, "cd\r")

    def test_long_line_split_preserves_every_code_point(self):
        body = ("word " * 300).strip()
        self.assertGreater(len(body), MAX_SEGMENT_CODE_POINTS)
        segments = segment_body(body)
        self.assertGreater(len(segments), 1)
        self.assertTrue(
            all(len(segment.text) <= MAX_SEGMENT_CODE_POINTS for segment in segments)
        )
        self.assertEqual("".join(segment.text for segment in segments), body)
        self.assertEqual(segments[0].start_offset, 0)
        self.assertEqual(segments[-1].end_offset, len(body))

    def test_hard_split_keeps_unspaced_text(self):
        body = "א" * (MAX_SEGMENT_CODE_POINTS + 25)
        segments = segment_body(body)
        self.assertEqual("".join(segment.text for segment in segments), body)
        self.assertTrue(
            all(len(segment.text) <= MAX_SEGMENT_CODE_POINTS for segment in segments)
        )


class AlignmentTests(unittest.TestCase):
    def test_repeated_surface_without_occurrence_is_not_given_the_first_offset(self):
        body = "משה פגש את משה"
        segments = segment_body(body)
        aligned, unaligned, _suppressed = align_proposals(
            body,
            segments,
            [GeminiProposal("S0001", "משה", "PARTIAL_NAME", None)],
        )
        self.assertEqual(aligned, [])
        self.assertEqual(unaligned[0].reason, "AMBIGUOUS_REPEATED_SURFACE")
        self.assertFalse(hasattr(unaligned[0], "start_offset"))

    def test_occurrence_hint_selects_the_verified_copy(self):
        body = "משה פגש את משה"
        second = body.index("משה", body.index("משה") + len("משה"))
        aligned, unaligned, _suppressed = align_proposals(
            body,
            segment_body(body),
            [GeminiProposal("S0001", "משה", "PARTIAL_NAME", 2)],
        )
        self.assertEqual(unaligned, [])
        self.assertEqual(aligned[0].start_offset, second)
        self.assertEqual(body[aligned[0].start_offset : aligned[0].end_offset], "משה")
        self.assertEqual(aligned[0].exact_mention, "משה")

    def test_rtl_offsets_are_python_code_points(self):
        prefix = "e\u0301"
        name = "חיים סעדיה"
        body = prefix + name + "."
        aligned, unaligned, _suppressed = align_proposals(
            body,
            segment_body(body),
            [GeminiProposal("S0001", name, "FULL_NAME", None)],
        )
        self.assertEqual(unaligned, [])
        start = aligned[0].start_offset
        end = aligned[0].end_offset
        self.assertEqual(start, len(prefix))
        self.assertEqual(body[start:end], name)
        self.assertNotEqual(start, 1)

    def test_hallucinated_surface_is_unaligned(self):
        body = "Morzouk wrote."
        aligned, unaligned, _suppressed = align_proposals(
            body,
            segment_body(body),
            [GeminiProposal("S0001", "Marzouk", "FULL_NAME", None)],
        )
        self.assertEqual(aligned, [])
        self.assertEqual(unaligned[0].reason, "SURFACE_NOT_IN_SEGMENT")
        self.assertEqual(unaligned[0].surface, "Marzouk")


class KnownNameTests(unittest.TestCase):
    def test_canonical_alias_pending_and_exact_bytes(self):
        identity = cloned_identity()
        identity["models"]["Person"]["rows"][0]["honorific"] = "Doctor"
        identity["models"]["PersonAlias"]["rows"].append(
            {
                "id": 13,
                "person_id": 1,
                "name": "Secret Alias",
                "review_status": "PENDING",
            }
        )
        identity["models"]["PersonAlias"]["rows"].append(
            {
                "id": 14,
                "person_id": 5,
                "name": "Hidden Pending",
            }
        )
        body = (
            "e\u0301Doctor. Pending Person met Ada Lovelace and Augusta King. "
            "Secret Alias. Hidden Pending."
        )
        spans = find_known_name_spans(body, identity, segment_body(body))
        mentions = [span.exact_mention for span in spans]
        self.assertIn("Ada Lovelace", mentions)
        self.assertIn("Augusta King", mentions)
        self.assertNotIn("Pending Person", mentions)
        self.assertNotIn("Secret Alias", mentions)
        self.assertNotIn("Hidden Pending", mentions)
        self.assertNotIn("Doctor", mentions)
        ada = next(span for span in spans if span.exact_mention == "Ada Lovelace")
        self.assertEqual(body[ada.start_offset : ada.end_offset], "Ada Lovelace")
        self.assertEqual(ada.start_offset, body.index("Ada Lovelace"))
        self.assertGreater(ada.start_offset, len("e\u0301"))
        self.assertNotIn("person_id", ada.__dict__)

    def test_source_text_is_not_normalized_before_offsets(self):
        body = "Ada  Lovelace wrote.\nada lovelace wrote.\nAda\u00a0Lovelace wrote.\nAda Lovelace wrote."
        spans = find_known_name_spans(body, IDENTITY, segment_body(body))
        self.assertEqual([span.exact_mention for span in spans], ["Ada Lovelace"])
        span = spans[0]
        self.assertEqual(body[span.start_offset : span.end_offset], "Ada Lovelace")
        self.assertGreater(span.start_offset, body.index("Ada  Lovelace"))
        self.assertNotEqual(body[span.start_offset - 1], " ")

    def test_overlapping_known_spans_are_both_kept(self):
        body = "Dana Cohen arrived."
        spans = find_known_name_spans(body, IDENTITY, segment_body(body))
        self.assertEqual(
            [
                (
                    span.exact_mention,
                    span.start_offset,
                    span.end_offset,
                    span.match_quality,
                )
                for span in spans
            ],
            [
                ("Dana", 0, 4, "EXACT_BOUNDARY"),
                ("Dana Cohen", 0, 10, "EXACT_BOUNDARY"),
            ],
        )

    def test_dana_inside_a_larger_token_is_embedded(self):
        # "Adana" does not contain the exact string "Dana". "ADana" does.
        body = "ADana"
        spans = find_known_name_spans(body, IDENTITY, segment_body(body))
        dana = [span for span in spans if span.exact_mention == "Dana"]
        self.assertEqual(len(dana), 1)
        self.assertEqual(dana[0].match_quality, "EMBEDDED")
        self.assertEqual(body[dana[0].start_offset : dana[0].end_offset], "Dana")
        self.assertEqual(
            find_known_name_spans("Adana", IDENTITY, segment_body("Adana")),
            [],
        )

    def test_standalone_hebrew_name_is_boundary_quality(self):
        identity = cloned_identity()
        identity["models"]["Person"]["rows"].append(
            {"id": 6, "name": "משה", "honorific": "", "biography": ""}
        )
        body = "משה בא"
        spans = find_known_name_spans(body, identity, segment_body(body))
        self.assertEqual(spans[0].exact_mention, "משה")
        self.assertEqual(spans[0].match_quality, "EXACT_BOUNDARY")
        self.assertEqual(body[spans[0].start_offset : spans[0].end_offset], "משה")

    def test_punctuation_around_a_name_is_boundary_quality(self):
        body = "(Ada Lovelace)."
        spans = find_known_name_spans(body, IDENTITY, segment_body(body))
        ada = next(span for span in spans if span.exact_mention == "Ada Lovelace")
        self.assertEqual(ada.match_quality, "EXACT_BOUNDARY")
        self.assertEqual(body[ada.start_offset : ada.end_offset], "Ada Lovelace")

    def test_hebrew_proclitic_is_prefixed_and_other_letters_stay_embedded(self):
        identity = cloned_identity()
        identity["models"]["Person"]["rows"].append(
            {"id": 6, "name": "משה", "honorific": "", "biography": ""}
        )
        prefixed = find_known_name_spans("ומשה,", identity, segment_body("ומשה,"))
        self.assertEqual(prefixed[0].match_quality, "PREFIXED")
        self.assertEqual(prefixed[0].exact_mention, "משה")
        vocalized = find_known_name_spans("בְמשה", identity, segment_body("בְמשה"))
        self.assertEqual(vocalized[0].match_quality, "PREFIXED")
        embedded = find_known_name_spans("אמשה", identity, segment_body("אמשה"))
        self.assertEqual(embedded[0].match_quality, "EMBEDDED")
        self.assertEqual(
            find_known_name_spans("משה", identity, segment_body("משה"))[
                0
            ].match_quality,
            "EXACT_BOUNDARY",
        )


class PilotRunTests(unittest.TestCase):
    def test_same_span_is_both_and_overlap_is_retained(self):
        body = "Ada Lovelace wrote the note."
        detector = ScriptedDetector(
            [
                GeminiProposal("S0001", "Ada Lovelace", "FULL_NAME", None),
                GeminiProposal("S0001", "Ada", "PARTIAL_NAME", None),
                GeminiProposal("S0001", "Marzouk", "FULL_NAME", None),
            ]
        )
        result = run_mention_pilot(
            body_text=body,
            archive_item_id=10,
            evidence_source="ocr_document.displayed_transcription.text",
            text_roles=["ocr_document.displayed_transcription.text"],
            identity_snapshot=IDENTITY,
            detector=detector,
        )
        self.assertEqual(result["kind"], "GEMINI_MENTION_PILOT_NOT_ARTIFACT")
        self.assertNotIn("schema_version", result)
        by_mention = {row["exact_mention"]: row for row in result["candidates"]}
        self.assertEqual(by_mention["Ada Lovelace"]["comparison"], "BOTH")
        self.assertEqual(
            by_mention["Ada Lovelace"]["detectors"], ["GEMINI", "KNOWN_NAME"]
        )
        self.assertEqual(by_mention["Ada Lovelace"]["gemini_kind"], "FULL_NAME")
        self.assertEqual(
            by_mention["Ada Lovelace"]["known_name_match_quality"], "EXACT_BOUNDARY"
        )
        self.assertEqual(by_mention["Ada"]["comparison"], "GEMINI")
        self.assertIsNone(by_mention["Ada"]["known_name_match_quality"])
        self.assertNotIn("person_id", by_mention["Ada Lovelace"])
        for row in result["candidates"]:
            self.assertEqual(
                body[row["start_offset"] : row["end_offset"]], row["exact_mention"]
            )
        self.assertEqual(result["unaligned_gemini_candidates"][0]["surface"], "Marzouk")
        self.assertNotIn("start_offset", result["unaligned_gemini_candidates"][0])
        self.assertIsNotNone(detector.seen_segments)

    def test_multiple_bodies_require_an_evidence_source(self):
        with self.assertRaises(ArtifactError):
            displayed_body_for_item(DOCUMENTS, 10, None)


class ProviderSchemaTests(unittest.TestCase):
    def test_valid_payload_and_rejections(self):
        parsed = parse_candidate_payload(
            {
                "candidates": [
                    {
                        "segment_id": "S0001",
                        "surface": "משה",
                        "kind": "PARTIAL_NAME",
                        "occurrence": 1,
                    }
                ]
            }
        )
        self.assertEqual(parsed[0].surface, "משה")
        branches = CANDIDATE_RESPONSE_SCHEMA["properties"]["candidates"]["items"][
            "anyOf"
        ]
        self.assertEqual(
            [branch["required"] for branch in branches],
            [
                ["segment_id", "surface", "kind", "name_surface", "title_surface"],
                ["segment_id", "surface"],
            ],
        )
        for branch in branches:
            self.assertNotIn("person_id", branch["properties"])
            self.assertNotIn("start_offset", branch["properties"])
        for payload in (
            {"candidates": [{"segment_id": "S0001"}]},
            {
                "candidates": [
                    {"segment_id": "S0001", "surface": "משה", "kind": "PERSON"}
                ]
            },
            {"candidates": [{"segment_id": "S0001", "surface": "משה", "person_id": 1}]},
            {
                "candidates": [
                    {"segment_id": "S0001", "surface": "משה", "occurrence": 0}
                ]
            },
        ):
            with self.assertRaises(PilotProviderError):
                parse_candidate_payload(payload)

    def test_generate_config_uses_low_thinking_and_model_defaults(self):
        config = build_generate_config()
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIs(config.response_json_schema, CANDIDATE_RESPONSE_SCHEMA)
        self.assertEqual(
            str(config.thinking_config.thinking_level).rsplit(".", 1)[-1], "LOW"
        )
        self.assertIsNone(config.thinking_config.thinking_budget)
        self.assertIsNone(config.temperature)
        self.assertIsNone(config.top_p)
        self.assertIsNone(config.top_k)
        self.assertIsNone(config.max_output_tokens)
        self.assertIsNone(config.tools)
        self.assertIsNone(config.safety_settings)

    def test_missing_api_key_fails_before_a_client_is_built(self):
        env = {
            key: value for key, value in os.environ.items() if key != "GEMINI_API_KEY"
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(PilotConfigurationError) as raised:
                require_gemini_api_key()
            self.assertIn("GEMINI_API_KEY", str(raised.exception))
            with patch(
                "vs_archive_people.mention_pilot.gemini.build_client",
                side_effect=AssertionError("client must not be built"),
            ):
                with self.assertRaises(PilotConfigurationError):
                    GeminiMentionDetector()
                with self.assertRaises(PilotConfigurationError):
                    GeminiMentionDetector(api_key="  ")

    def test_fake_client_parses_structured_output_without_a_network_call(self):
        models = FakeModels(
            text=json.dumps(
                {
                    "candidates": [
                        {
                            "segment_id": "S0001",
                            "surface": "משה",
                            "kind": "PARTIAL_NAME",
                        }
                    ]
                }
            )
        )
        detector = GeminiMentionDetector(client=SimpleNamespace(models=models))
        segments = segment_body("משה בא")
        detection = detector.detect(segments)
        self.assertEqual(models.kwargs["model"], "gemini-3.8-flash")
        self.assertIn("משה בא", models.kwargs["contents"])
        self.assertIsNone(models.kwargs["config"].temperature)
        self.assertEqual(detection.proposals[0].surface, "משה")
        self.assertEqual(detection.calls[0]["response_id"], "resp-1")
        self.assertEqual(detection.calls[0]["usage"]["prompt_token_count"], 11)
        self.assertNotIn("api_key", detection.calls[0])

    def test_title_and_name_requires_both_surfaces_in_schema(self):
        valid = {
            "segment_id": "S0001",
            "surface": 'ד"ר משה מרזוק',
            "kind": "TITLE_AND_NAME",
            "name_surface": "משה מרזוק",
            "title_surface": 'ד"ר',
        }
        parsed = parse_candidate_payload({"candidates": [valid]})
        self.assertEqual(parsed[0].title_surface, 'ד"ר')
        self.assertEqual(parsed[0].name_surface, "משה מרזוק")
        rejected = [
            {key: value for key, value in valid.items() if key != "title_surface"},
            {**valid, "title_surface": None},
            {key: value for key, value in valid.items() if key != "name_surface"},
            {**valid, "name_surface": None},
            {**valid, "person_id": 1},
        ]
        for row in rejected:
            with self.subTest(row=row):
                with self.assertRaises(PilotProviderError):
                    parse_candidate_payload({"candidates": [row]})

    def test_non_title_branch_keeps_optional_name_and_non_person(self):
        parsed = parse_candidate_payload(
            {
                "candidates": [
                    {
                        "segment_id": "S0001",
                        "surface": "משה מרזוק",
                        "kind": "FULL_NAME",
                    },
                    {
                        "segment_id": "S0001",
                        "surface": "לאהרוני",
                        "kind": "PARTIAL_NAME",
                        "name_surface": "אהרוני",
                    },
                    {
                        "segment_id": "S0002",
                        "surface": "פארוק",
                        "kind": "FULL_NAME",
                        "referent": "NON_PERSON",
                    },
                ]
            }
        )
        self.assertIsNone(parsed[0].title_surface)
        self.assertEqual(parsed[1].name_surface, "אהרוני")
        self.assertEqual(parsed[2].referent, "NON_PERSON")
        sent = build_generate_config().response_json_schema
        branches = sent["properties"]["candidates"]["items"]["anyOf"]
        self.assertEqual(
            [branch["properties"]["kind"] for branch in branches],
            [
                {"enum": ["TITLE_AND_NAME"]},
                {
                    "anyOf": [
                        {
                            "enum": [
                                "FULL_NAME",
                                "PARTIAL_NAME",
                                "NICKNAME",
                                "UNCERTAIN_PERSON_NAME",
                            ]
                        },
                        {"type": "null"},
                    ]
                },
            ],
        )

    def test_truncated_provider_response_is_not_parsed_as_candidates(self):
        models = FakeModels(
            text=json.dumps(
                {"candidates": [{"segment_id": "S0001", "surface": "משה"}]}
            ),
            finish_reason="MAX_TOKENS",
        )
        detector = GeminiMentionDetector(client=SimpleNamespace(models=models))
        with self.assertRaises(PilotProviderError) as raised:
            detector.detect(segment_body("משה"))
        self.assertIn("MAX_TOKENS", str(raised.exception))
        self.assertNotIn("משה", str(raised.exception))

    def test_api_error_becomes_pilot_provider_without_prompt_or_key(self):
        from google.genai import errors

        secret_key = "super-secret-api-key"
        body = "UNIQUE_PILOT_BODY_משה"
        poisoned = errors.ClientError(
            400,
            {
                "error": {
                    "code": 400,
                    "status": "INVALID_ARGUMENT",
                    "message": f"rejected {secret_key} {body}",
                }
            },
        )
        detector = GeminiMentionDetector(
            client=SimpleNamespace(models=_RaisingModels(poisoned))
        )
        with self.assertRaises(PilotProviderError) as raised:
            detector.detect(segment_body(body))
        message = str(raised.exception)
        self.assertEqual(raised.exception.error_code, "PILOT_PROVIDER")
        self.assertIn("ClientError", message)
        self.assertIn("code=400", message)
        self.assertIn("status=INVALID_ARGUMENT", message)
        self.assertNotIn(secret_key, message)
        self.assertNotIn(body, message)
        self.assertIsNone(raised.exception.__cause__)

    def test_unsafe_status_and_transport_text_are_not_copied(self):
        import httpx
        from google.genai import errors

        secret_key = "super-secret-api-key"
        body = "UNIQUE_PILOT_BODY_משה"
        poisoned = errors.ServerError(
            503,
            {
                "error": {
                    "status": f"UNAVAILABLE {secret_key}",
                    "message": body,
                }
            },
        )
        detector = GeminiMentionDetector(
            client=SimpleNamespace(models=_RaisingModels(poisoned))
        )
        with self.assertRaises(PilotProviderError) as raised:
            detector.detect(segment_body("משה"))
        message = str(raised.exception)
        self.assertIn("ServerError", message)
        self.assertIn("code=503", message)
        self.assertNotIn("status=", message)
        self.assertNotIn(secret_key, message)
        self.assertNotIn(body, message)

        timeout = httpx.ReadTimeout(f"timed out {secret_key} {body}")
        detector = GeminiMentionDetector(
            client=SimpleNamespace(models=_RaisingModels(timeout))
        )
        with self.assertRaises(PilotProviderError) as raised:
            detector.detect(segment_body(body))
        message = str(raised.exception)
        self.assertEqual(message, "provider transport failed: ReadTimeout")
        self.assertNotIn(secret_key, message)
        self.assertNotIn(body, message)

    def test_client_asks_the_sdk_for_three_attempts(self):
        client = build_client("test-key")
        options = client._api_client._http_options
        retry = options.retry_options
        self.assertEqual(options.timeout, 120_000)
        self.assertEqual(retry.attempts, 3)
        self.assertIsNone(retry.initial_delay)
        self.assertIsNone(retry.max_delay)
        self.assertIsNone(retry.exp_base)
        self.assertIsNone(retry.jitter)
        self.assertIsNone(retry.http_status_codes)

    def test_detector_calls_generate_content_once_per_batch(self):
        models = FakeModels(text=json.dumps({"candidates": []}))
        detector = GeminiMentionDetector(client=SimpleNamespace(models=models))
        with patch(
            "vs_archive_people.mention_pilot.gemini.MAX_BATCH_CODE_POINTS",
            4,
        ):
            detector.detect(segment_body("one\ntwo"))
        self.assertEqual(models.calls, 2)
        source = Path(gemini_module.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("client.models.generate_content("), 1)
        self.assertNotIn("time.sleep", source)

    def test_provider_failure_is_not_retried_in_the_detector(self):
        from google.genai import errors

        models = _RaisingModels(
            errors.ClientError(
                429,
                {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
            )
        )
        detector = GeminiMentionDetector(client=SimpleNamespace(models=models))
        with self.assertRaises(PilotProviderError):
            detector.detect(segment_body("one\ntwo"))
        self.assertEqual(models.calls, 1)

    def test_programmer_errors_are_not_translated(self):
        detector = GeminiMentionDetector(
            client=SimpleNamespace(models=_RaisingModels(RuntimeError("bug")))
        )
        with self.assertRaises(RuntimeError):
            detector.detect(segment_body("משה"))


class _RaisingModels:
    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        raise self.exc


class FakeModels:
    def __init__(self, text, finish_reason="STOP"):
        self.text = text
        self.finish_reason = finish_reason
        self.kwargs = None
        self.calls = 0

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        self.kwargs = {"model": model, "contents": contents, "config": config}
        return SimpleNamespace(
            text=self.text,
            candidates=[SimpleNamespace(finish_reason=self.finish_reason)],
            prompt_feedback=None,
            response_id="resp-1",
            model_version="gemini-3.8-flash",
            usage_metadata=SimpleNamespace(
                prompt_token_count=11,
                candidates_token_count=3,
                thoughts_token_count=2,
                total_token_count=16,
            ),
        )


class PilotCliTests(unittest.TestCase):
    def test_missing_api_key_blocks_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents = root / "documents.json"
            identity = root / "identity.json"
            output = root / "pilot.json"
            documents.write_text(json.dumps(DOCUMENTS), encoding="utf-8")
            identity.write_text(json.dumps(IDENTITY), encoding="utf-8")
            env = {
                key: value
                for key, value in os.environ.items()
                if key != "GEMINI_API_KEY"
            }
            stderr = StringIO()
            with patch.dict(os.environ, env, clear=True):
                with redirect_stderr(stderr):
                    code = main(
                        [
                            "pilot-find-mentions",
                            "--documents",
                            str(documents),
                            "--archive-item-id",
                            "10",
                            "--evidence-source",
                            "ocr_document.displayed_transcription.text",
                            "--identity",
                            str(identity),
                            "--output",
                            str(output),
                        ]
                    )
            self.assertEqual(code, 2)
            payload = json.loads(stderr.getvalue())
            self.assertEqual(payload["status"], "BLOCKED")
            self.assertEqual(payload["error_code"], "PILOT_CONFIGURATION")
            self.assertFalse(output.exists())

    def test_scripted_detector_writes_experimental_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents = root / "documents.json"
            identity = root / "identity.json"
            output = root / "pilot.json"
            documents.write_text(json.dumps(DOCUMENTS), encoding="utf-8")
            identity.write_text(json.dumps(IDENTITY), encoding="utf-8")
            scripted = ScriptedDetector(
                [GeminiProposal("S0001", "Ada Lovelace", "FULL_NAME", None)]
            )
            with patch(
                "vs_archive_people.cli.GeminiMentionDetector",
                return_value=scripted,
            ):
                code = main(
                    [
                        "pilot-find-mentions",
                        "--documents",
                        str(documents),
                        "--archive-item-id",
                        "10",
                        "--evidence-source",
                        "ocr_document.displayed_transcription.text",
                        "--identity",
                        str(identity),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            published = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(published["kind"], "GEMINI_MENTION_PILOT_NOT_ARTIFACT")
            self.assertNotIn("schema_version", published)
            candidate = published["candidates"][0]
            body = "Ada Lovelace wrote the note."
            self.assertEqual(
                body[candidate["start_offset"] : candidate["end_offset"]],
                candidate["exact_mention"],
            )
            self.assertEqual(candidate["comparison"], "BOTH")


class PilotRefinementTests(unittest.TestCase):
    def test_title_only_role_is_not_a_person_candidate(self):
        body = 'הנשיא נאם. ד"ר משה מרזוק ישב.'
        aligned, unaligned, _suppressed = align_proposals(
            body,
            segment_body(body),
            [
                GeminiProposal("S0001", "הנשיא", None, None, name_surface="הנשיא"),
                GeminiProposal("S0001", "הנשיא", None, None, name_surface=""),
            ],
        )
        self.assertEqual(aligned, [])
        self.assertEqual(
            [row.reason for row in unaligned],
            ["TITLE_OR_ROLE_WITHOUT_NAME", "TITLE_OR_ROLE_WITHOUT_NAME"],
        )

    def test_title_and_name_is_one_mention_with_exact_slices(self):
        cases = [
            ('ד"ר משה מרזוק', 'ד"ר', "משה מרזוק"),
            ("השגריר האמריקאי ג'פרסון קאפרי", "השגריר האמריקאי", "ג'פרסון קאפרי"),
            ("הלורד מויו", "הלורד", "מויו"),
        ]
        for full, title, name in cases:
            with self.subTest(full=full):
                result = run_mention_pilot(
                    body_text=full,
                    archive_item_id=1,
                    evidence_source="body",
                    text_roles=["body"],
                    identity_snapshot=IDENTITY,
                    detector=ScriptedDetector(
                        [
                            GeminiProposal(
                                "S0001",
                                full,
                                "TITLE_AND_NAME",
                                None,
                                name_surface=name,
                                title_surface=title,
                            )
                        ]
                    ),
                )
                self.assertEqual(len(result["candidates"]), 1)
                row = result["candidates"][0]
                self.assertEqual(row["exact_mention"], full)
                self.assertEqual(row["title_surface"], title)
                self.assertEqual(row["name_surface"], name)
                self.assertEqual(full[row["start_offset"] : row["end_offset"]], full)
                self.assertEqual(
                    full[row["title_start_offset"] : row["title_end_offset"]], title
                )
                self.assertEqual(
                    full[row["name_start_offset"] : row["name_end_offset"]], name
                )
                self.assertNotIn("person_id", row)
                self.assertEqual(result["unaligned_gemini_candidates"], [])

    def test_street_occurrence_is_rejected_without_banning_the_name(self):
        body = (
            "רחוב פארוק נפגש עם פארוק. "
            "רחוב פואד הראשון. פואד הראשון פתח. "
            "רחוב סולימאן פאשה. סולימאן פאשה הגיע."
        )
        identity = cloned_identity()
        identity["models"]["Person"]["rows"].append(
            {"id": 6, "name": "פארוק", "honorific": "", "biography": ""}
        )
        proposals = []
        for surface, occurrence in (
            ("פארוק", 1),
            ("פארוק", 2),
            ("פואד הראשון", 1),
            ("פואד הראשון", 2),
            ("סולימאן פאשה", 1),
            ("סולימאן פאשה", 2),
        ):
            proposals.append(GeminiProposal("S0001", surface, "FULL_NAME", occurrence))
        result = run_mention_pilot(
            body_text=body,
            archive_item_id=1,
            evidence_source="body",
            text_roles=["body"],
            identity_snapshot=identity,
            detector=ScriptedDetector(proposals),
        )
        kept = [
            (row["exact_mention"], row["start_offset"]) for row in result["candidates"]
        ]
        self.assertEqual(
            kept,
            [
                ("פארוק", body.index("עם פארוק") + len("עם ")),
                ("פואד הראשון", body.index("פתח") - len("פואד הראשון ")),
                ("סולימאן פאשה", body.index("הגיע") - len("סולימאן פאשה ")),
            ],
        )
        for row in result["candidates"]:
            self.assertEqual(
                body[row["start_offset"] : row["end_offset"]], row["exact_mention"]
            )
            self.assertFalse(body[: row["start_offset"]].endswith("רחוב "))
        reasons = [row["reason"] for row in result["unaligned_gemini_candidates"]]
        self.assertEqual(reasons, ["PLACE_OR_INSTITUTION_CONTEXT"] * 3)

    def test_non_person_referent_suppresses_one_occurrence_only(self):
        body = "אוניברסיטת פארוק. פארוק נאם."
        identity = cloned_identity()
        identity["models"]["Person"]["rows"].append(
            {"id": 6, "name": "פארוק", "honorific": "", "biography": ""}
        )
        first = body.index("פארוק")
        second = body.index("פארוק", first + len("פארוק"))
        result = run_mention_pilot(
            body_text=body,
            archive_item_id=1,
            evidence_source="body",
            text_roles=["body"],
            identity_snapshot=identity,
            detector=ScriptedDetector(
                [
                    GeminiProposal(
                        "S0001",
                        "פארוק",
                        "FULL_NAME",
                        1,
                        referent="NON_PERSON",
                    ),
                    GeminiProposal("S0001", "פארוק", "FULL_NAME", 2),
                ]
            ),
        )
        self.assertEqual(
            [row["start_offset"] for row in result["candidates"]], [second]
        )
        self.assertNotIn(first, [row["start_offset"] for row in result["candidates"]])
        self.assertEqual(
            result["unaligned_gemini_candidates"][0]["reason"],
            "PLACE_OR_INSTITUTION_CONTEXT",
        )

    def test_attached_lamed_prefix_uses_exact_core_name_offsets(self):
        cases = [
            ("לאהרוני", "אהרוני"),
            ("לויקטור לוי", "ויקטור לוי"),
            ("לאיסר כראל", "איסר כראל"),
        ]
        for full, name in cases:
            with self.subTest(full=full):
                body = f"כתב {full} אתמול"
                aligned, unaligned, _suppressed = align_proposals(
                    body,
                    segment_body(body),
                    [
                        GeminiProposal(
                            "S0001",
                            full,
                            "FULL_NAME",
                            None,
                            name_surface=name,
                        )
                    ],
                )
                self.assertEqual(unaligned, [])
                mention = aligned[0]
                self.assertEqual(mention.exact_mention, full)
                self.assertEqual(mention.name_surface, name)
                self.assertEqual(
                    body[mention.name_start_offset : mention.name_end_offset], name
                )
                self.assertEqual(body[mention.start_offset : mention.end_offset], full)
                self.assertEqual(mention.name_start_offset, body.index(name))
                self.assertNotEqual(mention.name_surface, full)

    def test_prefix_is_not_stripped_blindly_or_rewritten(self):
        prefixed = "לאהרוני"
        kept_whole, unaligned, _suppressed = align_proposals(
            prefixed,
            segment_body(prefixed),
            [GeminiProposal("S0001", prefixed, "FULL_NAME", None)],
        )
        self.assertEqual(unaligned, [])
        self.assertEqual(kept_whole[0].name_surface, prefixed)
        self.assertEqual(kept_whole[0].name_start_offset, 0)

        body = "שלום אמר Morzouk והלורד."
        kept, rejected, _suppressed = align_proposals(
            body,
            segment_body(body),
            [
                GeminiProposal("S0001", "שלום", "FULL_NAME", None, name_surface="לום"),
                GeminiProposal(
                    "S0001", "הלורד", "FULL_NAME", None, name_surface="לורד"
                ),
                GeminiProposal(
                    "S0001", "Morzouk", "FULL_NAME", None, name_surface="Marzouk"
                ),
                GeminiProposal("S0001", "לאהרוני", "FULL_NAME", None),
            ],
        )
        self.assertEqual(rejected[0].reason, "SURFACE_NOT_IN_SEGMENT")
        self.assertEqual(rejected[0].surface, "לאהרוני")
        by_surface = {row.exact_mention: row for row in kept}
        self.assertEqual(set(by_surface), {"שלום", "הלורד", "Morzouk"})
        for row in kept:
            self.assertEqual(row.name_surface, row.exact_mention)
            self.assertEqual(
                body[row.name_start_offset : row.name_end_offset], row.exact_mention
            )
            self.assertNotIn(row.name_surface, {"לום", "לורד", "Marzouk"})

    def test_cross_line_title_and_name_keep_the_original_break(self):
        body = "האלוף\r\nבנימין גיבלי"
        segments = segment_body(body)
        self.assertEqual(segments[0].text, "האלוף\r")
        self.assertEqual(segments[0].break_after, "\n")
        self.assertEqual(segments[1].text, "בנימין גיבלי")
        full = "האלוף\r\nבנימין גיבלי"
        aligned, unaligned, _suppressed = align_proposals(
            body,
            segments,
            [
                GeminiProposal(
                    "S0001",
                    full,
                    "TITLE_AND_NAME",
                    None,
                    name_surface="בנימין גיבלי",
                    title_surface="האלוף",
                    end_segment_id="S0002",
                )
            ],
        )
        self.assertEqual(unaligned, [])
        mention = aligned[0]
        self.assertEqual(mention.exact_mention, full)
        self.assertIn("\r\n", body[mention.start_offset : mention.end_offset])
        self.assertEqual(
            body[mention.title_start_offset : mention.title_end_offset], "האלוף"
        )
        self.assertEqual(
            body[mention.name_start_offset : mention.name_end_offset], "בנימין גיבלי"
        )
        self.assertEqual(mention.end_segment_id, "S0002")

        spaced, missed, _suppressed = align_proposals(
            body,
            segments,
            [
                GeminiProposal(
                    "S0001",
                    "האלוף בנימין גיבלי",
                    "TITLE_AND_NAME",
                    None,
                    end_segment_id="S0002",
                ),
                GeminiProposal(
                    "S0001",
                    "האלוף\nבנימין גיבלי",
                    "TITLE_AND_NAME",
                    None,
                    end_segment_id="S0002",
                ),
            ],
        )
        self.assertEqual(spaced, [])
        self.assertEqual(
            [row.reason for row in missed],
            ["SURFACE_NOT_IN_SEGMENT", "SURFACE_NOT_IN_SEGMENT"],
        )

    def test_cross_line_name_is_aligned_without_removing_the_break(self):
        body = "שמואל\r\nעזר"
        full = body
        aligned, unaligned, _suppressed = align_proposals(
            body,
            segment_body(body),
            [
                GeminiProposal(
                    "S0001",
                    full,
                    "FULL_NAME",
                    None,
                    end_segment_id="S0002",
                )
            ],
        )
        self.assertEqual(unaligned, [])
        mention = aligned[0]
        self.assertEqual(body[mention.start_offset : mention.end_offset], full)
        self.assertEqual(mention.name_surface, full)
        self.assertIn("\n", mention.exact_mention)
        self.assertNotEqual(mention.exact_mention, "שמואל עזר")
        bare, missed, _suppressed = align_proposals(
            body,
            segment_body(body),
            [GeminiProposal("S0001", full, "FULL_NAME", None)],
        )
        self.assertEqual(bare, [])
        self.assertEqual(missed[0].reason, "SURFACE_NOT_IN_SEGMENT")

    def test_repeated_surface_still_requires_an_occurrence(self):
        body = "משה פגש את משה"
        aligned, unaligned, _suppressed = align_proposals(
            body,
            segment_body(body),
            [
                GeminiProposal(
                    "S0001",
                    "משה",
                    "PARTIAL_NAME",
                    None,
                    name_surface="משה",
                )
            ],
        )
        self.assertEqual(aligned, [])
        self.assertEqual(unaligned[0].reason, "AMBIGUOUS_REPEATED_SURFACE")

    def test_instruction_requires_local_context_and_exact_copying(self):
        instruction = gemini_module.SYSTEM_INSTRUCTION
        self.assertIn("רחוב פארוק", instruction)
        self.assertIn("הנשיא", instruction)
        self.assertIn("לאהרוני", instruction)
        self.assertIn("break_after", instruction)
        self.assertIn("Morzouk", instruction)
        self.assertIn("NON_PERSON", instruction)
        self.assertIn("omit referent or set PERSON", instruction)
        self.assertNotIn("Omit referent, or set PERSON", instruction)
        self.assertNotIn("person_id", instruction)

    def test_titled_occurrence_absorbs_the_exact_core_name(self):
        full = 'ד"ר משה מרזוק'
        name = "משה מרזוק"
        body = f"{full} בא. {name} הלך. משה נשאר."
        identity = cloned_identity()
        identity["models"]["Person"]["rows"].append(
            {"id": 6, "name": name, "honorific": "", "biography": ""}
        )
        identity["models"]["Person"]["rows"].append(
            {"id": 7, "name": "משה", "honorific": "", "biography": ""}
        )
        titled = GeminiProposal(
            "S0001",
            full,
            "TITLE_AND_NAME",
            None,
            name_surface=name,
            title_surface='ד"ר',
        )
        core = GeminiProposal("S0001", name, "PARTIAL_NAME", 1)
        result = run_mention_pilot(
            body_text=body,
            archive_item_id=1,
            evidence_source="body",
            text_roles=["body"],
            identity_snapshot=identity,
            detector=ScriptedDetector([titled, core]),
        )
        titled_rows = [
            row for row in result["candidates"] if row["exact_mention"] == full
        ]
        self.assertEqual(len(titled_rows), 1)
        row = titled_rows[0]
        self.assertEqual(body[row["start_offset"] : row["end_offset"]], full)
        self.assertEqual(row["name_surface"], name)
        self.assertEqual(body[row["name_start_offset"] : row["name_end_offset"]], name)
        self.assertEqual(row["title_surface"], 'ד"ר')
        self.assertEqual(
            body[row["title_start_offset"] : row["title_end_offset"]], 'ד"ר'
        )
        self.assertEqual(row["detectors"], ["GEMINI", "KNOWN_NAME"])
        self.assertEqual(row["comparison"], "BOTH")
        self.assertEqual(row["gemini_kind"], "TITLE_AND_NAME")
        self.assertEqual(row["known_name_match_quality"], "EXACT_BOUNDARY")
        core_offsets = {
            (item["start_offset"], item["end_offset"])
            for item in result["candidates"]
            if item["exact_mention"] == name
        }
        self.assertNotIn(
            (row["name_start_offset"], row["name_end_offset"]), core_offsets
        )
        later = body.index(f"{name} הלך")
        self.assertIn(
            later,
            [
                item["start_offset"]
                for item in result["candidates"]
                if item["exact_mention"] == name
            ],
        )
        partial = next(
            item for item in result["candidates"] if item["exact_mention"] == "משה"
        )
        self.assertNotEqual(
            (partial["start_offset"], partial["end_offset"]),
            (row["name_start_offset"], row["name_end_offset"]),
        )

    def test_person_and_null_referents_stay_published(self):
        body = "פארוק נאם. פארוק ישב."
        identity = cloned_identity()
        identity["models"]["Person"]["rows"].append(
            {"id": 6, "name": "פארוק", "honorific": "", "biography": ""}
        )
        first = body.index("פארוק")
        second = body.index("פארוק", first + len("פארוק"))
        result = run_mention_pilot(
            body_text=body,
            archive_item_id=1,
            evidence_source="body",
            text_roles=["body"],
            identity_snapshot=identity,
            detector=ScriptedDetector(
                [
                    GeminiProposal("S0001", "פארוק", "FULL_NAME", 1, referent="PERSON"),
                    GeminiProposal("S0001", "פארוק", "FULL_NAME", 2),
                ]
            ),
        )
        self.assertEqual(
            [row["start_offset"] for row in result["candidates"]],
            [first, second],
        )
        self.assertEqual(result["unaligned_gemini_candidates"], [])

    def test_non_person_span_suppresses_a_contained_known_name_only(self):
        body = "אוניברסיטת פארוק נפתחה. פארוק נאם."
        identity = cloned_identity()
        identity["models"]["Person"]["rows"].append(
            {"id": 6, "name": "פארוק", "honorific": "", "biography": ""}
        )
        phrase = "אוניברסיטת פארוק"
        contained = body.index("פארוק")
        person = body.index("פארוק נאם")
        result = run_mention_pilot(
            body_text=body,
            archive_item_id=1,
            evidence_source="body",
            text_roles=["body"],
            identity_snapshot=identity,
            detector=ScriptedDetector(
                [
                    GeminiProposal(
                        "S0001",
                        phrase,
                        None,
                        None,
                        referent="NON_PERSON",
                    ),
                    GeminiProposal("S0001", "פארוק", "FULL_NAME", 2, referent="PERSON"),
                ]
            ),
        )
        published = [row["exact_mention"] for row in result["candidates"]]
        self.assertNotIn(phrase, published)
        self.assertEqual(
            [row["start_offset"] for row in result["candidates"]], [person]
        )
        self.assertNotIn(
            contained, [row["start_offset"] for row in result["candidates"]]
        )
        self.assertEqual(result["unaligned_gemini_candidates"][0]["surface"], phrase)
        self.assertEqual(
            result["unaligned_gemini_candidates"][0]["reason"],
            "PLACE_OR_INSTITUTION_CONTEXT",
        )


class TitleFallbackTests(unittest.TestCase):
    def test_supplied_title_split_is_kept(self):
        full = 'ד"ר משה מרזוק'
        aligned, unaligned, _suppressed = align_proposals(
            full,
            segment_body(full),
            [
                GeminiProposal(
                    "S0001",
                    full,
                    "TITLE_AND_NAME",
                    None,
                    name_surface="משה מרזוק",
                    title_surface='ד"ר',
                )
            ],
        )
        self.assertEqual(unaligned, [])
        self.assertEqual(aligned[0].title_surface, 'ד"ר')
        self.assertEqual(aligned[0].name_surface, "משה מרזוק")

    def test_free_text_titles_keep_the_supplied_exact_split(self):
        cases = [
            ('ד"ר משה מרזוק', 'ד"ר', "משה מרזוק", None),
            (
                "השגריר האמריקאי ג'פרסון קאפרי",
                "השגריר האמריקאי",
                "ג'פרסון קאפרי",
                None,
            ),
            ("הלורד מויו", "הלורד", "מויו", None),
        ]
        for full, title, name, end_segment in cases:
            with self.subTest(full=full):
                aligned, unaligned, _suppressed = align_proposals(
                    full,
                    segment_body(full),
                    [
                        GeminiProposal(
                            "S0001",
                            full,
                            "TITLE_AND_NAME",
                            None,
                            name_surface=name,
                            title_surface=title,
                            end_segment_id=end_segment,
                        )
                    ],
                )
                self.assertEqual(unaligned, [])
                mention = aligned[0]
                self.assertEqual(mention.exact_mention, full)
                self.assertEqual(mention.title_surface, title)
                self.assertEqual(mention.name_surface, name)
                self.assertEqual(
                    full[mention.title_start_offset : mention.title_end_offset],
                    title,
                )
                self.assertEqual(
                    full[mention.name_start_offset : mention.name_end_offset],
                    name,
                )

    def test_cross_line_attached_title_keeps_exact_offsets(self):
        body = "והאלוף\nבנימין גיבלי"
        segments = segment_body(body)
        self.assertEqual(segments[0].break_after, "\n")
        aligned, unaligned, _suppressed = align_proposals(
            body,
            segments,
            [
                GeminiProposal(
                    "S0001",
                    body,
                    "TITLE_AND_NAME",
                    None,
                    name_surface="בנימין גיבלי",
                    title_surface="והאלוף",
                    end_segment_id="S0002",
                )
            ],
        )
        self.assertEqual(unaligned, [])
        mention = aligned[0]
        self.assertEqual(body[mention.start_offset : mention.end_offset], body)
        self.assertIn("\n", mention.exact_mention)
        self.assertEqual(mention.title_surface, "והאלוף")
        self.assertEqual(
            body[mention.title_start_offset : mention.title_end_offset], "והאלוף"
        )
        self.assertEqual(mention.name_surface, "בנימין גיבלי")
        self.assertEqual(
            body[mention.name_start_offset : mention.name_end_offset], "בנימין גיבלי"
        )
        self.assertLess(mention.title_end_offset, mention.name_start_offset)

    def test_incomplete_or_inexact_title_split_is_unresolved(self):
        full = 'ד"ר משה מרזוק'
        proposals = [
            GeminiProposal("S0001", full, "TITLE_AND_NAME", None),
            GeminiProposal(
                "S0001",
                full,
                "TITLE_AND_NAME",
                None,
                name_surface="משה מרזוק",
            ),
            GeminiProposal(
                "S0001",
                full,
                "TITLE_AND_NAME",
                None,
                title_surface='ד"ר',
            ),
            GeminiProposal(
                "S0001",
                full,
                "TITLE_AND_NAME",
                None,
                name_surface="מרזוק",
                title_surface="דוקטור",
            ),
        ]
        aligned, unaligned, _suppressed = align_proposals(
            full, segment_body(full), proposals
        )
        self.assertEqual(aligned, [])
        self.assertEqual(
            [row.reason for row in unaligned],
            ["TITLE_SPLIT_UNRESOLVED"] * len(proposals),
        )
        self.assertTrue(all(row.surface == full for row in unaligned))

    def test_unresolved_title_is_not_published_as_the_full_name(self):
        full = "המושל משה דיין"
        result = run_mention_pilot(
            body_text=full,
            archive_item_id=1,
            evidence_source="body",
            text_roles=["body"],
            identity_snapshot=IDENTITY,
            detector=ScriptedDetector(
                [GeminiProposal("S0001", full, "TITLE_AND_NAME", None)]
            ),
        )
        self.assertEqual(result["candidates"], [])
        rejected = result["unaligned_gemini_candidates"][0]
        self.assertEqual(rejected["reason"], "TITLE_SPLIT_UNRESOLVED")
        self.assertEqual(rejected["surface"], full)
        self.assertEqual(rejected["gemini_kind"], "TITLE_AND_NAME")

    def test_full_name_kind_is_not_rewritten_as_a_title(self):
        full = 'ד"ר משה מרזוק'
        aligned, unaligned, _suppressed = align_proposals(
            full,
            segment_body(full),
            [GeminiProposal("S0001", full, "FULL_NAME", None)],
        )
        self.assertEqual(unaligned, [])
        self.assertEqual(aligned[0].exact_mention, full)
        self.assertEqual(aligned[0].name_surface, full)
        self.assertIsNone(aligned[0].title_surface)

    def test_punctuation_between_exact_title_and_name_stays_uncovered(self):
        cases = [
            ('ראש אמ"ן, בנימין גיבלי', 'ראש אמ"ן', "בנימין גיבלי", None),
            ('הרמטכ"ל, משה דיין', 'הרמטכ"ל', "משה דיין", None),
            (
                "שר הבטחון דאז,\r\nפנחס לבון",
                "שר הבטחון דאז",
                "פנחס לבון",
                "S0002",
            ),
            (
                "ראש הממשלה\r\nדאז, משה שרת",
                "ראש הממשלה\r\nדאז",
                "משה שרת",
                "S0002",
            ),
        ]
        for full, title, name, end_segment in cases:
            with self.subTest(full=full):
                aligned, unaligned, _suppressed = align_proposals(
                    full,
                    segment_body(full),
                    [
                        GeminiProposal(
                            "S0001",
                            full,
                            "TITLE_AND_NAME",
                            None,
                            name_surface=name,
                            title_surface=title,
                            end_segment_id=end_segment,
                        )
                    ],
                )
                self.assertEqual(unaligned, [])
                mention = aligned[0]
                self.assertEqual(mention.exact_mention, full)
                self.assertEqual(mention.title_surface, title)
                self.assertEqual(mention.name_surface, name)
                self.assertEqual(
                    full[mention.title_start_offset : mention.title_end_offset],
                    title,
                )
                self.assertEqual(
                    full[mention.name_start_offset : mention.name_end_offset],
                    name,
                )
                gap = full[mention.title_end_offset : mention.name_start_offset]
                self.assertIn(",", gap)
                self.assertNotIn(",", mention.title_surface)
                self.assertNotIn(",", mention.name_surface)

    def test_title_gap_rejects_letters_overlap_and_inexact_slices(self):
        full = "ראש הממשלה\r\nדאז, משה שרת"
        proposals = [
            GeminiProposal(
                "S0001",
                full,
                "TITLE_AND_NAME",
                None,
                name_surface="משה שרת",
                title_surface="ראש הממשלה",
                end_segment_id="S0002",
            ),
            GeminiProposal(
                "S0001",
                'הרמטכ"ל, משה דיין',
                "TITLE_AND_NAME",
                None,
                name_surface="משה דיין",
                title_surface="משה",
            ),
            GeminiProposal(
                "S0001",
                'הרמטכ"ל, משה דיין',
                "TITLE_AND_NAME",
                None,
                name_surface="משה דיין",
                title_surface="הרמטכל",
            ),
            GeminiProposal(
                "S0001",
                'הרמטכ"ל, משה דיין',
                "TITLE_AND_NAME",
                None,
                name_surface="משה דיין",
            ),
            GeminiProposal(
                "S0001",
                'הרמטכ"ל, משה דיין',
                "TITLE_AND_NAME",
                None,
                title_surface='הרמטכ"ל',
            ),
        ]
        for proposal in proposals:
            with self.subTest(surface=proposal.surface, title=proposal.title_surface):
                aligned, unaligned, _suppressed = align_proposals(
                    proposal.surface,
                    segment_body(proposal.surface),
                    [proposal],
                )
                self.assertEqual(aligned, [])
                self.assertEqual(unaligned[0].reason, "TITLE_SPLIT_UNRESOLVED")
                self.assertEqual(unaligned[0].surface, proposal.surface)


class SplitConflictTests(unittest.TestCase):
    def test_identical_title_splits_consolidate_to_one_candidate(self):
        full = 'ד"ר משה מרזוק'
        proposal = GeminiProposal(
            "S0001",
            full,
            "TITLE_AND_NAME",
            None,
            name_surface="משה מרזוק",
            title_surface='ד"ר',
        )
        aligned, unaligned, _suppressed = align_proposals(
            full, segment_body(full), [proposal, proposal]
        )
        self.assertEqual(unaligned, [])
        rows = consolidate(full, aligned, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exact_mention"], full)
        self.assertEqual(rows[0]["title_surface"], 'ד"ר')
        self.assertEqual(rows[0]["name_surface"], "משה מרזוק")
        self.assertNotEqual(rows[0]["name_surface"], rows[0]["exact_mention"])

    def test_conflicting_exact_splits_fail_closed(self):
        full = 'ד"ר משה מרזוק'
        proposals = [
            GeminiProposal(
                "S0001",
                full,
                "TITLE_AND_NAME",
                None,
                name_surface="משה מרזוק",
                title_surface='ד"ר',
            ),
            GeminiProposal(
                "S0001",
                full,
                "TITLE_AND_NAME",
                None,
                name_surface="מרזוק",
                title_surface='ד"ר משה',
            ),
        ]
        aligned, unaligned, _suppressed = align_proposals(
            full, segment_body(full), proposals
        )
        self.assertEqual(unaligned, [])
        self.assertEqual(len(aligned), 2)
        with self.assertRaises(PilotSplitConflictError) as raised:
            consolidate(full, aligned, [])
        self.assertEqual(raised.exception.error_code, "PILOT_SPLIT_CONFLICT")
        with self.assertRaises(PilotSplitConflictError):
            run_mention_pilot(
                body_text=full,
                archive_item_id=1,
                evidence_source="body",
                text_roles=["body"],
                identity_snapshot=IDENTITY,
                detector=ScriptedDetector(proposals),
            )


class ExactNameIndexTests(unittest.TestCase):
    def test_pending_names_are_absent_from_exact_name_strings(self):
        names = build_index(IDENTITY).exact_names
        self.assertIn("Ada Lovelace", names)
        self.assertIn("Augusta King", names)
        self.assertIn("Dana", names)
        self.assertNotIn("Pending Person", names)
        self.assertEqual(names.count("Dana"), 1)


if __name__ == "__main__":
    unittest.main()
