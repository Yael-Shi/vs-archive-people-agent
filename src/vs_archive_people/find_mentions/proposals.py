"""Provider-neutral semantic surface proposals.

A detector proposes exact source surfaces. It does not propose offsets, and it
does not assign a production identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from vs_archive_people.find_mentions.segment import Segment


@dataclass(frozen=True)
class SemanticProposal:
    segment_id: str
    surface: str
    kind: str | None
    occurrence: int | None
    name_surface: str | None = None
    title_surface: str | None = None
    end_segment_id: str | None = None
    referent: str | None = None


class SemanticDetector(Protocol):
    def detect(self, segments: list[Segment]) -> list[SemanticProposal]: ...
