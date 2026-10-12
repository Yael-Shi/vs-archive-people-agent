"""Controlled failures for the experimental mention pilot."""

from __future__ import annotations

from vs_archive_people.artifacts.io import ArtifactError


class PilotConfigurationError(ArtifactError):
    """The pilot cannot start because local configuration is missing."""

    error_code = "PILOT_CONFIGURATION"


class PilotProviderError(ArtifactError):
    """The provider response cannot be used. No offsets are invented."""

    error_code = "PILOT_PROVIDER"


class PilotSplitConflictError(ArtifactError):
    """One source span has two exact splits, so neither is published."""

    error_code = "PILOT_SPLIT_CONFLICT"
