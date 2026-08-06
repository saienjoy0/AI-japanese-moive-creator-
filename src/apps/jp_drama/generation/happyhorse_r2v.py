"""Derive a HappyHorse 1.1 R2V plan and bundle from an approved route plan."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from ..assets.models import ApprovedAssetBundle
from ..generation.models import (
    GenerationCostItem,
    GenerationCostPlan,
    GenerationPlanEpisode,
    GenerationReadinessIssue,
    GenerationReadinessReport,
    GenerationRenderGraph,
    GenerationSegment,
    GenerationTaskNode,
)


HAPPYHORSE_R2V_ROUTE_ID = "dashscope/happyhorse-1.1-r2v"
HAPPYHORSE_R2V_PROFILE_ID = "happyhorse-1.1-r2v-auto-reference-v1"


class HappyHorsePlanError(RuntimeError):
    """A source plan cannot be safely retargeted to HappyHorse R2V."""


def derive_happyhorse_r2v_plan_and_bundle(
    source_plan: GenerationPlanEpisode,
    source_bundle: ApprovedAssetBundle,
    *,
    audio_strategy: str,
    price_snapshot_id: str,
    quoted_cost_cny_per_segment: Decimal,
) -> tuple[GenerationPlanEpisode, ApprovedAssetBundle]:
    if source_bundle.generation_plan_digest != source_plan.content_digest:
        raise HappyHorsePlanError(
            "source AssetBundle does not belong to the supplied GenerationPlan"
        )
    if quoted_cost_cny_per_segment < 0:
        raise HappyHorsePlanError("quoted cost must not be negative")
    snapshot = price_snapshot_id.strip()
    if not snapshot:
        raise HappyHorsePlanError("price_snapshot_id is required")

    mapped_audio = {
        "native_audio": "native_av",
        "external_tts": "external_audio_post",
        "silent": "silent",
    }.get(audio_strategy)
    if mapped_audio is None:
        raise HappyHorsePlanError(f"unsupported audio strategy: {audio_strategy}")

    segments: list[GenerationSegment] = []
    for source in source_plan.segments:
        references = [
            item
            for item in source.reference_asset_ids
            if item.startswith(("ref_char_", "ref_loc_", "ref_prop_"))
        ]
        if not references:
            raise HappyHorsePlanError(
                f"{source.segment_id} has no character/location/prop references"
            )
        if len(references) > 9:
            raise HappyHorsePlanError(
                f"{source.segment_id} exceeds HappyHorse's nine-image limit"
            )
        segments.append(
            source.model_copy(
                update={
                    "provider_route_id": HAPPYHORSE_R2V_ROUTE_ID,
                    "reference_asset_ids": references,
                    "audio_strategy": mapped_audio,
                }
            )
        )

    requirements = [
        item
        for item in source_plan.reference_asset_requirements
        if item.role != "first_frame"
    ]
    contracts = [
        item.model_copy(
            update={
                "reference_asset_ids": [
                    asset_id
                    for asset_id in item.reference_asset_ids
                    if not asset_id.startswith("ref_first_")
                ]
            }
        )
        for item in source_plan.continuity_contracts
    ]
    render_graph = _build_render_graph(segments)
    cost_plan = _build_cost_plan(
        segments,
        price_snapshot_id=snapshot,
        quoted_cost_cny_per_segment=quoted_cost_cny_per_segment,
    )
    warnings = list(source_plan.readiness_report.warnings)
    warnings.append(
        GenerationReadinessIssue(
            code="happyhorse_r2v_canary_required",
            scope="quality",
            severity="warning",
            message=(
                "HappyHorse R2V is a new paid route. Run one approval-gated "
                "segment canary before full-episode execution."
            ),
        )
    )
    derived_plan = GenerationPlanEpisode.build_with_digest(
        generation_plan_episode_id=_stable_id(
            "happyhorse_r2v_plan",
            source_plan.content_digest,
            HAPPYHORSE_R2V_ROUTE_ID,
            audio_strategy,
            snapshot,
        ),
        source_episode_id=source_plan.source_episode_id,
        source_prepared_episode_digest=source_plan.source_prepared_episode_digest,
        policy_digest=_digest(
            {
                "source_policy_digest": source_plan.policy_digest,
                "route": HAPPYHORSE_R2V_ROUTE_ID,
                "input_mode": "reference_images",
                "audio_strategy": audio_strategy,
                "max_reference_images": 9,
                "ratio": "9:16",
            }
        ),
        provider_profile_id=HAPPYHORSE_R2V_PROFILE_ID,
        provider_route_id=HAPPYHORSE_R2V_ROUTE_ID,
        timeline_fps=source_plan.timeline_fps,
        target_frame_count=source_plan.target_frame_count,
        target_duration_seconds=source_plan.target_duration_seconds,
        segments=segments,
        continuity_contracts=contracts,
        reference_asset_requirements=requirements,
        render_graph=render_graph,
        cost_plan=cost_plan,
        readiness_report=GenerationReadinessReport(
            planning_ready=True,
            execution_route_ready=True,
            media_quality_validated=False,
            external_api_calls=0,
            errors=[],
            warnings=warnings,
        ),
    )
    derived_bundle = ApprovedAssetBundle.build_with_digest(
        bundle_id=f"{source_bundle.bundle_id}_happyhorse_r2v",
        source_episode_id=source_bundle.source_episode_id,
        source_prepared_episode_digest=source_bundle.source_prepared_episode_digest,
        generation_plan_digest=derived_plan.content_digest,
        assets=[
            item
            for item in source_bundle.assets
            if item.role in {"character_master", "location_master", "prop_master"}
        ],
        voice_profiles=list(source_bundle.voice_profiles),
    )
    return derived_plan, derived_bundle


def _build_render_graph(
    segments: list[GenerationSegment],
) -> GenerationRenderGraph:
    nodes: list[GenerationTaskNode] = []
    validate_ids: list[str] = []
    for segment in segments:
        prepare_id = f"task_{segment.segment_id}_happyhorse_refs"
        render_id = f"task_{segment.segment_id}_happyhorse_r2v"
        validate_id = f"task_{segment.segment_id}_validate"
        nodes.extend(
            [
                GenerationTaskNode(
                    task_id=prepare_id,
                    segment_id=segment.segment_id,
                    task_type="prepare_references",
                    external_api_required=False,
                    provider_route_id=HAPPYHORSE_R2V_ROUTE_ID,
                ),
                GenerationTaskNode(
                    task_id=render_id,
                    segment_id=segment.segment_id,
                    task_type=(
                        "generate_native_av"
                        if segment.audio_strategy == "native_av"
                        else "generate_video"
                    ),
                    depends_on=[prepare_id],
                    external_api_required=True,
                    provider_route_id=HAPPYHORSE_R2V_ROUTE_ID,
                ),
                GenerationTaskNode(
                    task_id=validate_id,
                    segment_id=segment.segment_id,
                    task_type="validate_segment",
                    depends_on=[render_id],
                    external_api_required=False,
                    provider_route_id=HAPPYHORSE_R2V_ROUTE_ID,
                ),
            ]
        )
        validate_ids.append(validate_id)
    concat_id = "task_happyhorse_concat_episode"
    nodes.append(
        GenerationTaskNode(
            task_id=concat_id,
            task_type="concat_episode",
            depends_on=validate_ids,
            external_api_required=False,
            provider_route_id=HAPPYHORSE_R2V_ROUTE_ID,
        )
    )
    nodes.append(
        GenerationTaskNode(
            task_id="task_happyhorse_validate_episode",
            task_type="validate_episode",
            depends_on=[concat_id],
            external_api_required=False,
            provider_route_id=HAPPYHORSE_R2V_ROUTE_ID,
        )
    )
    return GenerationRenderGraph(nodes=nodes)


def _build_cost_plan(
    segments: list[GenerationSegment],
    *,
    price_snapshot_id: str,
    quoted_cost_cny_per_segment: Decimal,
) -> GenerationCostPlan:
    items = [
        GenerationCostItem(
            segment_id=segment.segment_id,
            component="video",
            calls=1,
            amount=quoted_cost_cny_per_segment,
            currency="CNY",
            confidence="exact",
            price_snapshot_id=price_snapshot_id,
        )
        for segment in segments
    ]
    total = quoted_cost_cny_per_segment * Decimal(len(segments))
    return GenerationCostPlan(
        reference_image_calls=0,
        video_calls=len(segments),
        tts_calls=0,
        native_audio_calls=sum(
            1 for item in segments if item.audio_strategy == "native_av"
        ),
        expected_calls=len(segments),
        hard_maximum_calls=len(segments),
        items=items,
        totals_by_currency={"CNY": total},
        unknown_cost_components=[],
        pricing_snapshot_dates=[price_snapshot_id],
    )


def _stable_id(*parts: object) -> str:
    digest = hashlib.sha256(
        "|".join(str(item) for item in parts).encode("utf-8")
    ).hexdigest()
    return f"happyhorse_r2v_{digest[:20]}"


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
