"""Exact source searches. The body is not normalized before matching."""

from __future__ import annotations


def occurrence_spans(text: str, surface: str) -> list[tuple[int, int]]:
    """Non-overlapping left-to-right matches. An empty surface matches nothing."""
    if surface == "":
        return []
    found: list[tuple[int, int]] = []
    cursor = 0
    while True:
        index = text.find(surface, cursor)
        if index < 0:
            return found
        found.append((index, index + len(surface)))
        cursor = index + len(surface)
