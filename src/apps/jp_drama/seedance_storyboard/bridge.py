"""Connect imported Seedance storyboards to existing production contracts.

The upstream Markdown remains the creative source of truth.  This bridge adds
only deterministic production identity, provider routing, asset slots, and
cost/readiness metadata.  It never submits an image, video, audio, or LLM job.
"""

from __future__ import annotations

import hashlib
import math
import re
from decimal import Decimal

from ..assets.bundle import build_pending_asset_bundle, prepared_content_digest
from ..assets.models import ApprovedAssetBundle
from ..domain import Currency, RenderStrategy
from ..generation.compiler import segment_to_generation_spec
from ..generation.models import (
    ContinuityContract,
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
    SeedanceStoryboardEpisode,
    SeedanceStoryboardPackage,
    StoryboardAsset,
    TimelineBeat,
)


STORYBOARD_BRIDGE_VERSION = "1.0.0"
SUPPORTED_ROUTES = {
    "h3": "minimax/h3-reference-av",
    "wan": "wan/i2v",
    "seedance": "seedance/platform",
}
_REFERENCE_TOKEN = re.compile(r"@(?P<slot>图片[0-9]+)")


class SeedanceStoryboardBridgeError(RuntimeError):
    """The storyboard cannot be mapped without inventing production data."""


