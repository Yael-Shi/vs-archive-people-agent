"""Gemini 3.8 Flash semantic-surface adapter.

The adapter proposes surfaces. It does not assign identity, and it does not
read or log ``GEMINI_API_KEY``. Retry stays inside the SDK. This module does
not add a second retry loop, and it does not return provider payloads.
"""

from __future__ import annotations

import json
import os
import re

from vs_archive_people.artifacts.io import sha256_bytes
from vs_archive_people.find_mentions.candidates import (
    CANDIDATE_RESPONSE_SCHEMA,
    parse_candidate_payload,
)
from vs_archive_people.find_mentions.errors import (
    FindMentionsConfigurationError,
    FindMentionsProviderError,
)
from vs_archive_people.find_mentions.instruction import SYSTEM_INSTRUCTION
from vs_archive_people.find_mentions.proposals import SemanticProposal
from vs_archive_people.find_mentions.segment import Segment

MODEL_NAME = "gemini-3.8-flash"
REQUEST_TIMEOUT_MS = 120_000
MAX_BATCH_CODE_POINTS = 20_000
ACCEPTED_FINISH_REASONS = frozenset({"STOP", "FINISH_REASON_UNSPECIFIED"})
_SAFE_STATUS = re.compile(r"[A-Za-z0-9_]{1,64}")


def prompt_sha256() -> str:
    """SHA-256 of the semantic system instruction. The prompt text is not stored."""
    return sha256_bytes(SYSTEM_INSTRUCTION.encode("utf-8"))


def require_gemini_api_key() -> str:
    """Return ``GEMINI_API_KEY``. Fail when it is missing or blank."""
    value = os.environ.get("GEMINI_API_KEY")
    if value is None or value.strip() == "":
        raise FindMentionsConfigurationError(
            "GEMINI_API_KEY is required for official mention finding"
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


class GeminiSemanticDetector:
    """Synchronous ``gemini-3.8-flash`` detector. No tools and no search."""

    def __init__(self, *, client=None, api_key: str | None = None):
        if client is not None:
            self._client = client
            return
        key = require_gemini_api_key() if api_key is None else api_key
        if key.strip() == "":
            raise FindMentionsConfigurationError(
                "GEMINI_API_KEY is required for official mention finding"
            )
        self._client = build_client(key.strip())

    def detect(self, segments: list[Segment]) -> list[SemanticProposal]:
        proposals: list[SemanticProposal] = []
        config = build_generate_config()
        provider_errors = _provider_error_types()
        for batch in _batches(segments):
            response = _generate_content(
                self._client,
                _user_contents(batch),
                config,
                provider_errors,
            )
            proposals.extend(_proposals_from_response(response))
        return proposals


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
        raise _provider_error_from_api(exc) from None
    except unknown_response:
        raise FindMentionsProviderError(
            "provider returned an unreadable response"
        ) from None
    except request_error as exc:
        raise FindMentionsProviderError(
            f"provider transport failed: {type(exc).__name__}"
        ) from None


def _provider_error_from_api(exc: Exception) -> FindMentionsProviderError:
    parts = [f"provider request failed: {type(exc).__name__}"]
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        parts.append(f"code={code}")
    status = getattr(exc, "status", None)
    if isinstance(status, str) and _SAFE_STATUS.fullmatch(status):
        parts.append(f"status={status}")
    return FindMentionsProviderError(" ".join(parts))


def _provider_error_types():
    """Public SDK API errors, plus httpx transport failures the SDK does not wrap."""
    try:
        import httpx
        from google.genai import errors as genai_errors
    except ImportError as exc:
        raise FindMentionsConfigurationError(
            "google-genai is required for official mention finding"
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
    """Split on the code-point budget and repeat one segment across the cut.

    A semantic mention may cross one line break. The next batch starts with
    the previous batch's last segment so that boundary pair is sent together.
    An identical outer span from both batches becomes one proposal row later.
    """
    batches: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        if current and size + len(segment.text) > MAX_BATCH_CODE_POINTS:
            batches.append(current)
            overlap = current[-1]
            current = [overlap]
            size = len(overlap.text)
        current.append(segment)
        size += len(segment.text)
    if current:
        batches.append(current)
    return batches


def _proposals_from_response(response: object) -> list[SemanticProposal]:
    _reject_blocked_or_truncated(response)
    text = getattr(response, "text", None)
    if not isinstance(text, str) or text.strip() == "":
        raise FindMentionsProviderError("provider returned no candidate JSON text")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FindMentionsProviderError("provider candidate text was not JSON") from exc
    return parse_candidate_payload(payload)


def _reject_blocked_or_truncated(response: object) -> None:
    feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(feedback, "block_reason", None) if feedback else None
    if block_reason not in (None, ""):
        raise FindMentionsProviderError(
            f"provider blocked the prompt: {_public_value(block_reason)}"
        )
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return
    finish = _public_value(getattr(candidates[0], "finish_reason", None))
    if finish is not None and finish not in ACCEPTED_FINISH_REASONS:
        raise FindMentionsProviderError(f"provider finish_reason was {finish}")


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
        raise FindMentionsConfigurationError(
            "google-genai is required for official mention finding"
        ) from exc
    return genai, types


def _types():
    return _sdk()[1]
