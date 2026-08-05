"""Adaptive generation segmentation for Japanese short dramas."""

from .compiler import (
    GenerationCompilationError,
    compile_generation_plan,
    segment_to_generation_spec,
)
from .models import (
    GENERATION_COMPILER_VERSION,
    GENERATION_PLAN_SCHEMA_VERSION,
    ContinuityContract,
    DialogueSlice,
    EditorialShot,
    GenerationCostPlan,
    GenerationPlanEpisode,
    GenerationReadinessReport,
    GenerationRenderGraph,
    GenerationSegment,
    PromptBundle,
    ReferenceAssetRequirement,
    SegmentComplexity,
)
from .policy import DurationBand, ProviderSegmentationProfile, SegmentationPolicy
from .serialization import render_generation_summary, write_generation_artifacts

__all__ = [
    "GENERATION_COMPILER_VERSION",
    "GENERATION_PLAN_SCHEMA_VERSION",
    "ContinuityContract",
    "DialogueSlice",
    "DurationBand",
    "EditorialShot",
    "GenerationCompilationError",
    "GenerationCostPlan",
    "GenerationPlanEpisode",
    "GenerationReadinessReport",
    "GenerationRenderGraph",
    "GenerationSegment",
    "PromptBundle",
    "ProviderSegmentationProfile",
    "ReferenceAssetRequirement",
    "SegmentComplexity",
    "SegmentationPolicy",
    "compile_generation_plan",
    "render_generation_summary",
    "segment_to_generation_spec",
    "write_generation_artifacts",
]
