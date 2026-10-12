"""Gemini 3.8 Flash detector for the mention pilot.

The adapter proposes surfaces. It does not assign identity, and it does not
read or log ``GEMINI_API_KEY``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from vs_archive_people.mention_pilot.errors import (
    PilotConfigurationError,
    PilotProviderError,
)
from vs_archive_people.mention_pilot.schema import (
    CANDIDATE_RESPONSE_SCHEMA,
    GeminiProposal,
    parse_candidate_payload,
)
from vs_archive_people.mention_pilot.segment import Segment

MODEL_NAME = "gemini-3.8-flash"
REQUEST_TIMEOUT_MS = 120_000
MAX_BATCH_CODE_POINTS = 20_000
ACCEPTED_FINISH_REASONS = frozenset({"STOP", "FINISH_REASON_UNSPECIFIED"})
_SAFE_STATUS = re.compile(r"[A-Za-z0-9_]{1,64}")

SYSTEM_INSTRUCTION = """You identify text spans that look like the name of a real human being.

You do not identify which real-world person a span belongs to. You do not assign a person id, choose a canonical identity, create an alias, suggest a merge, or approve a person.

Return only structured candidates for the supplied segments. Copy surfaces exactly. Keep OCR errors, historical spelling, transliteration, and mixed Hebrew, Arabic, French, and English scripts. Do not correct spelling. If the segment says Morzouk, the surface is Morzouk and not Marzouk. Do not translate. Do not normalize. Do not return source offsets.

Prefer recall. Include a full name, a partial name, a title plus a name, a nickname, or another person-reference surface of a real human, including when the identity is uncertain.

Use the local sentence, not the letters of the string alone. The same string can be a person in one occurrence and only part of a place, institution, street, organization, or other non-person referent in another. For a person occurrence, omit referent or set PERSON. When the current occurrence is clearly only a non-person referent, return that exact surface with referent NON_PERSON. A NON_PERSON row is suppression evidence for that occurrence. It is not a person candidate. Examples to mark NON_PERSON: פארוק in רחוב פארוק, פואד הראשון in רחוב פואד הראשון, סולימאן פאשה in רחוב סולימאן פאשה. If that same string refers to a person elsewhere, return that other occurrence with referent PERSON or omit referent, and set occurrence to its 1-based index. Do not use a memorized list of streets. Judge the occurrence in front of you.

A title or role without a name is not a candidate. Do not return הנשיא, הנשיאה, הוא, היא, האיש, or האישה by themselves. Keep the title when a name is part of the same mention. One title plus one name is one candidate, not two people. Title text is whatever the source says. It is not chosen from a list. For kind TITLE_AND_NAME, set surface to the exact full source slice, title_surface to the exact title or role slice, and name_surface to the exact name-only slice. All three are required. Examples: surface ד"ר משה מרזוק, title_surface ד"ר, name_surface משה מרזוק; surface השגריר האמריקאי ג'פרסון קאפרי, title_surface השגריר האמריקאי, name_surface ג'פרסון קאפרי; surface הלורד מויו, title_surface הלורד, name_surface מויו; surface והאלוף followed by a newline then בנימין גיבלי, title_surface והאלוף, name_surface בנימין גיבלי. Copy והאלוף as והאלוף. Do not shorten it to אלוף. Do not normalize, translate, or canonicalize a title. If you cannot point at both slices exactly, do not use TITLE_AND_NAME. Omit title_surface when the mention has no title. Do not invent a title or a name that is not an exact contiguous slice of surface.

When a Hebrew proclitic is attached to the name, set surface to the exact prefixed slice and name_surface to the exact unprefixed name inside it. Do this only when that remainder really is the name. Examples: לאהרוני with name_surface אהרוני; לויקטור לוי with name_surface ויקטור לוי; לאיסר כראל with name_surface איסר כראל. Do not peel letters from an ordinary word. Do not turn שלום into לום, and do not turn הלורד into לורד. Do not change spelling, punctuation, or OCR.

Each segment has break_after, the exact characters before the next segment. A person name, or a title immediately followed by a name, may cross one break_after whose value is a single newline. Set end_segment_id to the next segment id and copy those characters into surface. Keep the newline. Do not replace it with a space. Do not join segments across a blank line. Examples of full surfaces: האלוף followed by a newline then בנימין גיבלי; שמואל followed by a newline then עזר.

