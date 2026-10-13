"""Controlled failures for official mention finding."""

from __future__ import annotations

from vs_archive_people.artifacts.io import ArtifactError


class FindMentionsConfigurationError(ArtifactError):
    """The finder cannot start because local configuration is missing."""

    error_code = "FIND_MENTIONS_CONFIGURATION"


class FindMentionsProviderError(ArtifactError):
    """The semantic provider response cannot be used. No offsets are invented."""

    error_code = "FIND_MENTIONS_PROVIDER"


class DiagnosticsPublicationFailed(ArtifactError):
    """The proposal file is already published and the sidecar was not."""

    error_code = "DIAGNOSTICS_PUBLICATION_FAILED"

    def __init__(
        self,
        reason: str,
        *,
        proposal_output: str,
        proposal_sha256: str,
        proposal_logical_sha256: str,
    ) -> None:
        super().__init__(reason)
        self.proposal_output = proposal_output
        self.proposal_sha256 = proposal_sha256
        self.proposal_logical_sha256 = proposal_logical_sha256
