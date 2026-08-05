"""Safety wrapper around the Seedance storyboard production bridge.

The base bridge performs provider-specific compilation. This module rebuilds
continuity requirements by actual continuity group and prevents a continuation
storyboard from being reported execution-ready until its previous-video asset
can be hash-bound by the approved-asset contract.
"""

from __future__ import annotations

from collections import defaultdict

from ..generation.models import (
    ContinuityContract,
    GenerationPlanEpisode,
    GenerationReadinessIssue,
    ReferenceAssetRequirement,
)
from ..preparation.models import PreparedEpisode
from ..rendering.provider_registry import ProviderRegistry
from .bridge import (
    build_storyboard_asset_bundle,
    build_storyboard_prepared_episode,
    compile_storyboard_generation_plan as _compile_storyboard_generation_plan,
)
from .models import SeedanceStoryboardPackage, StoryboardAsset


def compile_storyboard_generation_plan(
    package: SeedanceStoryboardPackage,
    prepared: PreparedEpisode,
    episode_id: str,
    *,
    route_id: str,
    registry: ProviderRegistry,
) -> GenerationPlanEpisode:
    """Compile and then enforce production-safe continuity/readiness semantics."""
    plan = _compile_storyboard_generation_plan(
        package,
        prepared,
        episode_id,
        route_id=route_id,
        registry=registry,
    )
    contracts, requirements = _rebuild_continuity(package, plan)
    episode = next(item for item in package.episodes if item.episode_id == episode_id)

    errors = list(plan.readiness_report.errors)
    if episode.continuation_source and not any(
        item.code == "continuation_reference_not_bound" for item in errors
    ):
        errors.append(
            GenerationReadinessIssue(
                code="continuation_reference_not_bound",
                scope="route",
                severity="error",
                message=(
                    f"{episode_id} requires approved previous video "
                    f"@{episode.continuation_source}, but the current ApprovedAssetBundle "
                    "cannot yet hash-bind video references. Provider submission is blocked."
                ),
                segment_id=plan.segments[0].segment_id,
                source_shot_id=plan.segments[0].parent_shot_ids[0],
            )
        )

    readiness = plan.readiness_report.model_copy(
        update={
            "execution_route_ready": (
                plan.readiness_report.planning_ready and not errors
            ),
            "errors": errors,
        }
    )
    # Keep nested Pydantic models typed while the canonical digest is computed.
    # Dumping them to dicts before model_construct changes serializer behavior
    # and creates a digest that no longer matches the validated model.
    return GenerationPlanEpisode.build_with_digest(
        schema_version=plan.schema_version,
        compiler_version=plan.compiler_version,
        generation_plan_episode_id=plan.generation_plan_episode_id,
        source_episode_id=plan.source_episode_id,
        source_prepared_episode_digest=plan.source_prepared_episode_digest,
        policy_digest=plan.policy_digest,
        provider_profile_id=plan.provider_profile_id,
        provider_route_id=plan.provider_route_id,
        timeline_fps=plan.timeline_fps,
        target_frame_count=plan.target_frame_count,
        target_duration_seconds=plan.target_duration_seconds,
        segments=plan.segments,
        continuity_contracts=contracts,
        reference_asset_requirements=requirements,
        render_graph=plan.render_graph,
        cost_plan=plan.cost_plan,
        readiness_report=readiness,
    )


def _rebuild_continuity(
    package: SeedanceStoryboardPackage,
    plan: GenerationPlanEpisode,
) -> tuple[list[ContinuityContract], list[ReferenceAssetRequirement]]:
    assets = {item.asset_id: item for item in package.assets}
    segments_by_group = defaultdict(list)
    for segment in plan.segments:
        segments_by_group[segment.continuity_group_id].append(segment)

    requirements: dict[str, ReferenceAssetRequirement] = {}
    contracts: list[ContinuityContract] = []
    for group_id, segments in sorted(segments_by_group.items()):
        locations = {item.location_id for item in segments}
        if len(locations) != 1:
            raise ValueError(
                f"continuity group {group_id} contains several locations: "
                f"{sorted(locations)}"
            )
        location_id = next(iter(locations))
        location = _require_asset(assets, location_id, "scene")
        character_ids = sorted(
            {character_id for segment in segments for character_id in segment.character_ids}
        )
        prop_ids = sorted(
            {prop_id for segment in segments for prop_id in segment.prop_ids}
        )
        reference_ids: set[str] = set()

        for segment in segments:
            for asset_id, kind, role, prefix in [
                *[
                    (item, "character", "character", "ref_char_")
                    for item in segment.character_ids
                ],
                (segment.location_id, "scene", "location", "ref_loc_"),
                *[
                    (item, "prop", "prop", "ref_prop_")
                    for item in segment.prop_ids
                ],
            ]:
                asset = _require_asset(assets, asset_id, kind)
                reference_id = prefix + asset.asset_id
                reference_ids.add(reference_id)
                previous = requirements.get(reference_id)
                required_for = sorted(
                    set(
                        (previous.required_for_segment_ids if previous else [])
                        + [segment.segment_id]
                    )
                )
                requirements[reference_id] = ReferenceAssetRequirement(
                    asset_id=reference_id,
                    role=role,
                    subject_id=asset.asset_id,
                    continuity_group_id=group_id,
                    required_for_segment_ids=required_for,
                )

            first_frame_id = f"ref_first_{segment.segment_id}"
            if first_frame_id in segment.reference_asset_ids:
                reference_ids.add(first_frame_id)
                requirements[first_frame_id] = ReferenceAssetRequirement(
                    asset_id=first_frame_id,
                    role="first_frame",
                    subject_id=segment.segment_id,
                    continuity_group_id=group_id,
                    required_for_segment_ids=[segment.segment_id],
                )

        contracts.append(
            ContinuityContract(
                continuity_group_id=group_id,
                character_appearance_locks={
                    item: _require_asset(assets, item, "character").prompt
                    for item in character_ids
                },
                location_id=location_id,
                location_lock=location.prompt,
                prop_state_locks={
                    item: _require_asset(assets, item, "prop").prompt
                    for item in prop_ids
                },
                reference_asset_ids=sorted(reference_ids),
            )
        )

    return contracts, sorted(requirements.values(), key=lambda item: item.asset_id)


def _require_asset(
    assets: dict[str, StoryboardAsset],
    asset_id: str,
    expected_kind: str,
) -> StoryboardAsset:
    asset = assets.get(asset_id)
    if asset is None:
        raise ValueError(f"generation plan references unknown asset {asset_id}")
    if asset.kind != expected_kind:
        raise ValueError(
            f"asset {asset_id} must be {expected_kind}, got {asset.kind}"
        )
    return asset


__all__ = [
    "build_storyboard_asset_bundle",
    "build_storyboard_prepared_episode",
    "compile_storyboard_generation_plan",
]
