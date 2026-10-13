"""Detector registry entries written by the official finder.

The ids are data on ``mention-proposals-v1``. They are not schema keys.
"""

from __future__ import annotations

SEMANTIC_DETECTOR_ID = "semantic-surface"
KNOWN_NAME_DETECTOR_ID = "known-name"
SEMANTIC_IMPLEMENTATION = "gemini-3.8-flash"
IMPLEMENTATION_VERSION = "1"
DETECTOR_ID_ORDER = (SEMANTIC_DETECTOR_ID, KNOWN_NAME_DETECTOR_ID)


def official_detectors(prompt_sha256: str) -> list[dict]:
    """Return the official registry, semantic entry first."""
    return [
        {
            "id": SEMANTIC_DETECTOR_ID,
            "role": "semantic",
            "implementation": SEMANTIC_IMPLEMENTATION,
            "version": IMPLEMENTATION_VERSION,
            "reproducibility": {"prompt_sha256": prompt_sha256},
        },
        {
            "id": KNOWN_NAME_DETECTOR_ID,
            "role": "deterministic",
            "implementation": "exact-literal",
            "version": IMPLEMENTATION_VERSION,
        },
    ]


def canonical_detector_ids(detector_ids: set[str]) -> list[str]:
    ordered = [
        detector_id for detector_id in DETECTOR_ID_ORDER if detector_id in detector_ids
    ]
    extra = sorted(detector_ids - set(ordered))
    return ordered + extra