def build_storyboard_prepared_episode(
    package: SeedanceStoryboardPackage,
    episode_id: str,
    *,
    fps: int = 30,
) -> PreparedEpisode:
    """Build the deterministic PreparedEpisode identity used by existing gates."""
    episode = _episode(package, episode_id)
    episode_assets = _episode_assets(package, episode)
    primary_scene = next(
        (item for item in episode_assets if item.kind == "scene"),
        None,
    )
    if primary_scene is None:
        raise SeedanceStoryboardBridgeError(
            f"{episode_id} requires at least one scene asset"
        )

    key = package.content_digest.split(":", 1)[1][:12]
    prepared_episode_id = f"seedance_{key}_{episode_id.lower()}"
    frames: list[StoryboardFrameDraft] = []
    intents: list[RenderIntent] = []
    graph_nodes: list[RenderTaskNode] = []
    budget_items: list[ShotBudgetItem] = []

    for beat in episode.timeline:
        used = _assets_for_text(package, episode, beat.text) or episode_assets
        characters = [item.asset_id for item in used if item.kind == "character"]
        props = [item.asset_id for item in used if item.kind == "prop"]
        scenes = [item.asset_id for item in used if item.kind == "scene"]
        location_id = scenes[0] if scenes else primary_scene.asset_id
        shot_id = f"{episode_id.lower()}_storyboard_{beat.order:02d}"
        intent_id = f"intent_{shot_id}"
        strategy = (
            RenderStrategy.NATIVE_AV
            if episode.sound_prompt
            else RenderStrategy.SILENT_VIDEO
        )
        generation_task_type = (
            "generate_native_av"
            if strategy is RenderStrategy.NATIVE_AV
            else "generate_video"
        )
        generation_task = f"task_{shot_id}_{generation_task_type}"
        final_task = f"task_{shot_id}_finalize"

        frames.append(
            StoryboardFrameDraft(
                frame_id=f"frame_{shot_id}",
                source_shot_id=shot_id,
                adapted_beat_id=f"adapted_{shot_id}",
                order=beat.order,
                duration_seconds=float(beat.end_seconds - beat.start_seconds),
                location_seed_id=location_id,
                character_seed_ids=characters,
                prop_seed_ids=props,
                action=beat.text,
                visual_description=_visual_prompt(episode, beat.text),
                camera=_camera_from_text(beat.text),
                dialogue_cues=[],
                audio=AudioDraft(
                    ambience=episode.sound_prompt,
                    sound_effects=[episode.sound_prompt] if episode.sound_prompt else [],
                    generated_native_audio=bool(episode.sound_prompt),
                ),
                render_intent_id=intent_id,
            )
        )
        intents.append(
            RenderIntent(
                intent_id=intent_id,
                shot_id=shot_id,
                requested_strategy=strategy,
                resolved_strategy=strategy,
                provider="seedance-storyboard",
                model="upstream-creative-contract",
                model_capabilities_required=["video", "9:16"],
                tasks=[generation_task, final_task],
                estimated_primary_cost=Decimal("0"),
                reserved_retry_cost=Decimal("0"),
                estimated_total_cost=Decimal("0"),
            )
        )
        graph_nodes.extend(
            [
                RenderTaskNode(
                    task_id=generation_task,
                    shot_id=shot_id,
                    task_type=generation_task_type,
                    external_api_required=True,
                    provider_required=True,
                ),
                RenderTaskNode(
                    task_id=final_task,
                    shot_id=shot_id,
                    task_type="finalize_shot",
                    depends_on=[generation_task],
                    external_api_required=False,
                    provider_required=False,
                ),
            ]
        )
        budget_items.append(
            ShotBudgetItem(
                shot_id=shot_id,
                strategy=strategy,
                primary_cost=Decimal("0"),
                retry_cost=Decimal("0"),
                total_cost=Decimal("0"),
            )
        )

    asset_mappings = [
        MappingEntry(source_id=item.asset_id, target_id=item.asset_id)
        for item in episode_assets
    ]
    warnings = [
        ReadinessIssue(
            code="seedance_dialogue_not_structured",
            severity="warning",
            message=(
                "Dialogue and sound remain verbatim inside the upstream prompt; "
                "speaker-specific TTS identities are not invented."
            ),
        )
    ]
    if episode.continuation_source:
        warnings.append(
            ReadinessIssue(
                code="seedance_continuation_requires_previous_video",
                severity="warning",
                message=(
                    f"{episode_id} requires approved continuation source "
                    f"@{episode.continuation_source}."
                ),
            )
        )

    return PreparedEpisode(
        source_digest=package.content_digest,
        package_id=f"package_{prepared_episode_id}",
        episode_id=prepared_episode_id,
        project_draft=ProjectDraft(
            project_id=f"project_{prepared_episode_id}",
            title=f"{package.project_title} {episode.episode_id} {episode.title}",
            description="Pinned Seedance2 Storyboard Generator creative contract",
            fps=fps,
            target_duration_seconds=float(episode.duration_seconds),
            episode_number=int(episode.episode_id[1:]),
            series_id=f"series_seedance_{key}",
        ),
        character_seeds=[
            CharacterSeed(
                seed_id=item.asset_id,
                source_character_id=item.asset_id,
                name=item.name,
                description=item.prompt,
                visual_prompt=item.prompt,
            )
            for item in episode_assets
            if item.kind == "character"
        ],
        location_seeds=[
            LocationSeed(
                seed_id=item.asset_id,
                source_location_id=item.asset_id,
                name=item.name,
                description=item.prompt,
                continuity_rules=[
                    "Keep the upstream upload-slot identity and spatial layout stable"
                ],
                visual_prompt=item.prompt,
            )
            for item in episode_assets
            if item.kind == "scene"
        ],
        prop_seeds=[
            PropSeed(
                seed_id=item.asset_id,
                source_prop_id=item.asset_id,
                name=item.name,
                story_function=item.story_function or item.name,
                visual_prompt=item.prompt,
            )
            for item in episode_assets
            if item.kind == "prop"
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
            characters=[item for item in asset_mappings if item.source_id.startswith("C")],
            locations=[item for item in asset_mappings if item.source_id.startswith("S")],
            props=[item for item in asset_mappings if item.source_id.startswith("P")],
            shots=[
                MappingEntry(
                    source_id=f"{episode_id.lower()}_storyboard_{beat.order:02d}",
                    target_id=f"frame_{episode_id.lower()}_storyboard_{beat.order:02d}",
                )
                for beat in episode.timeline
            ],
            adapted_beats=[
                MappingEntry(
                    source_id=f"beat_{beat.order:02d}",
                    target_id=f"adapted_{episode_id.lower()}_storyboard_{beat.order:02d}",
                )
                for beat in episode.timeline
            ],
            mapping_coverage=1.0,
        ),
        readiness_report=ReadinessReport(
            package_id=f"package_{prepared_episode_id}",
            episode_id=prepared_episode_id,
            duration_seconds=float(episode.duration_seconds),
            shot_count=len(frames),
            character_count=sum(item.kind == "character" for item in episode_assets),
            location_count=sum(item.kind == "scene" for item in episode_assets),
            prop_count=sum(item.kind == "prop" for item in episode_assets),
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


def compile_storyboard_generation_plan(
    package: SeedanceStoryboardPackage,
    prepared: PreparedEpisode,
    episode_id: str,
    *,
    route_id: str,
    registry: ProviderRegistry,
) -> GenerationPlanEpisode:
    """Compile one H3, Wan, or manual Seedance GenerationPlanEpisode."""
    if route_id not in set(SUPPORTED_ROUTES.values()):
        raise SeedanceStoryboardBridgeError(f"unsupported route: {route_id}")
    episode = _episode(package, episode_id)
    if prepared.source_digest != package.content_digest:
        raise SeedanceStoryboardBridgeError("PreparedEpisode belongs to another package")
    if prepared.project_draft.episode_number != int(episode_id[1:]):
        raise SeedanceStoryboardBridgeError("PreparedEpisode episode mismatch")

    adapter = registry.require(route_id)
    capabilities = adapter.capabilities()
    prepared_digest = prepared_content_digest(prepared)
    fps = prepared.project_draft.fps
    episode_assets = _episode_assets(package, episode)
    primary_scene = next(
        (item for item in episode_assets if item.kind == "scene"),
        None,
    )
    if primary_scene is None:
        raise SeedanceStoryboardBridgeError(f"{episode_id} has no scene asset")

    # H3 keeps the upstream timed multi-shot prompt. Wan is single-shot and is
    # split at each explicit upstream timeline boundary. The manual Seedance
    # route remains one operator operation; its raw timed prompt is authoritative.
    groups = (
        [[beat] for beat in episode.timeline]
        if route_id == "wan/i2v"
        else [list(episode.timeline)]
    )
    segments: list[GenerationSegment] = []
    for order, group in enumerate(groups, start=1):
        start = group[0].start_seconds
        end = group[-1].end_seconds
        editorial_frames = round(float(end - start) * fps)
        requested_seconds = max(
            math.ceil(capabilities.min_duration_seconds),
            math.ceil(editorial_frames / fps),
        )
        if requested_seconds > math.floor(capabilities.max_duration_seconds):
            raise SeedanceStoryboardBridgeError(
                f"{episode_id} group {order} exceeds {route_id} duration limits"
            )
        segment_id = _stable_id(
            "storyboard_segment",
            package.content_digest,
            episode_id,
            route_id,
            str(order),
            str(start),
            str(end),
        )
        used_assets = _assets_for_beats(package, episode, group) or episode_assets
        characters = [item.asset_id for item in used_assets if item.kind == "character"]
        props = [item.asset_id for item in used_assets if item.kind == "prop"]
        scenes = [item.asset_id for item in used_assets if item.kind == "scene"]
        location_id = scenes[0] if scenes else primary_scene.asset_id
        continuity_group_id = _stable_id(
            "storyboard_continuity",
            package.content_digest,
            episode_id,
            location_id,
        )
        shots = _editorial_shots(
            package,
            episode,
            group,
            segment_id=segment_id,
            group_start=start,
            fps=fps,
            collapse_to_operator_shot=(route_id == "seedance/platform"),
        )
        requested_frames = requested_seconds * fps
        handle_frames = requested_frames - editorial_frames
        used_start = handle_frames // 2
        references = [_reference_id(item) for item in used_assets]
        if route_id == "wan/i2v":
            references.append(f"ref_first_{segment_id}")
        prompt = (
            episode.raw_prompt
            if len(group) == len(episode.timeline)
            else _wan_prompt(episode, group[0])
        )
        has_native_audio = (
            route_id in {"minimax/h3-reference-av", "seedance/platform"}
            and bool(episode.sound_prompt)
        )
        segments.append(
            GenerationSegment(
                segment_id=segment_id,
                order=order,
                parent_shot_ids=[
                    f"{episode_id.lower()}_storyboard_{beat.order:02d}"
                    for beat in group
                ],
                continuity_group_id=continuity_group_id,
                provider_route_id=route_id,
                timeline_fps=fps,
                editorial_start_frame=round(float(start) * fps),
                editorial_end_frame=round(float(end) * fps),
                editorial_frame_count=editorial_frames,
                editorial_duration_seconds=Decimal(editorial_frames) / Decimal(fps),
                requested_duration_seconds=requested_seconds,
                used_start_frame=used_start,
                used_end_frame=used_start + editorial_frames,
                complexity=_complexity(shots, characters, props),
                editorial_shots=shots,
                character_ids=characters,
                location_id=location_id,
                prop_ids=props,
                reference_asset_ids=references,
                prompt_bundle=PromptBundle(
                    narrative_summary=(
                        f"{package.project_title} {episode.episode_id} {episode.title}"
                    ),
                    visual_prompt=_visual_prompt(episode, prompt),
                    motion_prompt="\n".join(beat.text for beat in group),
                    camera_prompt="Follow the exact upstream timed camera directions.",
                    timed_shot_prompt=prompt,
                    audio_prompt=episode.sound_prompt,
                    negative_constraints=[
                        "Keep uploaded character, scene, and prop identities unchanged.",
                        "Do not render subtitles, logos, watermarks, or modern text.",
                        "Avoid duplicate people, malformed hands, missing props, and black frames.",
                    ],
                ),
                audio_strategy="native_av" if has_native_audio else "silent",
                transition_in=(
                    "continuation"
                    if order > 1 or (order == 1 and episode.continuation_source)
                    else "cut"
                ),
                transition_out=(
                    "continuation"
                    if order < len(groups) or _next_episode_continues(package, episode_id)
                    else "cut"
                ),
            )
        )

    contracts, requirements = _continuity_and_requirements(
        package,
        episode,
        segments,
        route_id=route_id,
    )
    errors: list[GenerationReadinessIssue] = []
    warnings = _planning_warnings(episode, route_id, adapter.descriptor().execution_mode)
    cost_items: list[GenerationCostItem] = []
    totals: dict[str, Decimal] = {}
    unknown: list[str] = []
    snapshots: set[str] = set()

    reference_image_calls = 0
    if route_id == "wan/i2v":
        image_adapter = registry.get("wan/image")
        for segment in segments:
            reference_image_calls += 1
            estimate = None
            if image_adapter is not None:
                estimate = image_adapter.estimate_cost(
                    ShotGenerationSpec(
                        source_digest=prepared_digest,
                        shot_id=segment.segment_id,
                        task_id=f"task_first_frame_{segment.segment_id}",
                        modality="image",
                        duration_seconds=1,
                        prompt=segment.prompt_bundle.visual_prompt,
                        audio_strategy="silent",
                        required_capabilities=ProviderCapabilitiesRequired(modality="image"),
                    )
                )
            item = _cost_item_from_estimate(
                segment.segment_id,
                "reference_image",
                1,
                estimate,
                totals,
                unknown,
                snapshots,
            )
            cost_items.append(item)

    for segment in segments:
        spec = segment_to_generation_spec(segment, prepared_digest, capabilities)
        validation = adapter.validate(spec)
        errors.extend(
            GenerationReadinessIssue(
                code=issue.code,
                scope="route",
                severity="error",
                message=issue.message,
                segment_id=segment.segment_id,
                source_shot_id=segment.parent_shot_ids[0],
            )
            for issue in validation.errors
        )
        estimate = adapter.estimate_cost(spec)
        cost_items.append(
            _cost_item_from_estimate(
                segment.segment_id,
                "video",
                1,
                estimate,
                totals,
                unknown,
                snapshots,
            )
        )
        if segment.audio_strategy == "native_av":
            cost_items.append(
                GenerationCostItem(
                    segment_id=segment.segment_id,
                    component="native_audio",
                    calls=0,
                    amount=Decimal("0"),
                    currency=(
                        estimate.native_cost.currency
                        if estimate.native_cost is not None
                        else None
                    ),
                    confidence="exact",
                    price_snapshot_id=estimate.price_snapshot_id,
                )
            )

    policy_digest = _digest(
        f"{STORYBOARD_BRIDGE_VERSION}|{route_id}|preserve-upstream-timeline"
    )
    expected_calls = reference_image_calls + len(segments)
    return GenerationPlanEpisode.build_with_digest(
        generation_plan_episode_id=_stable_id(
            "storyboard_generation_plan",
            package.content_digest,
            episode_id,
            route_id,
            STORYBOARD_BRIDGE_VERSION,
        ),
        source_episode_id=prepared.episode_id,
        source_prepared_episode_digest=prepared_digest,
        policy_digest=policy_digest,
        provider_profile_id=f"seedance-storyboard-{_route_alias(route_id)}-v1",
        provider_route_id=route_id,
        timeline_fps=fps,
        target_frame_count=round(float(episode.duration_seconds) * fps),
        target_duration_seconds=episode.duration_seconds,
        segments=segments,
        continuity_contracts=contracts,
        reference_asset_requirements=requirements,
        render_graph=_render_graph(segments, route_id),
        cost_plan=GenerationCostPlan(
            reference_image_calls=reference_image_calls,
            video_calls=len(segments),
            tts_calls=0,
            native_audio_calls=sum(
                segment.audio_strategy == "native_av" for segment in segments
            ),
            expected_calls=expected_calls,
            hard_maximum_calls=expected_calls,
            items=cost_items,
            totals_by_currency=totals,
            unknown_cost_components=sorted(set(unknown)),
            pricing_snapshot_dates=sorted(snapshots),
        ),
        readiness_report=GenerationReadinessReport(
            planning_ready=True,
            execution_route_ready=not errors,
            media_quality_validated=False,
            external_api_calls=0,
            errors=errors,
            warnings=warnings,
        ),
    )


def build_storyboard_asset_bundle(
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
) -> ApprovedAssetBundle:
    """Build the existing hash-bound pending Asset Bundle."""
    return build_pending_asset_bundle(prepared, plan)


def _episode(
    package: SeedanceStoryboardPackage,
    episode_id: str,
) -> SeedanceStoryboardEpisode:
    episode = next(
        (item for item in package.episodes if item.episode_id == episode_id),
        None,
    )
    if episode is None:
        raise SeedanceStoryboardBridgeError(f"unknown episode: {episode_id}")
    return episode


def _episode_assets(
    package: SeedanceStoryboardPackage,
    episode: SeedanceStoryboardEpisode,
) -> list[StoryboardAsset]:
    by_id = {item.asset_id: item for item in package.assets}
    result: list[StoryboardAsset] = []
    for slot in episode.upload_slots:
        asset = by_id.get(slot.asset_id)
        if asset is None:
            raise SeedanceStoryboardBridgeError(
                f"{episode.episode_id} references missing asset {slot.asset_id}"
            )
        if asset not in result:
            result.append(asset)
    return result


def _assets_for_text(
    package: SeedanceStoryboardPackage,
    episode: SeedanceStoryboardEpisode,
    text: str,
) -> list[StoryboardAsset]:
    slot_assets = {slot.slot_name: slot.asset_id for slot in episode.upload_slots}
    by_id = {item.asset_id: item for item in package.assets}
    result: list[StoryboardAsset] = []
    for slot_name in _REFERENCE_TOKEN.findall(text):
        asset_id = slot_assets.get(slot_name)
        asset = by_id.get(asset_id or "")
        if asset is not None and asset not in result:
            result.append(asset)
    return result


def _assets_for_beats(
    package: SeedanceStoryboardPackage,
    episode: SeedanceStoryboardEpisode,
    beats: list[TimelineBeat],
) -> list[StoryboardAsset]:
    result: list[StoryboardAsset] = []
    for beat in beats:
        for asset in _assets_for_text(package, episode, beat.text):
            if asset not in result:
                result.append(asset)
    return result


def _editorial_shots(
    package: SeedanceStoryboardPackage,
    episode: SeedanceStoryboardEpisode,
    beats: list[TimelineBeat],
    *,
    segment_id: str,
    group_start: Decimal,
    fps: int,
    collapse_to_operator_shot: bool,
) -> list[EditorialShot]:
    if collapse_to_operator_shot:
        return [
            EditorialShot(
                editorial_shot_id=f"{segment_id}_operator_shot",
                segment_id=segment_id,
                order_within_segment=1,
                start_frame=0,
                end_frame=round(float(beats[-1].end_seconds - group_start) * fps),
                framing="upstream_timed_prompt",
                camera_movement="upstream_timed_prompt",
                visual_action=episode.raw_prompt,
                character_ids=[
                    item.asset_id
                    for item in _episode_assets(package, episode)
                    if item.kind == "character"
                ],
                source_beat_ids=[
                    f"{episode.episode_id}_beat_{beat.order:02d}" for beat in beats
                ],
                source_shot_ids=[
                    f"{episode.episode_id.lower()}_storyboard_{beat.order:02d}"
                    for beat in beats
                ],
            )
        ]

    shots: list[EditorialShot] = []
    fallback_assets = _episode_assets(package, episode)
    for order, beat in enumerate(beats, start=1):
        assets = _assets_for_text(package, episode, beat.text) or fallback_assets
        camera = _camera_from_text(beat.text)
        shots.append(
            EditorialShot(
                editorial_shot_id=f"{segment_id}_shot_{order:02d}",
                segment_id=segment_id,
                order_within_segment=order,
                start_frame=round(float(beat.start_seconds - group_start) * fps),
                end_frame=round(float(beat.end_seconds - group_start) * fps),
                framing=camera.shot_size,
                camera_movement=camera.movement,
                visual_action=beat.text,
                character_ids=[
                    item.asset_id for item in assets if item.kind == "character"
                ],
                source_beat_ids=[f"{episode.episode_id}_beat_{beat.order:02d}"],
                source_shot_ids=[
                    f"{episode.episode_id.lower()}_storyboard_{beat.order:02d}"
                ],
            )
        )
    return shots


def _continuity_and_requirements(
    package: SeedanceStoryboardPackage,
    episode: SeedanceStoryboardEpisode,
    segments: list[GenerationSegment],
    *,
    route_id: str,
) -> tuple[list[ContinuityContract], list[ReferenceAssetRequirement]]:
    assets = {item.asset_id: item for item in package.assets}
    requirements: dict[str, ReferenceAssetRequirement] = {}
    all_refs: set[str] = set()
    all_characters: set[str] = set()
    all_props: set[str] = set()
    group_id = segments[0].continuity_group_id
    location_id = segments[0].location_id

    for segment in segments:
        all_characters.update(segment.character_ids)
        all_props.update(segment.prop_ids)
        for asset_id in [*segment.character_ids, segment.location_id, *segment.prop_ids]:
            asset = assets[asset_id]
            ref_id = _reference_id(asset)
            all_refs.add(ref_id)
            existing = requirements.get(ref_id)
            required_for = sorted(
                set((existing.required_for_segment_ids if existing else []) + [segment.segment_id])
            )
            requirements[ref_id] = ReferenceAssetRequirement(
                asset_id=ref_id,
                role={
                    "character": "character",
                    "scene": "location",
                    "prop": "prop",
                }[asset.kind],
                subject_id=asset.asset_id,
                continuity_group_id=group_id,
                required_for_segment_ids=required_for,
            )
        if route_id == "wan/i2v":
            ref_id = f"ref_first_{segment.segment_id}"
            all_refs.add(ref_id)
            requirements[ref_id] = ReferenceAssetRequirement(
                asset_id=ref_id,
                role="first_frame",
                subject_id=segment.segment_id,
                continuity_group_id=group_id,
                required_for_segment_ids=[segment.segment_id],
            )

    contract = ContinuityContract(
        continuity_group_id=group_id,
        character_appearance_locks={
            item: assets[item].prompt for item in sorted(all_characters)
        },
        location_id=location_id,
        location_lock=assets[location_id].prompt,
        prop_state_locks={item: assets[item].prompt for item in sorted(all_props)},
        reference_asset_ids=sorted(all_refs),
    )
    return [contract], sorted(requirements.values(), key=lambda item: item.asset_id)


def _planning_warnings(
    episode: SeedanceStoryboardEpisode,
    route_id: str,
    execution_mode: str,
) -> list[GenerationReadinessIssue]:
    warnings = [
        GenerationReadinessIssue(
            code="reference_assets_missing",
            scope="planning",
            severity="warning",
            message="Storyboard references are mapped to pending Asset Bundle slots.",
        ),
        GenerationReadinessIssue(
            code="seedance_dialogue_not_structured",
            scope="quality",
            severity="warning",
            message=(
                "Dialogue and sound are preserved in the upstream prompt; "
                "speaker-specific TTS requires a linked script contract."
            ),
        ),
    ]
    if execution_mode == "manual":
        warnings.append(
            GenerationReadinessIssue(
                code="manual_operator_route",
                scope="route",
                severity="warning",
                message="Seedance is exported for operator execution without browser automation.",
            )
        )
    if route_id == "wan/i2v" and episode.sound_prompt:
        warnings.append(
            GenerationReadinessIssue(
                code="wan_audio_postproduction_required",
                scope="quality",
                severity="warning",
                message="Wan receives visual-only segments; upstream sound needs later postproduction.",
            )
        )
    if episode.continuation_source:
        warnings.append(
            GenerationReadinessIssue(
                code="continuation_asset_required",
                scope="planning",
                severity="warning",
                message=f"Approved previous video @{episode.continuation_source} is required.",
            )
        )
    return warnings


def _cost_item_from_estimate(
    segment_id: str,
    component: str,
    calls: int,
    estimate: object | None,
    totals: dict[str, Decimal],
    unknown: list[str],
    snapshots: set[str],
) -> GenerationCostItem:
    native_cost = getattr(estimate, "native_cost", None)
    confidence = getattr(estimate, "confidence", "unknown")
    snapshot = getattr(estimate, "price_snapshot_id", None)
    amount = native_cost.amount if native_cost is not None else None
    currency = native_cost.currency if native_cost is not None else None
    if amount is None:
        unknown.append(f"{segment_id}:{component}")
    else:
        totals[currency] = totals.get(currency, Decimal("0")) + amount
    if snapshot:
        snapshots.add(snapshot)
    return GenerationCostItem(
        segment_id=segment_id,
        component=component,
        calls=calls,
        amount=amount,
        currency=currency,
        confidence=confidence,
        price_snapshot_id=snapshot,
    )


def _render_graph(
    segments: list[GenerationSegment],
    route_id: str,
) -> GenerationRenderGraph:
    nodes: list[GenerationTaskNode] = []
    validated: list[str] = []
    for segment in segments:
        prepare = f"prepare_references_{segment.segment_id}"
        nodes.append(
            GenerationTaskNode(
                task_id=prepare,
                segment_id=segment.segment_id,
                task_type="prepare_references",
            )
        )
        dependency = prepare
        if route_id == "wan/i2v":
            first = f"generate_first_frame_{segment.segment_id}"
            nodes.append(
                GenerationTaskNode(
                    task_id=first,
                    segment_id=segment.segment_id,
                    task_type="generate_first_frame",
                    depends_on=[prepare],
                    external_api_required=True,
                    provider_route_id="wan/image",
                )
            )
            dependency = first
        generate = f"generate_video_{segment.segment_id}"
        trim = f"trim_{segment.segment_id}"
        validate = f"validate_{segment.segment_id}"
        nodes.extend(
            [
                GenerationTaskNode(
                    task_id=generate,
                    segment_id=segment.segment_id,
                    task_type=(
                        "generate_native_av"
                        if segment.audio_strategy == "native_av"
                        else "generate_video"
                    ),
                    depends_on=[dependency],
                    external_api_required=True,
                    provider_route_id=route_id,
                ),
                GenerationTaskNode(
                    task_id=trim,
                    segment_id=segment.segment_id,
                    task_type="trim_segment",
                    depends_on=[generate],
                ),
                GenerationTaskNode(
                    task_id=validate,
                    segment_id=segment.segment_id,
                    task_type="validate_segment",
                    depends_on=[trim],
                ),
            ]
        )
        validated.append(validate)
    nodes.extend(
        [
            GenerationTaskNode(
                task_id="concat_storyboard_episode",
                task_type="concat_episode",
                depends_on=validated,
            ),
            GenerationTaskNode(
                task_id="validate_storyboard_episode",
                task_type="validate_episode",
                depends_on=["concat_storyboard_episode"],
            ),
        ]
    )
    return GenerationRenderGraph(nodes=nodes)


def _reference_id(asset: StoryboardAsset) -> str:
    return {
        "character": "ref_char_",
        "scene": "ref_loc_",
        "prop": "ref_prop_",
    }[asset.kind] + asset.asset_id


def _visual_prompt(episode: SeedanceStoryboardEpisode, text: str) -> str:
    parts = [episode.style_prompt, text]
    if episode.reference_prompt:
        parts.append(f"Reference direction: {episode.reference_prompt}")
    return "\n".join(parts)


def _wan_prompt(episode: SeedanceStoryboardEpisode, beat: TimelineBeat) -> str:
    return (
        f"{episode.style_prompt}\n"
        f"{beat.start_seconds}-{beat.end_seconds}秒: {beat.text}\n"
        "Generate one continuous visual action. Do not render subtitles or spoken text."
    )


def _camera_from_text(text: str) -> CameraDraft:
    shot_size = (
        "extreme_close_up"
        if any(token in text for token in ("极近特写", "超特写"))
        else "close_up"
        if any(token in text for token in ("特写", "近景"))
        else "long"
        if any(token in text for token in ("全景", "远景"))
        else "medium"
    )
    movement = (
        "push_in"
        if any(token in text for token in ("推近", "推进"))
        else "pull_out"
        if any(token in text for token in ("拉远", "后拉"))
        else "pan_left"
        if "左摇" in text
        else "pan_right"
        if "右摇" in text
        else "follow"
        if any(token in text for token in ("跟拍", "跟随"))
        else "static"
    )
    angle = "low" if "低角度" in text else "high" if "高角度" in text else "eye_level"
    speed = (
        "fast"
        if any(token in text for token in ("快速", "迅速"))
        else "slow"
        if "缓慢" in text
        else "normal"
    )
    return CameraDraft(
        shot_size=shot_size,
        angle=angle,
        movement=movement,
        speed=speed,
    )


def _complexity(
    shots: list[EditorialShot],
    characters: list[str],
    props: list[str],
) -> SegmentComplexity:
    character_score = 0 if len(characters) <= 1 else 2 if len(characters) == 2 else 4
    action_score = min(4, len(shots))
    camera_score = sum(shot.camera_movement != "static" for shot in shots)
    continuity_score = 1 if len(shots) > 1 else 0
    prop_score = 1 if props else 0
    score = character_score + action_score + camera_score + continuity_score + prop_score
    level = (
        "low"
        if score <= 3
        else "medium"
        if score <= 7
        else "high"
        if score <= 11
        else "very_high"
    )
    return SegmentComplexity(
        score=score,
        level=level,
        character_complexity=character_score,
        action_complexity=action_score,
        dialogue_complexity=0,
        camera_complexity=camera_score,
        continuity_complexity=continuity_score,
        object_interaction_complexity=prop_score,
        reasons=[
            f"{len(shots)} upstream timed beat(s)",
            f"{len(characters)} character reference(s)",
            f"{len(props)} prop reference(s)",
        ],
    )


def _next_episode_continues(
    package: SeedanceStoryboardPackage,
    episode_id: str,
) -> bool:
    next_id = f"E{int(episode_id[1:]) + 1:02d}"
    next_episode = next(
        (item for item in package.episodes if item.episode_id == next_id),
        None,
    )
    return bool(next_episode and next_episode.continuation_source)


def _route_alias(route_id: str) -> str:
    return next(alias for alias, value in SUPPORTED_ROUTES.items() if value == route_id)


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
