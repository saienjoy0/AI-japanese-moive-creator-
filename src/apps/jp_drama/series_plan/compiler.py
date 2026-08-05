"""Compile a strict multi-episode storyboard plan into production contracts."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path

import yaml

from ..assets import ApprovedAssetBundle, build_pending_asset_bundle
from ..assets.bundle import prepared_content_digest
from ..domain import Currency, RenderStrategy
from ..generation.compiler import segment_to_generation_spec
from ..generation.models import (
    ContinuityContract,
    DialogueSlice,
    EditorialShot,
    GenerationCostItem,
    GenerationCostPlan,
    GenerationPlanEpisode,
    GenerationReadinessIssue,
    GenerationReadinessReport,
    GenerationRenderGraph,
    GenerationSegment,
    GenerationTaskNode,
    PromptBundle,
    ReferenceAssetRequirement,
    SegmentComplexity,
)
from ..preparation.models import (
    AudioDraft,
    BudgetSnapshot,
    CameraDraft,
    CharacterSeed,
    DialogueDraft,
    LocationSeed,
    MappingEntry,
    MappingTrace,
    PreparedEpisode,
    ProjectDraft,
    PropSeed,
    ReadinessIssue,
    ReadinessReport,
    RenderGraph,
    RenderIntent,
    RenderTaskNode,
    ShotBudgetItem,
    StoryboardFrameDraft,
)
from ..rendering.provider_core import ProviderCapabilitiesRequired, ShotGenerationSpec
from ..rendering.provider_registry import ProviderRegistry
from .models import (
    AssetCatalogEntry,
    SeriesAssetCatalog,
    SeriesDialogue,
    SeriesEpisode,
    SeriesGenerationPlan,
    SeriesSegment,
    canonical_digest,
)


SERIES_IMPORT_COMPILER_VERSION = "1.0.0"
SUPPORTED_ROUTES = {
    "h3": "minimax/h3-reference-av",
    "wan": "wan/i2v",
    "seedance": "seedance/platform",
}
_NON_LIP_SYNC_MODES = {"inner_monologue", "voice_over", "memory_voice"}


class SeriesPlanError(RuntimeError):
    """The source series contracts cannot be compiled without invention."""


def load_series_inputs(
    series_plan_path: str | Path,
    asset_catalog_path: str | Path,
) -> tuple[SeriesGenerationPlan, SeriesAssetCatalog]:
    try:
        raw_plan = yaml.safe_load(Path(series_plan_path).read_text(encoding="utf-8"))
        raw_catalog = yaml.safe_load(Path(asset_catalog_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SeriesPlanError(f"cannot load series YAML: {exc}") from exc
    if not isinstance(raw_plan, dict) or not isinstance(raw_catalog, dict):
        raise SeriesPlanError("series plan and asset catalogue must be YAML objects")
    plan = SeriesGenerationPlan.model_validate(raw_plan)
    catalog = SeriesAssetCatalog.model_validate(raw_catalog)
    validate_cross_contract(plan, catalog)
    return plan, catalog


def validate_cross_contract(
    plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
) -> None:
    errors: list[str] = []
    if plan.project_id != catalog.project_id:
        errors.append("project_id differs between series plan and asset catalogue")
    if plan.title != catalog.title:
        errors.append("title differs between series plan and asset catalogue")
    known = catalog.by_id
    episode_ids = {item.episode_id for item in plan.episodes}
    referenced_by_episode: dict[str, set[str]] = {item: set() for item in episode_ids}

    for episode in plan.episodes:
        for segment in episode.segments:
            expected_frames = segment.requested_duration_seconds * plan.production.timeline_fps
            if segment.editorial_frame_count != expected_frames:
                errors.append(
                    f"{segment.segment_id} editorial frames do not equal requested seconds at FPS"
                )
            references = {
                *segment.location_ids,
                *segment.character_ids,
                *segment.background_character_ids,
                *segment.prop_ids,
                *(item.speaker for item in segment.dialogue),
            }
            referenced_by_episode[episode.episode_id].update(references)
            unknown = sorted(references - set(known))
            if unknown:
                errors.append(f"{segment.segment_id} references unknown assets: {unknown}")
            for dialogue in segment.dialogue:
                asset = known.get(dialogue.speaker)
                if asset is not None and asset.kind != "character":
                    errors.append(
                        f"{segment.segment_id} dialogue speaker {dialogue.speaker} is not a character"
                    )

    for episode_id, references in referenced_by_episode.items():
        for asset_id in references:
            asset = known.get(asset_id)
            if asset is not None and episode_id not in asset.used_in_episode_ids:
                errors.append(
                    f"asset {asset_id} is used in {episode_id} but catalogue usage omits it"
                )

    continuity_ids = {
        *plan.continuity_contract.character_ids,
        *plan.continuity_contract.locations,
        *plan.continuity_contract.prop_state_tracking,
    }
    unknown_continuity = sorted(continuity_ids - set(known))
    if unknown_continuity:
        errors.append(f"continuity contract references unknown assets: {unknown_continuity}")
    for asset_id in plan.continuity_contract.character_ids:
        if asset_id in known and known[asset_id].kind != "character":
            errors.append(f"continuity character {asset_id} is not a character asset")
    for asset_id in plan.continuity_contract.locations:
        if asset_id in known and known[asset_id].kind != "scene":
            errors.append(f"continuity location {asset_id} is not a scene asset")
    for asset_id in plan.continuity_contract.prop_state_tracking:
        if asset_id in known and known[asset_id].kind != "prop":
            errors.append(f"continuity prop {asset_id} is not a prop asset")

    if plan.provider_policy.generated_text_in_video:
        errors.append("generated_text_in_video must remain false")
    if plan.source.source_kind != "public_domain_literary_work":
        errors.append("only the declared public-domain literary source is accepted")
    if errors:
        raise SeriesPlanError("; ".join(errors))


def build_prepared_episode(
    plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
    episode_id: str,
) -> PreparedEpisode:
    episode = _episode(plan, episode_id)
    by_id = catalog.by_id
    source_digest = _series_source_digest(plan, catalog)
    key = source_digest.split(":", 1)[1][:12]
    episode_number = int(episode_id[1:])
    prepared_episode_id = f"{plan.project_id}-{episode_id.lower()}"
    used_character_ids = sorted(
        {
            *(
                item
                for segment in episode.segments
                for item in segment.visual_character_ids
            ),
            *(
                dialogue.speaker
                for segment in episode.segments
                for dialogue in segment.dialogue
            ),
        }
    )
    used_location_ids = sorted(
        {item for segment in episode.segments for item in segment.location_ids}
    )
    used_prop_ids = sorted({item for segment in episode.segments for item in segment.prop_ids})

    frames: list[StoryboardFrameDraft] = []
    intents: list[RenderIntent] = []
    graph_nodes: list[RenderTaskNode] = []
    budget_items: list[ShotBudgetItem] = []
    warnings: list[ReadinessIssue] = [
        ReadinessIssue(
            code="source_public_domain_declared",
            severity="warning",
            message=(
                f"Source is declared public domain: {plan.source.author}『{plan.source.title}』. "
                "Preserve provenance through publication review."
            ),
        )
    ]

    for order, segment in enumerate(episode.segments, start=1):
        shot_id = segment.segment_id
        duration = segment.editorial_frame_count / plan.production.timeline_fps
        dialogue = _dialogue_drafts(segment, duration)
        primary_location = segment.location_ids[0]
        visual_characters = segment.visual_character_ids
        intent_id = f"intent_{shot_id}"
        task_video = f"task_{shot_id}_video"
        task_tts = f"task_{shot_id}_tts"
        task_subtitle = f"task_{shot_id}_subtitles"
        task_mux = f"task_{shot_id}_mux"
        task_finalize = f"task_{shot_id}_finalize"
        tasks = [task_video]
        graph_nodes.append(
            RenderTaskNode(
                task_id=task_video,
                shot_id=shot_id,
                task_type="generate_video",
                external_api_required=True,
                provider_required=True,
            )
        )
        mux_dependencies = [task_video]
        if dialogue:
            graph_nodes.append(
                RenderTaskNode(
                    task_id=task_tts,
                    shot_id=shot_id,
                    task_type="generate_tts",
                    external_api_required=True,
                    provider_required=True,
                )
            )
            tasks.append(task_tts)
            mux_dependencies.append(task_tts)
        graph_nodes.append(
            RenderTaskNode(
                task_id=task_subtitle,
                shot_id=shot_id,
                task_type="generate_subtitles",
                external_api_required=False,
                provider_required=False,
            )
        )
        tasks.append(task_subtitle)
        mux_dependencies.append(task_subtitle)
        graph_nodes.append(
            RenderTaskNode(
                task_id=task_mux,
                shot_id=shot_id,
                task_type="mux_audio_video",
                depends_on=mux_dependencies,
                external_api_required=False,
                provider_required=False,
            )
        )
        graph_nodes.append(
            RenderTaskNode(
                task_id=task_finalize,
                shot_id=shot_id,
                task_type="finalize_shot",
                depends_on=[task_mux],
                external_api_required=False,
                provider_required=False,
            )
        )
        tasks.extend([task_mux, task_finalize])

        frames.append(
            StoryboardFrameDraft(
                frame_id=f"frame_{shot_id}",
                source_shot_id=shot_id,
                adapted_beat_id=f"adapted_{shot_id}",
                order=order,
                duration_seconds=duration,
                location_seed_id=primary_location,
                character_seed_ids=visual_characters,
                prop_seed_ids=list(segment.prop_ids),
                action=segment.central_action,
                visual_description=_prepared_visual_description(
                    plan,
                    catalog,
                    segment,
                ),
                camera=_camera_for(segment),
                dialogue_cues=dialogue,
                audio=AudioDraft(
                    ambience="1880年代の横浜山手の学校、控えめな室内環境音",
                    sound_effects=_sound_effects(segment),
                    bgm_cue="感情を説明しすぎない静かな短劇音楽",
                    generated_native_audio=False,
                ),
                render_intent_id=intent_id,
            )
        )
        intents.append(
            RenderIntent(
                intent_id=intent_id,
                shot_id=shot_id,
                requested_strategy=RenderStrategy.VIDEO_PLUS_TTS,
                resolved_strategy=RenderStrategy.VIDEO_PLUS_TTS,
                provider="series-production-plan",
                model="provider-route-selected-later",
                model_capabilities_required=["video", "9:16"],
                tasks=tasks,
                estimated_primary_cost=Decimal("0"),
                reserved_retry_cost=Decimal("0"),
                estimated_total_cost=Decimal("0"),
            )
        )
        budget_items.append(
            ShotBudgetItem(
                shot_id=shot_id,
                strategy=RenderStrategy.VIDEO_PLUS_TTS,
                primary_cost=Decimal("0"),
                retry_cost=Decimal("0"),
                total_cost=Decimal("0"),
            )
        )
        if _dialogue_density_warning(segment, duration):
            warnings.append(
                ReadinessIssue(
                    code="dialogue_density_manual_review",
                    severity="warning",
                    message=(
                        f"{shot_id} dialogue is compressed into {duration:.1f}s; "
                        "review speaking rate and reaction-shot coverage."
                    ),
                    shot_id=shot_id,
                    field="dialogue",
                )
            )
        if len(segment.location_ids) > 1:
            warnings.append(
                ReadinessIssue(
                    code="multi_location_memory_transition",
                    severity="warning",
                    message=(
                        f"{shot_id} uses {segment.location_ids}; the first location is the "
                        "primary continuity location and the others remain explicit references."
                    ),
                    shot_id=shot_id,
                    field="location_ids",
                )
            )

    warnings.extend(
        ReadinessIssue(
            code="series_manual_review_required",
            severity="warning",
            message=item,
        )
        for item in plan.manual_review_required
    )
    mapping_assets = [
        MappingEntry(source_id=item, target_id=item)
        for item in [*used_character_ids, *used_location_ids, *used_prop_ids]
    ]
    return PreparedEpisode(
        source_digest=source_digest,
        package_id=f"package_{key}_{episode_id.lower()}",
        episode_id=prepared_episode_id,
        project_draft=ProjectDraft(
            project_id=f"project_{plan.project_id}_{episode_id.lower()}",
            title=f"{plan.title} {episode_id} {episode.title}",
            description=(
                f"{plan.source.author}『{plan.source.title}』を三話完結の日本語短劇へ翻案。"
                f"原作権利宣言=public_domain; {plan.source.adaptation_note}"
            ),
            fps=plan.production.timeline_fps,
            target_duration_seconds=float(episode.editorial_duration_seconds),
            episode_number=episode_number,
            series_id=plan.project_id,
        ),
        character_seeds=[
            CharacterSeed(
                seed_id=item,
                source_character_id=item,
                name=by_id[item].name,
                description=by_id[item].description,
                speech_style=(
                    "内気で短い言葉"
                    if item == "C01"
                    else "穏やかな少年の話し方"
                    if item == "C02"
                    else "静かで明瞭な教師の話し方"
                    if item == "C03"
                    else None
                ),
                visual_prompt=by_id[item].prompt,
                negative_prompt=by_id[item].negative_prompt,
            )
            for item in used_character_ids
        ],
        location_seeds=[
            LocationSeed(
                seed_id=item,
                source_location_id=item,
                name=by_id[item].name,
                description=by_id[item].description,
                continuity_rules=[
                    rule for rule in catalog.continuity_rules if item in rule or "人物" not in rule
                ],
                visual_prompt=by_id[item].prompt,
            )
            for item in used_location_ids
        ],
        prop_seeds=[
            PropSeed(
                seed_id=item,
                source_prop_id=item,
                name=by_id[item].name,
                story_function=by_id[item].story_function or by_id[item].description,
                visual_prompt=(
                    by_id[item].prompt
                    + _prop_state_suffix(plan, by_id[item], episode_id)
                ),
            )
            for item in used_prop_ids
        ],
        storyboard_frame_drafts=frames,
        render_intents=intents,
        render_graph=RenderGraph(nodes=graph_nodes),
        budget_snapshot=BudgetSnapshot(
            currency=Currency.USD,
            budget_limit=Decimal("0"),
            contingency_rate=Decimal("0"),
            shot_items=budget_items,
            subtotal=Decimal("0"),
            contingency=Decimal("0"),
            estimated_total=Decimal("0"),
            within_budget=True,
            hard_stop=False,
        ),
        mapping_trace=MappingTrace(
            characters=[item for item in mapping_assets if item.source_id.startswith("C")],
            locations=[item for item in mapping_assets if item.source_id.startswith("S")],
            props=[item for item in mapping_assets if item.source_id.startswith("P")],
            shots=[
                MappingEntry(source_id=item.segment_id, target_id=f"frame_{item.segment_id}")
                for item in episode.segments
            ],
            adapted_beats=[
                MappingEntry(
                    source_id=item.segment_id,
                    target_id=f"adapted_{item.segment_id}",
                )
                for item in episode.segments
            ],
            mapping_coverage=1.0,
        ),
        readiness_report=ReadinessReport(
            package_id=f"package_{key}_{episode_id.lower()}",
            episode_id=prepared_episode_id,
            duration_seconds=float(episode.editorial_duration_seconds),
            shot_count=len(frames),
            character_count=len(used_character_ids),
            location_count=len(used_location_ids),
            prop_count=len(used_prop_ids),
            mapping_coverage=1.0,
            resolved_render_intents=len(intents),
            total_render_intents=len(intents),
            budget_limit=Decimal("0"),
            estimated_total=Decimal("0"),
            currency=Currency.USD,
            generation_ready=True,
            warnings=warnings,
        ),
    )


def compile_episode_generation_plan(
    source_plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
    prepared: PreparedEpisode,
    episode_id: str,
    *,
    route_id: str,
    registry: ProviderRegistry,
) -> GenerationPlanEpisode:
    if route_id not in set(SUPPORTED_ROUTES.values()):
        raise SeriesPlanError(f"unsupported route: {route_id}")
    episode = _episode(source_plan, episode_id)
    if prepared.source_digest != _series_source_digest(source_plan, catalog):
        raise SeriesPlanError("PreparedEpisode belongs to another series source")
    if prepared.project_draft.episode_number != int(episode_id[1:]):
        raise SeriesPlanError("PreparedEpisode episode number does not match source episode")
    adapter = registry.require(route_id)
    capabilities = adapter.capabilities()
    fps = source_plan.production.timeline_fps
    prepared_digest = prepared_content_digest(prepared)
    segments: list[GenerationSegment] = []
    route_errors: list[GenerationReadinessIssue] = []
    warnings: list[GenerationReadinessIssue] = []

    for order, source_segment in enumerate(episode.segments, start=1):
        primary_location = source_segment.location_ids[0]
        group_id = _continuity_group_id(source_plan.project_id, primary_location)
        visual_characters = source_segment.visual_character_ids
        dialogue_slices = _dialogue_slices(source_segment, fps)
        reference_ids = _reference_ids(
            group_id,
            source_segment,
            requires_first_frame=route_id == "wan/i2v",
        )
        complexity = _complexity(source_segment)
        audio_strategy = (
            "native_av"
            if source_segment.dialogue
            and route_id in {"minimax/h3-reference-av", "seedance/platform"}
            else "external_audio_post"
            if source_segment.dialogue
            else "silent"
        )
        segment = GenerationSegment(
            segment_id=source_segment.segment_id,
            order=order,
            parent_shot_ids=[source_segment.segment_id],
            continuity_group_id=group_id,
            provider_route_id=route_id,
            timeline_fps=fps,
            editorial_start_frame=source_segment.editorial_start_frame,
            editorial_end_frame=source_segment.editorial_end_frame,
            editorial_frame_count=source_segment.editorial_frame_count,
            editorial_duration_seconds=(
                Decimal(source_segment.editorial_frame_count) / Decimal(fps)
            ),
            requested_duration_seconds=source_segment.requested_duration_seconds,
            used_start_frame=0,
            used_end_frame=source_segment.editorial_frame_count,
            complexity=complexity,
            editorial_shots=[
                EditorialShot(
                    editorial_shot_id=f"editorial_{source_segment.segment_id}",
                    segment_id=source_segment.segment_id,
                    order_within_segment=1,
                    start_frame=0,
                    end_frame=source_segment.editorial_frame_count,
                    framing=_camera_for(source_segment).shot_size,
                    camera_movement=_camera_for(source_segment).movement,
                    visual_action=source_segment.central_action,
                    emotion=f"{source_segment.emotion_start} → {source_segment.emotion_end}",
                    character_ids=visual_characters,
                    active_speaker_id=_active_speaker(source_segment),
                    dialogue_slice_ids=[item.dialogue_slice_id for item in dialogue_slices],
                    source_beat_ids=[f"adapted_{source_segment.segment_id}"],
                    source_shot_ids=[source_segment.segment_id],
                )
            ],
            character_ids=visual_characters,
            location_id=primary_location,
            prop_ids=list(source_segment.prop_ids),
            dialogue_slices=dialogue_slices,
            reference_asset_ids=reference_ids,
            prompt_bundle=_prompt_bundle(source_plan, catalog, source_segment),
            audio_strategy=audio_strategy,
            transition_in="cut",
            transition_out="cut",
        )
        spec = segment_to_generation_spec(segment, prepared_digest, capabilities)
        validation = adapter.validate(spec)
        route_errors.extend(
            GenerationReadinessIssue(
                code=item.code,
                scope="route",
                severity="error",
                message=item.message,
                segment_id=segment.segment_id,
                source_shot_id=segment.parent_shot_ids[0],
            )
            for item in validation.errors
        )
        if complexity.level in {"high", "very_high"}:
            warnings.append(
                GenerationReadinessIssue(
                    code="complex_segment_manual_canary_required",
                    scope="quality",
                    severity="warning",
                    message=(
                        f"{segment.segment_id} is {complexity.level} complexity; "
                        "run it as an isolated Canary before full-episode execution."
                    ),
                    segment_id=segment.segment_id,
                    source_shot_id=segment.parent_shot_ids[0],
                )
            )
        if len(source_segment.dialogue) >= 3:
            warnings.append(
                GenerationReadinessIssue(
                    code="dialogue_dense_segment",
                    scope="quality",
                    severity="warning",
                    message=(
                        f"{segment.segment_id} contains {len(source_segment.dialogue)} dialogue "
                        "cues in 10 seconds; review TTS timing and lip-sync strategy."
                    ),
                    segment_id=segment.segment_id,
                )
            )
        segments.append(segment)

    if adapter.descriptor().execution_mode == "manual":
        warnings.append(
            GenerationReadinessIssue(
                code="manual_operator_route",
                scope="route",
                severity="warning",
                message=(
                    "Seedance official-platform execution requires an operator and the "
                    "returned MP4 must be imported as an approved SegmentArtifact."
                ),
            )
        )
    contracts, requirements = _continuity_and_requirements(
        source_plan,
        catalog,
        episode,
        segments,
        route_id=route_id,
    )
    cost_plan = _cost_plan(
        segments,
        prepared_digest=prepared_digest,
        route_id=route_id,
        registry=registry,
    )
    return GenerationPlanEpisode.build_with_digest(
        generation_plan_episode_id=_stable_id(
            "series_generation_plan",
            source_plan.project_id,
            episode_id,
            route_id,
            SERIES_IMPORT_COMPILER_VERSION,
        ),
        source_episode_id=prepared.episode_id,
        source_prepared_episode_digest=prepared_digest,
        policy_digest=canonical_digest(
            SERIES_IMPORT_COMPILER_VERSION,
            source_plan.provider_policy.model_dump(mode="json"),
            "preserve-15-source-segments",
        ),
        provider_profile_id=f"series-plan-{_route_alias(route_id)}-v1",
        provider_route_id=route_id,
        timeline_fps=fps,
        target_frame_count=episode.editorial_frame_count,
        target_duration_seconds=Decimal(episode.editorial_duration_seconds),
        segments=segments,
        continuity_contracts=contracts,
        reference_asset_requirements=requirements,
        render_graph=_render_graph(segments, route_id),
        cost_plan=cost_plan,
        readiness_report=GenerationReadinessReport(
            planning_ready=True,
            execution_route_ready=not route_errors,
            media_quality_validated=False,
            external_api_calls=0,
            errors=route_errors,
            warnings=warnings,
        ),
    )


def build_episode_asset_bundle(
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
) -> ApprovedAssetBundle:
    return build_pending_asset_bundle(prepared, plan)


def _episode(plan: SeriesGenerationPlan, episode_id: str) -> SeriesEpisode:
    result = next((item for item in plan.episodes if item.episode_id == episode_id), None)
    if result is None:
        raise SeriesPlanError(f"unknown episode: {episode_id}")
    return result


def _series_source_digest(
    plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
) -> str:
    return canonical_digest(
        plan.model_dump(mode="json", exclude_none=True),
        catalog.model_dump(mode="json", exclude_none=True),
    )


def _dialogue_drafts(segment: SeriesSegment, duration: float) -> list[DialogueDraft]:
    allocations = _dialogue_allocations(segment.dialogue, duration)
    return [
        DialogueDraft(
            cue_id=f"cue_{segment.segment_id}_{index:02d}",
            speaker_character_id=item.speaker,
            text=item.text,
            start_seconds=start,
            end_seconds=end,
            emotion=segment.emotion_end,
            delivery=f"mode:{item.mode}",
        )
        for index, (item, start, end) in enumerate(allocations, start=1)
    ]


def _dialogue_slices(segment: SeriesSegment, fps: int) -> list[DialogueSlice]:
    duration = segment.editorial_frame_count / fps
    allocations = _dialogue_allocations(segment.dialogue, duration)
    return [
        DialogueSlice(
            dialogue_slice_id=f"slice_{segment.segment_id}_{index:02d}",
            source_dialogue_id=f"cue_{segment.segment_id}_{index:02d}",
            speaker_character_id=item.speaker,
            text=item.text,
            start_frame=round(start * fps),
            end_frame=min(segment.editorial_frame_count, round(end * fps)),
            lip_sync_required=(
                item.mode not in _NON_LIP_SYNC_MODES
                and item.speaker in segment.visual_character_ids
            ),
            can_continue_over_reaction_shot=True,
        )
        for index, (item, start, end) in enumerate(allocations, start=1)
    ]


def _dialogue_allocations(
    dialogue: list[SeriesDialogue],
    duration: float,
) -> list[tuple[SeriesDialogue, float, float]]:
    if not dialogue:
        return []
    start_margin = min(0.5, duration * 0.08)
    end_margin = min(0.5, duration * 0.08)
    available = max(0.5, duration - start_margin - end_margin)
    weights = [max(1.0, len(item.text.replace("…", "")) / 7.0) for item in dialogue]
    total = sum(weights)
    cursor = start_margin
    result: list[tuple[SeriesDialogue, float, float]] = []
    for index, (item, weight) in enumerate(zip(dialogue, weights)):
        share = available * weight / total
        end = duration - end_margin if index == len(dialogue) - 1 else cursor + share
        result.append((item, round(cursor, 3), round(max(cursor + 0.05, end), 3)))
        cursor = end
    return result


def _dialogue_density_warning(segment: SeriesSegment, duration: float) -> bool:
    estimated = sum(max(0.6, len(item.text) / 5.5) for item in segment.dialogue)
    return estimated > duration - 1.0


def _prepared_visual_description(
    plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
    segment: SeriesSegment,
) -> str:
    names = catalog.by_id
    locations = "、".join(names[item].name for item in segment.location_ids)
    characters = "、".join(names[item].name for item in segment.visual_character_ids)
    props = "、".join(names[item].name for item in segment.prop_ids) or "なし"
    end_state = f" 終了状態: {segment.end_state}。" if segment.end_state else ""
    return (
        f"{plan.production.visual_style}。場所: {locations}。人物: {characters or '人物なし'}。"
        f"小道具: {props}。{segment.central_action}。感情: {segment.emotion_start}から"
        f"{segment.emotion_end}へ。{end_state}"
    )


def _camera_for(segment: SeriesSegment) -> CameraDraft:
    tags = set(segment.risk_tags)
    if "small_object_count" in tags or "hand_prop_interaction" in tags:
        return CameraDraft(
            shot_size="close_up",
            angle="eye_level",
            movement="static",
            speed="slow",
        )
    if "running_child" in tags:
        return CameraDraft(
            shot_size="full",
            angle="eye_level",
            movement="follow",
            speed="normal",
        )
    if len(segment.visual_character_ids) >= 3:
        return CameraDraft(
            shot_size="medium",
            angle="eye_level",
            movement="static",
            speed="slow",
        )
    return CameraDraft(
        shot_size="medium_close_up",
        angle="eye_level",
        movement="push_in",
        speed="slow",
    )


def _sound_effects(segment: SeriesSegment) -> list[str]:
    tags = set(segment.risk_tags)
    result: list[str] = []
    if "small_object_count" in tags:
        result.append("小さな固形絵具が木へ触れる乾いた音")
    if "door_character_reveal" in tags:
        result.append("木製の戸が静かに開く音")
    if "scissors_grape_interaction" in tags:
        result.append("葡萄の軸を鋏で一度切る小さな音")
    return result


def _prop_state_suffix(
    plan: SeriesGenerationPlan,
    asset: AssetCatalogEntry,
    episode_id: str,
) -> str:
    contract = plan.continuity_contract.prop_state_tracking.get(asset.asset_id)
    parts: list[str] = []
    if contract is not None:
        parts.extend(
            f"{key}: {value}"
            for key, value in contract.states.items()
            if key.startswith(episode_id) or key in {episode_id, "final"}
        )
    if episode_id in asset.instance_rules:
        parts.append(asset.instance_rules[episode_id])
    return (" Continuity: " + " | ".join(parts)) if parts else ""


def _continuity_group_id(project_id: str, location_id: str) -> str:
    return _stable_id("continuity", project_id, location_id)


def _reference_ids(
    group_id: str,
    segment: SeriesSegment,
    *,
    requires_first_frame: bool,
) -> list[str]:
    suffix = group_id[-10:]
    values = [
        *(f"ref_char_{item}_{suffix}" for item in segment.visual_character_ids),
        *(f"ref_loc_{item}_{suffix}" for item in segment.location_ids),
        *(f"ref_prop_{item}_{suffix}" for item in segment.prop_ids),
    ]
    if requires_first_frame:
        values.append(f"ref_first_{segment.segment_id}")
    return values


def _active_speaker(segment: SeriesSegment) -> str | None:
    visible = set(segment.visual_character_ids)
    speakers = {
        item.speaker
        for item in segment.dialogue
        if item.mode not in _NON_LIP_SYNC_MODES and item.speaker in visible
    }
    return next(iter(speakers)) if len(speakers) == 1 else None


def _complexity(segment: SeriesSegment) -> SegmentComplexity:
    characters = len(segment.visual_character_ids)
    dialogue = len(segment.dialogue)
    character_score = 0 if characters <= 1 else 2 if characters == 2 else 4
    dialogue_score = 0 if dialogue == 0 else 2 if dialogue == 1 else 3 if dialogue == 2 else 4
    action_score = 2 if any(
        item in set(segment.risk_tags)
        for item in {"hand_prop_interaction", "running_child", "scissors_grape_interaction"}
    ) else 1
    camera_score = 1 if _camera_for(segment).movement != "static" else 0
    continuity_score = 2 if len(segment.location_ids) > 1 or characters >= 3 else 1 if characters == 2 else 0
    object_score = 2 if segment.prop_ids else 0
    score = character_score + dialogue_score + action_score + camera_score + continuity_score + object_score
    level = "low" if score <= 3 else "medium" if score <= 7 else "high" if score <= 11 else "very_high"
    return SegmentComplexity(
        score=score,
        level=level,
        character_complexity=character_score,
        action_complexity=action_score,
        dialogue_complexity=dialogue_score,
        camera_complexity=camera_score,
        continuity_complexity=continuity_score,
        object_interaction_complexity=object_score,
        reasons=[
            f"{characters} visual character(s)",
            f"{dialogue} dialogue cue(s)",
            f"{len(segment.location_ids)} location reference(s)",
            f"{len(segment.prop_ids)} prop reference(s)",
            *segment.risk_tags,
        ],
    )


def _prompt_bundle(
    plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
    segment: SeriesSegment,
) -> PromptBundle:
    assets = catalog.by_id
    visual_assets = [
        assets[item].name
        for item in [
            *segment.visual_character_ids,
            *segment.location_ids,
            *segment.prop_ids,
        ]
    ]
    dialogue_prompt = " | ".join(
        f"{assets[item.speaker].name}({item.mode}): {item.text}"
        for item in segment.dialogue
    ) or None
    end_state = f" 最後は{segment.end_state}。" if segment.end_state else ""
    return PromptBundle(
        narrative_summary=f"{segment.title}: {segment.central_action}",
        visual_prompt=(
            f"{catalog.visual_style} 参照素材を厳密に維持: {', '.join(visual_assets)}。"
            f"{segment.central_action}。{end_state}"
        ),
        motion_prompt=(
            f"{segment.emotion_start}から{segment.emotion_end}へ変化する。"
            f"小道具と人物の位置・個数を維持する。"
        ),
        camera_prompt=(
            f"{_camera_for(segment).shot_size}, {_camera_for(segment).angle}, "
            f"{_camera_for(segment).movement}, {_camera_for(segment).speed}"
        ),
        timed_shot_prompt=(
            f"0-{segment.requested_duration_seconds}秒: {segment.central_action}。"
            f"感情は{segment.emotion_start}から{segment.emotion_end}へ。{end_state}"
        ),
        dialogue_prompt=dialogue_prompt,
        audio_prompt=(
            "日本語の台詞、控えめな環境音。字幕・話数・文字は後処理。"
            if segment.dialogue
            else "控えめな環境音。字幕・話数・文字は後処理。"
        ),
        negative_constraints=[
            catalog.by_id[item].negative_prompt
            for item in [
                *segment.visual_character_ids,
                *segment.location_ids,
                *segment.prop_ids,
            ]
        ]
        + [
            "字幕、話数、ロゴ、読める文字を映像へ描かない",
            "人物の顔、年齢、髪型、衣装、体格を変えない",
            "小道具の個数、所有者、位置、状態を勝手に変えない",
            *[f"risk:{item}" for item in segment.risk_tags],
        ],
    )


def _continuity_and_requirements(
    plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
    episode: SeriesEpisode,
    segments: list[GenerationSegment],
    *,
    route_id: str,
) -> tuple[list[ContinuityContract], list[ReferenceAssetRequirement]]:
    by_id = catalog.by_id
    source_by_id = {item.segment_id: item for item in episode.segments}
    grouped: dict[str, list[GenerationSegment]] = {}
    for segment in segments:
        grouped.setdefault(segment.continuity_group_id, []).append(segment)
    contracts: list[ContinuityContract] = []
    requirements: dict[str, dict[str, object]] = {}

    for group_id, group_segments in sorted(grouped.items()):
        primary_location = group_segments[0].location_id
        character_ids = sorted({item for segment in group_segments for item in segment.character_ids})
        prop_ids = sorted({item for segment in group_segments for item in segment.prop_ids})
        refs = sorted({item for segment in group_segments for item in segment.reference_asset_ids})
        contracts.append(
            ContinuityContract(
                continuity_group_id=group_id,
                character_appearance_locks={
                    item: by_id[item].prompt for item in character_ids
                },
                location_id=primary_location,
                location_lock=by_id[primary_location].prompt,
                prop_state_locks={
                    item: (
                        by_id[item].prompt
                        + _prop_state_suffix(plan, by_id[item], episode.episode_id)
                    )
                    for item in prop_ids
                },
                lighting=catalog.visual_style,
                time_of_day="秋、source segmentに従う",
                reference_asset_ids=refs,
            )
        )
        for segment in group_segments:
            source = source_by_id[segment.segment_id]
            suffix = group_id[-10:]
            subjects = {
                **{f"ref_char_{item}_{suffix}": ("character", item) for item in source.visual_character_ids},
                **{f"ref_loc_{item}_{suffix}": ("location", item) for item in source.location_ids},
                **{f"ref_prop_{item}_{suffix}": ("prop", item) for item in source.prop_ids},
            }
            if route_id == "wan/i2v":
                subjects[f"ref_first_{segment.segment_id}"] = ("first_frame", segment.segment_id)
            for asset_id, (role, subject_id) in subjects.items():
                record = requirements.setdefault(
                    asset_id,
                    {
                        "asset_id": asset_id,
                        "role": role,
                        "subject_id": subject_id,
                        "continuity_group_id": group_id,
                        "required_for_segment_ids": [],
                        "generation_status": "missing",
                    },
                )
                record["required_for_segment_ids"].append(segment.segment_id)

    return contracts, [
        ReferenceAssetRequirement.model_validate(
            {
                **item,
                "required_for_segment_ids": sorted(set(item["required_for_segment_ids"])),
            }
        )
        for _, item in sorted(requirements.items())
    ]


def _render_graph(
    segments: list[GenerationSegment],
    route_id: str,
) -> GenerationRenderGraph:
    nodes: list[GenerationTaskNode] = []
    final_tasks: list[str] = []
    for segment in segments:
        refs = f"task_refs_{segment.segment_id}"
        nodes.append(
            GenerationTaskNode(
                task_id=refs,
                segment_id=segment.segment_id,
                task_type="prepare_references",
            )
        )
        dependencies = [refs]
        if route_id == "wan/i2v":
            first = f"task_first_{segment.segment_id}"
            nodes.append(
                GenerationTaskNode(
                    task_id=first,
                    segment_id=segment.segment_id,
                    task_type="generate_first_frame",
                    depends_on=[refs],
                    external_api_required=True,
                    provider_route_id="wan/image",
                )
            )
            dependencies = [first]
        video = f"task_video_{segment.segment_id}"
        nodes.append(
            GenerationTaskNode(
                task_id=video,
                segment_id=segment.segment_id,
                task_type=(
                    "generate_native_av"
                    if segment.audio_strategy == "native_av"
                    else "generate_video"
                ),
                depends_on=dependencies,
                external_api_required=True,
                provider_route_id=route_id,
            )
        )
        subtitle = f"task_subtitle_{segment.segment_id}"
        nodes.append(
            GenerationTaskNode(
                task_id=subtitle,
                segment_id=segment.segment_id,
                task_type="generate_subtitles",
            )
        )
        mux_dependencies = [video, subtitle]
        if segment.audio_strategy == "external_audio_post":
            tts = f"task_tts_{segment.segment_id}"
            nodes.append(
                GenerationTaskNode(
                    task_id=tts,
                    segment_id=segment.segment_id,
                    task_type="generate_tts",
                    external_api_required=True,
                    provider_route_id="existing/qwen3-tts",
                )
            )
            mux_dependencies.append(tts)
        mux = f"task_mux_{segment.segment_id}"
        trim = f"task_trim_{segment.segment_id}"
        validate = f"task_validate_{segment.segment_id}"
        nodes.extend(
            [
                GenerationTaskNode(
                    task_id=mux,
                    segment_id=segment.segment_id,
                    task_type="mux_segment",
                    depends_on=mux_dependencies,
                ),
                GenerationTaskNode(
                    task_id=trim,
                    segment_id=segment.segment_id,
                    task_type="trim_segment",
                    depends_on=[mux],
                ),
                GenerationTaskNode(
                    task_id=validate,
                    segment_id=segment.segment_id,
                    task_type="validate_segment",
                    depends_on=[trim],
                ),
            ]
        )
        final_tasks.append(validate)
    nodes.extend(
        [
            GenerationTaskNode(
                task_id="task_episode_concat",
                task_type="concat_episode",
                depends_on=final_tasks,
            ),
            GenerationTaskNode(
                task_id="task_episode_validate",
                task_type="validate_episode",
                depends_on=["task_episode_concat"],
            ),
        ]
    )
    return GenerationRenderGraph(nodes=nodes)


def _cost_plan(
    segments: list[GenerationSegment],
    *,
    prepared_digest: str,
    route_id: str,
    registry: ProviderRegistry,
) -> GenerationCostPlan:
    adapter = registry.require(route_id)
    capabilities = adapter.capabilities()
    items: list[GenerationCostItem] = []
    totals: dict[str, Decimal] = {}
    unknown: list[str] = []
    snapshots: set[str] = set()
    reference_image_calls = 0
    tts_calls = 0
    native_audio_calls = 0

    for segment in segments:
        spec = segment_to_generation_spec(segment, prepared_digest, capabilities)
        estimate = adapter.estimate_cost(spec)
        amount = estimate.native_cost.amount if estimate.native_cost else None
        currency = estimate.native_cost.currency if estimate.native_cost else None
        items.append(
            GenerationCostItem(
                segment_id=segment.segment_id,
                component="video",
                calls=1,
                amount=amount,
                currency=currency,
                confidence=estimate.confidence,
                price_snapshot_id=estimate.price_snapshot_id,
            )
        )
        if amount is None or currency is None:
            unknown.append(f"video:{segment.segment_id}")
        else:
            totals[currency] = totals.get(currency, Decimal("0")) + amount
        if estimate.price_snapshot_id:
            snapshots.add(estimate.price_snapshot_id)

        if route_id == "wan/i2v":
            reference_image_calls += 1
            image_adapter = registry.get("wan/image")
            image_estimate = image_adapter.estimate_cost(
                ShotGenerationSpec(
                    source_digest=prepared_digest,
                    shot_id=segment.segment_id,
                    task_id=f"task_first_{segment.segment_id}",
                    modality="image",
                    duration_seconds=1,
                    aspect_ratio="9:16",
                    resolution="720P",
                    prompt=segment.prompt_bundle.visual_prompt,
                    audio_strategy="silent",
                    required_capabilities=ProviderCapabilitiesRequired(modality="image"),
                )
            ) if image_adapter is not None else None
            image_cost = image_estimate.native_cost if image_estimate else None
            items.append(
                GenerationCostItem(
                    segment_id=segment.segment_id,
                    component="reference_image",
                    calls=1,
                    amount=image_cost.amount if image_cost else None,
                    currency=image_cost.currency if image_cost else None,
                    confidence=image_estimate.confidence if image_estimate else "unknown",
                    price_snapshot_id=(
                        image_estimate.price_snapshot_id if image_estimate else None
                    ),
                )
            )
            if image_cost is None:
                unknown.append(f"reference_image:{segment.segment_id}")
            else:
                totals[image_cost.currency] = totals.get(
                    image_cost.currency, Decimal("0")
                ) + image_cost.amount
            if image_estimate and image_estimate.price_snapshot_id:
                snapshots.add(image_estimate.price_snapshot_id)

        if segment.audio_strategy == "external_audio_post":
            calls = len(segment.dialogue_slices)
            tts_calls += calls
            items.append(
                GenerationCostItem(
                    segment_id=segment.segment_id,
                    component="tts",
                    calls=calls,
                    confidence="unknown",
                )
            )
            if calls:
                unknown.append(f"tts:{segment.segment_id}")
        elif segment.audio_strategy == "native_av":
            native_audio_calls += 1
            items.append(
                GenerationCostItem(
                    segment_id=segment.segment_id,
                    component="native_audio",
                    calls=0,
                    amount=Decimal("0"),
                    currency=currency,
                    confidence="exact" if currency else "unknown",
                    price_snapshot_id=estimate.price_snapshot_id,
                )
            )

    expected = reference_image_calls + len(segments) + tts_calls
    return GenerationCostPlan(
        reference_image_calls=reference_image_calls,
        video_calls=len(segments),
        tts_calls=tts_calls,
        native_audio_calls=native_audio_calls,
        expected_calls=expected,
        hard_maximum_calls=expected,
        items=items,
        totals_by_currency=totals,
        unknown_cost_components=sorted(set(unknown)),
        pricing_snapshot_dates=sorted(snapshots),
    )


def _route_alias(route_id: str) -> str:
    return {
        "minimax/h3-reference-av": "h3",
        "wan/i2v": "wan",
        "seedance/platform": "seedance",
    }[route_id]


def _stable_id(prefix: str, *parts: str) -> str:
    material = "|".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(material).hexdigest()[:16]}"
