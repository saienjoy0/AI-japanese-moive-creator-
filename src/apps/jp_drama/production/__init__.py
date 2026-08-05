"""Unified production entry contracts and zero-call composition."""

from .composer import ProductionComposeError, ProductionEpisodeComposer
from .models import (
    EPISODE_COMPOSE_REPORT_SCHEMA_VERSION,
    PRODUCTION_ENTRY_SCHEMA_VERSION,
    SEGMENT_ARTIFACT_SCHEMA_VERSION,
    EpisodeComposeReport,
    ProductionPreflightReport,
    SegmentArtifact,
    SegmentArtifactManifest,
    SegmentComposeValidation,
)

__all__ = [
    "EPISODE_COMPOSE_REPORT_SCHEMA_VERSION",
    "PRODUCTION_ENTRY_SCHEMA_VERSION",
    "SEGMENT_ARTIFACT_SCHEMA_VERSION",
    "EpisodeComposeReport",
    "ProductionComposeError",
    "ProductionEpisodeComposer",
    "ProductionPreflightReport",
    "SegmentArtifact",
    "SegmentArtifactManifest",
    "SegmentComposeValidation",
]