Set kind only to FULL_NAME, PARTIAL_NAME, TITLE_AND_NAME, NICKNAME, or UNCERTAIN_PERSON_NAME. If the same exact surface occurs more than once in the segment window, set occurrence to the 1-based left-to-right non-overlapping index of that copy. If it occurs once, omit occurrence. Set referent to PERSON or omit it for a person. Set referent to NON_PERSON only when this occurrence is not a person. Do not explain.
"""


@dataclass(frozen=True)
class ProviderDetection:
    proposals: list[GeminiProposal]
    calls: list[dict]


def require_gemini_api_key() -> str:
    """Return ``GEMINI_API_KEY``. Fail when it is missing or blank."""
    value = os.environ.get("GEMINI_API_KEY")
    if value is None or value.strip() == "":
        raise PilotConfigurationError(
            "GEMINI_API_KEY is required for the Gemini mention pilot"
        )
    return value.strip()


def build_generate_config():
    """Structured-output config. Decoding stays at the model default."""
    types = _types()
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_json_schema=CANDIDATE_RESPONSE_SCHEMA,
        thinking_config=types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.LOW,
        ),
        tools=None,
        safety_settings=None,
    )


def build_client(api_key: str):
    genai, types = _sdk()
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=REQUEST_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
    )


class GeminiMentionDetector:
    """Synchronous ``gemini-3.8-flash`` detector. No tools and no search."""

    def __init__(self, *, client=None, api_key: str | None = None):
        if client is not None:
            self._client = client
            return
        key = require_gemini_api_key() if api_key is None else api_key
        if key.strip() == "":
            raise PilotConfigurationError(
                "GEMINI_API_KEY is required for the Gemini mention pilot"
            )
        self._client = build_client(key.strip())

    def detect(self, segments: list[Segment]) -> ProviderDetection:
        proposals: list[GeminiProposal] = []
        calls: list[dict] = []
        config = build_generate_config()
        provider_errors = _provider_error_types()
        for batch in _batches(segments):
            response = _generate_content(
                self._client,
                _user_contents(batch),
                config,
                provider_errors,
            )
            calls.append(_call_record(response))
            proposals.extend(_proposals_from_response(response))
        return ProviderDetection(proposals=proposals, calls=calls)


def _generate_content(client, contents: str, config, provider_errors) -> object:
    """Call the SDK once. Retry stays inside the SDK. Secrets stay out of the message."""
    api_error, unknown_response, request_error = provider_errors
    try:
        return client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=config,
        )
    except api_error as exc:
        raise _pilot_error_from_api(exc) from None
    except unknown_response:
        raise PilotProviderError("provider returned an unreadable response") from None
    except request_error as exc:
        raise PilotProviderError(
            f"provider transport failed: {type(exc).__name__}"
        ) from None


def _pilot_error_from_api(exc: Exception) -> PilotProviderError:
    parts = [f"provider request failed: {type(exc).__name__}"]
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        parts.append(f"code={code}")
    status = getattr(exc, "status", None)
    if isinstance(status, str) and _SAFE_STATUS.fullmatch(status):
        parts.append(f"status={status}")
    return PilotProviderError(" ".join(parts))


def _provider_error_types():
    """Public SDK API errors, plus httpx transport failures the SDK does not wrap."""
    try:
        import httpx
        from google.genai import errors as genai_errors
    except ImportError as exc:
        raise PilotConfigurationError(
            "google-genai is required for the Gemini mention pilot"
        ) from exc
    return (
        genai_errors.APIError,
        genai_errors.UnknownApiResponseError,
        httpx.RequestError,
    )


def _user_contents(segments: list[Segment]) -> str:
    payload = [
        {
            "segment_id": segment.segment_id,
            "text": segment.text,
            "break_after": segment.break_after,
        }
        for segment in segments
    ]
    return (
        "Segments follow as JSON. Copy surfaces from segment text only.\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _batches(segments: list[Segment]) -> list[list[Segment]]:
    batches: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        if current and size + len(segment.text) > MAX_BATCH_CODE_POINTS:
            batches.append(current)
            current = []
            size = 0
        current.append(segment)
        size += len(segment.text)
    if current:
        batches.append(current)
    return batches


def _proposals_from_response(response: object) -> list[GeminiProposal]:
    _reject_blocked_or_truncated(response)
    text = getattr(response, "text", None)
    if not isinstance(text, str) or text.strip() == "":
        raise PilotProviderError("provider returned no candidate JSON text")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PilotProviderError("provider candidate text was not JSON") from exc
    return parse_candidate_payload(payload)


def _reject_blocked_or_truncated(response: object) -> None:
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(feedback, "block_reason", None) if feedback else None
    if block_reason not in (None, ""):
        raise PilotProviderError(
            f"provider blocked the prompt: {_public_value(block_reason)}"
        )
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return
    finish = _public_value(getattr(candidates[0], "finish_reason", None))
    if finish is not None and finish not in ACCEPTED_FINISH_REASONS:
        raise PilotProviderError(f"provider finish_reason was {finish}")


def _call_record(response: object) -> dict:
    usage = getattr(response, "usage_metadata", None)
    candidates = getattr(response, "candidates", None) or []
    finish = None
    if candidates:
        finish = _public_value(getattr(candidates[0], "finish_reason", None))
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = None
    if feedback is not None:
        block_reason = _public_value(getattr(feedback, "block_reason", None))
    return {
        "requested_model": MODEL_NAME,
        "model_version": _public_value(getattr(response, "model_version", None)),
        "response_id": _public_value(getattr(response, "response_id", None)),
        "finish_reason": finish,
        "prompt_block_reason": block_reason,
        "usage": {
            "prompt_token_count": _optional_int(usage, "prompt_token_count"),
            "candidates_token_count": _optional_int(usage, "candidates_token_count"),
            "thoughts_token_count": _optional_int(usage, "thoughts_token_count"),
            "total_token_count": _optional_int(usage, "total_token_count"),
        },
    }


def _optional_int(usage: object, name: str) -> int | None:
    if usage is None:
        return None
    value = getattr(usage, name, None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _public_value(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw)
    if text == "":
        return None
    return text


def _sdk():
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise PilotConfigurationError(
            "google-genai is required for the Gemini mention pilot"
        ) from exc
    return genai, types


def _types():
    return _sdk()[1]
