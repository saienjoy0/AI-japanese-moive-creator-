"""Public PR11 compiler facade with capability-safe post-processing."""

from __future__ import annotations

import math
from typing import Any

from . import compiler as _base
from .models import GenerationPlanEpisode


GenerationCompilationError = _base.GenerationCompilationError
segment_to_generation_spec = _base.segment_to_generation_spec


class _CapabilityPlanningAdapter:
    """Expose a wider planning range while validating against the real adapter."""

    def __init__(self, adapter: Any, planning_max_seconds: int) -> None:
        self._adapter = adapter
        self._planning_max_seconds = planning_max_seconds

    def capabilities(self):
        capabilities = self._adapter.capabilities()
        return capabilities.model_copy(
            update={
                "max_duration_seconds": max(
                    capabilities.max_duration_seconds,
                    self._planning_max_seconds,
                )
            }
        )

    def validate(self, request):
        return self._adapter.validate(request)

    def estimate_cost(self, request):
        return self._adapter.estimate_cost(request)

    def __getattr__(self, name: str):
        return getattr(self._adapter, name)


class _PlanningRegistry:
    def __init__(self, registry: Any, route_id: str, adapter: Any) -> None:
        self._registry = registry
        self._route_id = route_id
        self._adapter = adapter

    def require(self, route_id: str):
        if route_id == self._route_id:
            return self._adapter
        return self._registry.require(route_id)

    def get(self, route_id: str):
        if route_id == self._route_id:
            return self._adapter
        return self._registry.get(route_id)


def compile_generation_plan(prepared, *, profile, registry) -> GenerationPlanEpisode:
    """Compile and normalize call counts/reference requirements without paid calls."""
    real_adapter = registry.require(profile.route_id)
    real_capabilities = real_adapter.capabilities()
    fps = prepared.project_draft.fps

    global_start = 0
    longest_explicit_seconds = 1
    for frame in sorted(prepared.storyboard_frame_drafts, key=lambda item: item.order):
        units = _base._extract_atomic_units(frame, global_start, fps)
        longest_explicit_seconds = max(
            longest_explicit_seconds,
            *(math.ceil(item.frame_count / fps) for item in units),
        )
        global_start += round(frame.duration_seconds * fps)

    effective_registry = registry
    if longest_explicit_seconds > real_capabilities.max_duration_seconds:
        effective_registry = _PlanningRegistry(
            registry,
            profile.route_id,
            _CapabilityPlanningAdapter(real_adapter, longest_explicit_seconds),
        )

    plan = _base.compile_generation_plan(
        prepared,
        profile=profile,
        registry=effective_registry,
    )

    use_i2v = real_capabilities.image_to_video and not real_capabilities.text_to_video
    if profile.route_id == "wan/i2v":
        use_i2v = True

    segments = plan.segments
    contracts = plan.continuity_contracts
    requirements = plan.reference_asset_requirements
    if not use_i2v:
        segments = [
            segment.model_copy(
                update={
                    "reference_asset_ids": [
                        item
                        for item in segment.reference_asset_ids
                        if not item.startswith("ref_first_")
                    ]
                }
            )
            for segment in segments
        ]
        contracts = [
            contract.model_copy(
                update={
                    "reference_asset_ids": [
                        item
                        for item in contract.reference_asset_ids
                        if not item.startswith("ref_first_")
                    ]
                }
            )
            for contract in contracts
        ]
        requirements = [item for item in requirements if item.role != "first_frame"]

    expected_calls = (
        plan.cost_plan.reference_image_calls
        + plan.cost_plan.video_calls
        + plan.cost_plan.tts_calls
    )
    cost_plan = plan.cost_plan.model_copy(
        update={
            "expected_calls": expected_calls,
            "hard_maximum_calls": expected_calls * 2,
        }
    )

    payload = {
        field_name: getattr(plan, field_name)
        for field_name in GenerationPlanEpisode.model_fields
        if field_name != "content_digest"
    }
    payload.update(
        segments=segments,
        continuity_contracts=contracts,
        reference_asset_requirements=requirements,
        cost_plan=cost_plan,
    )
    return GenerationPlanEpisode.build_with_digest(**payload)
