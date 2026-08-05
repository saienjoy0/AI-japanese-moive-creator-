"""Multi-episode storyboard plan import and compilation."""

from .compiler import (
    SERIES_IMPORT_COMPILER_VERSION,
    SUPPORTED_ROUTES,
    SeriesPlanError,
    build_episode_asset_bundle,
    load_series_inputs,
    validate_cross_contract,
)
from .contracts import (
    build_prepared_episode,
    compile_episode_generation_plan,
)
from .models import (
    ASSET_CATALOG_SCHEMA_VERSION,
    SERIES_MANIFEST_SCHEMA_VERSION,
    SERIES_PLAN_SCHEMA_VERSION,
    AssetCatalogEntry,
    SeriesAssetCatalog,
    SeriesDialogue,
    SeriesEpisode,
    SeriesEpisodeArtifact,
    SeriesGenerationPlan,
    SeriesProductionManifest,
    SeriesSegment,
    canonical_digest,
)

__all__ = [
    "ASSET_CATALOG_SCHEMA_VERSION",
    "SERIES_IMPORT_COMPILER_VERSION",
    "SERIES_MANIFEST_SCHEMA_VERSION",
    "SERIES_PLAN_SCHEMA_VERSION",
    "SUPPORTED_ROUTES",
    "AssetCatalogEntry",
    "SeriesAssetCatalog",
    "SeriesDialogue",
    "SeriesEpisode",
    "SeriesEpisodeArtifact",
    "SeriesGenerationPlan",
    "SeriesPlanError",
    "SeriesProductionManifest",
    "SeriesSegment",
    "build_episode_asset_bundle",
    "build_prepared_episode",
    "canonical_digest",
    "compile_episode_generation_plan",
    "load_series_inputs",
    "validate_cross_contract",
]
