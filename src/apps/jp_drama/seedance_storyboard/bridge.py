"""Bridge imported Seedance storyboards into existing production contracts.

The creative upstream remains :class:`SeedanceStoryboardPackage`.  This module
creates a deterministic PreparedEpisode identity, provider-specific
GenerationPlanEpisode documents, and the existing pending ApprovedAssetBundle.
It performs no provider submissions.
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
    """The imported storyboard cannot be mapped without inventing information."""


def build_storyboard_prepared_episode(
    package: SeedanceStoryboardPackage,
    episode_id: str,
    *,
    fps: int = 30,
) -> PreparedEpisode:
    """Create the deterministic PreparedEpisode identity used by existing gates."""
    episode = _episode(package, episode_id)
    assets = _episode_assets(package, episode)
    scene_assets = [item for item in assets if item.kind == "scene"]
    if not scene_assets:
        raise SeedanceStoryboardBridgeError(
            f"{episode_id} requires at least one S asset for production planning"
        )

    package_key = package.content_digest.split(":", 1)[1][:12]
    prepared_episode_id = f"seedance_{package_key}_{episode_id.lower()}"
    project_id = f"project_{prepared_episode_id}"
    primary_scene = scene_assets[0]

    character_seeds = [
        CharacterSeed(
            seed_id=item.asset_id,
            source_character_id=item.asset_id,
            name=item.name,
            description=item.prompt,
            visual_prompt=item.prompt,
        )
        for item in assets
        if item.kind == "character"
    ]
    location_seeds = [
        LocationSeed(
            seed_id=item.asset_id,
            source_location_id=item.asset_id,
            name=item.name,
            description=item.prompt,
            continuity_rules=[
                "Seedance storyboard upload-slot identity and spatial layout must remain stable"
            ],
            visual_prompt=item.prompt,
        )
        for item in assets
        if item.kind == "scene"
    ]
    prop_seeds = [
        PropSeed(
            seed_id=item.asset_id,
            source_prop_id=item.asset_id,
            name=item.name,
            story_function=item.story_function or item.name,
            visual_prompt=item.prompt,
        )
        for item in assets
        if item.kind == "prop"
    ]

    frames: list[StoryboardFrameDraft] = []
    intents: list[RenderIntent] = []
    graph_nodes: list[RenderTaskNode] = []
    budget_items: list[ShotBudgetItem] = []
    for beat in episode.timeline:
        referenced = _referenced_assets(episode, beat.text)
        if not referenced:
            referenced = assets
        characters = [item.asset_id for item in referenced if item.kind == "character"]
        props = [item.asset_id for item in referenced if item.kind == "prop"]
        scenes = [item.asset_id for item in referenced if item.kind == "scene"]
        location_id = scenes[0] if scenes else primary_scene.asset_id
        shot_id = f"{episode_id.lower()}_storyboard_{beat.order:02d}"
        intent_id = f"intent_{shot_id}"
        duration = float(beat.end_seconds - beat.start_seconds)
        camera = _camera_from_text(beat.text)
        strategy = (
            RenderStrategy.NATIVE_AV
            if episode.sound_prompt
            else RenderStrategy.SILENT_VIDEO
        )
        frames.append(
            StoryboardFrameDraft(
                frame_id=f"frame_{shot_id}",
                source_shot_id=shot_id,
                adapted_beat_id=f"adapted_{shot_id}",
                order=beat.order,
                duration_seconds=duration,
                location_seed_id=location_id,
                character_seed_ids=characters,
                prop_seed_ids=props,
                action=beat.text,
                visual_description=(
                    f"{episode.style_prompt}\n{beat.text}"
                    + (
                        f"\nReference direction: {episode.reference_prompt}"
                        if episode.reference_prompt
                        else ""
                    )
                ),
                camera=camera,
                dialogue_cues=[],
                audio=AudioDraft(
                    ambience=episode.sound_prompt,
                    sound_effects=[episode.sound_prompt] if episode.sound_prompt else [],
                    generated_native_audio=bool(episode.sound_prompt),
                ),
                render_intent_id=intent_id,
            )
        )
        task_type = "generate_native_av" if strategy is RenderStrategy.NATIVE_AV else "generate_video"
        generation_task = f"task_{shot_id}_{task_type}"
        final_task = f"task_{shot_id}_finalize"
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
                    task_type=task_type,
                    depends_on=[],
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

    mappings = [MappingEntry(source_id=item.asset_id, target_id=item.asset_id) for item in assets]
    shot_mappings = [
        MappingEntry(
            source_id=f"{episode_id.lower()}_storyboard_{item.order:02d}",
            target_id=f"frame_{episode_id.lower()}_storyboard_{item.order:02d}",
        )
        for item in episode.timeline
    ]
    warnings = [
        ReadinessIssue(
            code="seedance_dialogue_not_structured",
            severity="warning",
            message=(
                "Native storyboard dialogue and sound remain inside the upstream prompt; "
                "speaker-specific TTS profiles are not invented by this bridge."
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
            project_id=project_id,
            title=f"{package.project_title} {episode.episode_id} {episode.title}",
            description=(
                "Imported from the pinned Seedance2 Storyboard Generator creative contract"
            ),
            fps=fps,
            target_duration_seconds=float(episode.duration_seconds),
            episode_number=int(episode.episode_id[1:]),
            series_id=f"series_seedance_{package_key}",
        ),
        character_seeds=character_seeds,
        location_seeds=location_seeds,
        prop_seeds=prop_seeds,
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
            characters=[item for item in mappings if item.source_id.startswith("C")],
            locations=[item for item in mappings if item.source_id.startswith("S")],
            props=[item for item in mappings if item.source_id.startswith("P")],
            shots=shot_mappings,
            adapted_beats=[
                MappingEntry(
                    source_id=f"beat_{item.order:02d}",
                    target_id=f"adapted_{episode_id.lower()}_storyboard_{item.order:02d}",
                )
                for item in episode.timeline
            ],
            mapping_coverage=1.0,
        ),
        readiness_report=ReadinessReport(
            package_id=f"package_{prepared_episode_id}",
            episode_id=prepared_episode_id,
            duration_seconds=float(episode.duration_seconds),
            shot_count=len(frames),
            character_count=len(character_seeds),
            location_count=len(location_seeds),
            prop_count=len(prop_seeds),
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
    """Compile H3, Wan, or Seedance plans while preserving upstream timing."""
    if route_id not in set(SUPPORTED_ROUTES.values()):
        raise SeedanceStoryboardBridgeError(f"unsupported storyboard route: {route_id}")
    episode = _episode(package, episode_id)
    if prepared.source_digest != package.content_digest:
        raise SeedanceStoryboardBridgeError("PreparedEpisode does not belong to the package")
    if prepared.project_draft.episode_number != int(episode_id[1:]):
        raise SeedanceStoryboardBridgeError("PreparedEpisode episode number does not match")

    adapter = registry.require(route_id)
    capabilities = adapter.capabilities()
    fps = prepared.project_draft.fps
    prepared_digest = prepared_content_digest(prepared)
    assets_by_id = {item.asset_id: item for item in package.assets}
    episode_assets = _episode_assets(package, episode)
    primary_scene = next(
        (item for item in episode_assets if item.kind == "scene"),
        None,
    )
    if primary_scene is None:
        raise SeedanceStoryboardBridgeError(f"{episode_id} has no scene asset")

    groups: list[list[TimelineBeat]]
    if route_id == "wan/i2v":
        groups = [[item] for item in episode.timeline]
    else:
        groups = [list(episode.timeline)]

    segments: list[GenerationSegment] = []
    for order, group in enumerate(groups, start=1):
        group_start = group[0].start_seconds
        group_end = group[-1].end_seconds
        editorial_frames = round(float(group_end - group_start) * fps)
        requested_seconds = max(
            math.ceil(capabilities.min_duration_seconds),
            math.ceil(editorial_frames / fps),
        )
        if requested_seconds > math.floor(capabilities.max_duration_seconds):
            raise SeedanceStoryboardBridgeError(
                f"{episode_id} group {order} exceeds {route_id} duration capability"
            )
        requested_frames = requested_seconds * fps
        handle_frames = requested_frames - editorial_frames
        used_start = handle_frames // 2
        used_end = used_start + editorial_frames
        segment_id = _stable_id(
            "storyboard_segment",
            package.content_digest,
            episode_id,
            route_id,
            str(order),
            str(group_start),
            str(group_end),
        )
        referenced_assets = _group_assets(episode, group)
        if not referenced_assets:
            referenced_assets = episode_assets
        characters = [item.asset_id for item in referenced_assets if item.kind == "character"]
        props = [item.asset_id for item in referenced_assets if item.kind == "prop"]
        scenes = [item.asset_id for item in referenced_assets if item.kind == "scene"]
        location_id = scenes[0] if scenes else primary_scene.asset_id
        continuity_group_id = _stable_id(
            "storyboard_continuity",
            package.content_digest,
            episode_id,
            location_id,
        )

        editorial_shots: list[EditorialShot] = []
        for shot_order, beat in enumerate(group, start=1):
            local_start = round(float(beat.start_seconds - group_start) * fps)
            local_end = round(float(beat.end_seconds - group_start) * fps)
            beat_assets = _referenced_assets(episode, beat.text) or referenced_assets
            editorial_shots.append(
                EditorialShot(
                    editorial_shot_id=f"{segment_id}_shot_{shot_order:02d}",
                    segment_id=segment_id,
                    order_within_segment=shot_order,
                    start_frame=local_start,
                    end_frame=local_end,
                    framing=_camera_from_text(beat.text).shot_size,
                    camera_movement=_camera_from_text(beat.text).movement,
                    visual_action=beat.text,
                    character_ids=[
                        item.asset_id for item in beat_assets if item.kind == "character"
                    ],
                    source_beat_ids=[f"{episode_id}_beat_{beat.order:02d}"],
                    source_shot_ids=[f"{episode_id.lower()}_storyboard_{beat.order:02d}"],
                )
            )

        reference_ids = [
            _reference_id(item) for item in referenced_assets
        ]
        if route_id == "wan/i2v":
            reference_ids.append(f"ref_first_{segment_id}")
        prompt = (
            episode.raw_prompt
            if len(group) == len(episode.timeline)
            else _wan_prompt(episode, group[0])
        )
        native_audio = route_id in {
            "minimax/h3-reference-av",
            "seedance/platform",
        } and bool(episode.sound_prompt)
        segment = GenerationSegment(
            segment_id=segment_id,
            order=order,
            parent_shot_ids=[
                f"{episode_id.lower()}_storyboard_{item.order:02d}" for item in group
            ],
            continuity_group_id=continuity_group_id,
            provider_route_id=route_id,
            timeline_fps=fps,
            editorial_start_frame=round(float(group_start) * fps),
            editorial_end_frame=round(float(group_end) * fps),
            editorial_frame_count=editorial_frames,
            editorial_duration_seconds=Decimal(editorial_frames) / Decimal(fps),
            requested_duration_seconds=requested_seconds,
            used_start_frame=used_start,
            used_end_frame=used_end,
            complexity=_complexity(editorial_shots, characters, props),
            editorial_shots=editorial_shots,
            character_ids=characters,
            location_id=location_id,
            prop_ids=props,
            dialogue_slices=[],
            reference_asset_ids=reference_ids,
            prompt_bundle=PromptBundle(
                narrative_summary=f"{package.project_title} {episode.episode_id} {episode.title}",
                visual_prompt=f"{episode.style_prompt}\n{prompt}",
                motion_prompt="\n".join(item.text for item in group),
                camera_prompt="Follow the exact timed storyboard and upstream camera directions.",
                timed_shot_prompt=prompt,
                audio_prompt=episode.sound_prompt,
                negative_constraints=[
                    "Do not change uploaded character, scene, or prop identities.",
                    "Do not generate subtitles, logos, watermarks, or modern text.",
                    "Avoid duplicate people, malformed hands, missing props, and black frames.",
                ],
            ),
            audio_strategy="native_av" if native_audio else "silent",
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
        segments.append(segment)

    contracts, requirements = _continuity_and_requirements(
        package,
        episode,
        segments,
        assets_by_id,
        route_id,
    )
    graph = _render_graph(segments, route_id)
    errors: list[GenerationReadinessIssue] = []
    warnings: list[GenerationReadinessIssue] = [
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
                "Dialogue and sound are preserved verbatim in the upstream prompt; "
                "speaker-specific TTS is unavailable until a script contract is linked."
            ),
        ),
    ]
    if adapter.descriptor().execution_mode == "manual":
        warnings.append(
            GenerationReadinessIssue(
                code="manual_operator_route",
                scope="route",
                severity="warning",
                message="The Seedance plan is exported for operator execution, not browser automation.",
            )
        )
    if route_id == "wan/i2v" and episode.sound_prompt:
        warnings.append(
            GenerationReadinessIssue(
                code="wan_audio_postproduction_required",
                scope="quality",
                severity="warning",
                message="Wan receives a visual-only plan; upstream sound requires later audio postproduction.",
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

    cost_items: list[GenerationCostItem] = []
    totals: dict[str, Decimal] = {}
    unknown: list[str] = []
    snapshots: set[str] = set()
    reference_image_calls = 0
    if route_id == "wan/i2v":
        image_adapter = registry.get("wan/image")
        for segment in segments:
            reference_image_calls += 1
            amount = None
            currency = None
            confidence = "unknown"
            snapshot = None
            if image_adapter is not None:
                image_spec = ShotGenerationSpec(
                    source_digest=prepared_digest,
                    shot_id=segment.segment_id,
                    task_id=f"task_first_frame_{segment.segment_id}",
                    modality="image",
                    duration_seconds=1,
                    prompt=segment.prompt_bundle.visual_prompt,
                    audio_strategy="silent",
                    required_capabilities=ProviderCapabilitiesRequired(modality="image"),
                )
                estimate = image_adapter.estimate_cost(image_spec)
                if estimate.native_cost is not None:
                    amount = estimate.native_cost.amount
                    currency = estimate.native_cost.currency
                    confidence = estimate.confidence
                    totals[currency] = totals.get(currency, Decimal("0")) + amount
                snapshot = estimate.price_snapshot_id
            if amount is None:
                unknown.append(f"{segment.segment_id}:reference_image")
            if snapshot:
                snapshots.add(snapshot)
            cost_items.append(
                GenerationCostItem(
                    segment_id=segment.segment_id,
                    component="reference_image",
                    calls=1,
                    amount=amount,
                    currency=currency,
                    confidence=confidence,
                    price_snapshot_id=snapshot,
                )
            )

    for segment in segments:
        spec = segment_to_generation_spec(segment, prepared_digest, capabilities)
        validation = adapter.validate(spec)
        for issue in validation.errors:
            errors.append(
                GenerationReadinessIssue(
                    code=issue.code,
                    scope="route",
                    severity="error",
                    message=issue.message,
                    segment_id=segment.segment_id,
                    source_shot_id=segment.parent_shot_ids[0],
                )
            )
        estimate = adapter.estimate_cost(spec)
        amount = None
        currency = None
        if estimate.native_cost is not None:
            amount = estimate.native_cost.amount
            currency = estimate.native_cost.currency
            totals[currency] = totals.get(currency, Decimal("0")) + amount
        else:
            unknown.append(f"{segment.segment_id}:video")
        if estimate.price_snapshot_id:
            snapshots.add(estimate.price_snapshot_id)
        cost_items.append(
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
        if segment.audio_strategy == "native_av":
            cost_items.append(
                GenerationCostItem(
                    segment_id=segment.segment_id,
                    component="native_audio",
                    calls=0,
                    amount=Decimal("0"),
                    currency=currency,
                    confidence="exact",
                    price_snapshot_id=estimate.price_snapshot_id,
                )
            )

    planning_ready = True
    route_ready = not errors
    plan_id = _stable_id(
        "storyboard_generation_plan",
        package.content_digest,
        episode_id,
        route_id,
        STORYBOARD_BRIDGE_VERSION,
    )
    policy_digest = _digest(
        f"{STORYBOARD_BRIDGE_VERSION}|{route_id}|preserve-upstream-timeline"
    )
    return GenerationPlanEpisode.build_with_digest(
        generation_plan_episode_id=plan_id,
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
        render_graph=graph,
        cost_plan=GenerationCostPlan(
            reference_image_calls=reference_image_calls,
            video_calls=len(segments),
            tts_calls=0,
            native_audio_calls=sum(item.audio_strategy == "native_av" for item in segments),
            expected_calls=reference_image_calls + len(segments),
            hard_maximum_calls=reference_image_calls + len(segments),
            items=cost_items,
            totals_by_currency=totals,
            unknown_cost_components=sorted(set(unknown)),
            pricing_snapshot_dates=sorted(snapshots),
        ),
        readiness_report=GenerationReadinessReport(
            planning_ready=planning_ready,
            execution_route_ready=route_ready,
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
    """Reuse the existing hash-bound Asset Bundle contract."""
    return build_pending_asset_bundle(prepared, plan)


def _episode(
    package: SeedanceStoryboardPackage,
    episode_id: str,
) -> SeedanceStoryboardEpisode:
    try:
        return next(item for item in package.episodes if item.episode_id == episode_id)
    except StopIteration as exc:
        raise SeedanceStoryboardBridgeError(f"unknown storyboard episode: {episode_id}") from exc


def _episode_assets(
    package: SeedanceStoryboardPackage,
    episode: SeedanceStoryboardEpisode,
) -> list[StoryboardAsset]:
    by_id = {item.asset_id: item for item in package.assets}
    result = []
    for slot in episode.upload_slots:
        asset = by_id.get(slot.asset_id)
        if asset is None:
            raise SeedanceStoryboardBridgeError(
                f"{episode.episode_id} references missing asset {slot.asset_id}"
            )
        if asset not in result:
            result.append(asset)
    return result


def _referenced_assets(
    episode: SeedanceStoryboardEpisode,
    text: str,
) -> list[StoryboardAsset]:
    slot_to_asset = {item.slot_name: item.asset_id for item in episode.upload_slots}
    referenced_ids = [
        slot_to_asset[item]
        for item in _REFERENCE_TOKEN.findall(text)
        if item in slot_to_asset
    ]
    # Asset objects are attached later by _group_assets; this helper is replaced
    # with package-aware values through the private cache populated below.
    return [
        _ASSET_CACHE[item]
        for item in referenced_ids
        if item in _ASSET_CACHE
    ]


_ASSET_CACHE: dict[str, StoryboardAsset] = {}


def _group_assets(
    episode: SeedanceStoryboardEpisode,
    beats: list[TimelineBeat],
) -> list[StoryboardAsset]:
    result: list[StoryboardAsset] = []
    for beat in beats:
        for asset in _referenced_assets(episode, beat.text):
            if asset not in result:
                result.append(asset)
    return result


def _reference_id(asset: StoryboardAsset) -> str:
    prefix = {
        "character": "ref_char_",
        "scene": "ref_loc_",
        "prop": "ref_prop_",
    }[asset.kind]
    return prefix + asset.asset_id


def _camera_from_text(text: str) -> CameraDraft:
    shot_size = (
        "extreme_close_up"
        if any(item in text for item in ("极近特写", "超特写"))
        else "close_up"
        if any(item in text for item in ("特写", "近景"))
        else "long"
        if any(item in text for item in ("全景", "远景"))
        else "medium"
    )
    movement = (
        "push_in"
        if any(item in text for item in ("推近", "推进"))
        else "pull_out"
        if any(item in text for item in ("拉远", "后拉"))
        else "pan_left"
        if "左摇" in text
        else "pan_right"
        if "右摇" in text
        else "follow"
        if any(item in text for item in ("跟拍", "跟随"))
        else "static"
    )
    angle = "low" if "低角度" in text else "high" if "高角度" in text else "eye_level"
    speed = "fast" if any(item in text for item in ("快速", "迅速")) else "slow" if "缓慢" in text else "normal"
    return CameraDraft(shot_size=shot_size, angle=angle, movement=movement, speed=speed)


def _complexity(
    shots: list[EditorialShot],
    characters: list[str],
    props: list[str],
) -> SegmentComplexity:
    character_score = 0 if len(characters) <= 1 else 2 if len(characters) == 2 else 4
    action_score = min(4, len(shots))
    camera_score = sum(item.camera_movement != "static" for item in shots)
    continuity_score = 1 if len(shots) > 1 else 0
    prop_score = 1 if props else 0
    score = character_score + action_score + camera_score + continuity_score + prop_score
    level = "low" if score <= 3 else "medium" if score <= 7 else "high" if score <= 11 else "very_high"
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


def _continuity_and_requirements(
    package: SeedanceStoryboardPackage,
    episode: SeedanceStoryboardEpisode,
    segments: list[GenerationSegment],
    assets_by_id: dict[str, StoryboardAsset],
    route_id: str,
) -> tuple[list[ContinuityContract], list[ReferenceAssetRequirement]]:
    requirements: dict[str, ReferenceAssetRequirement] = {}
    contracts: list[ContinuityContract] = []
    for segment in segments:
        refs = []
        for asset_id in [*segment.character_ids, segment.location_id, *segment.prop_ids]:
            asset = assets_by_id[asset_id]
            ref_id = _reference_id(asset)
            refs.append(ref_id)
            role = {
                "character": "character",
                "scene": "location",
                "prop": "prop",
            }[asset.kind]
            existing = requirements.get(ref_id)
            segment_ids = sorted(
                set((existing.required_for_segment_ids if existing else []) + [segment.segment_id])
            )
            requirements[ref_id] = ReferenceAssetRequirement(
                asset_id=ref_id,
                role=role,
                subject_id=asset.asset_id,
                continuity_group_id=segment.continuity_group_id,
                required_for_segment_ids=segment_ids,
            )
        if route_id == "wan/i2v":
            first_id = f"ref_first_{segment.segment_id}"
            refs.append(first_id)
            requirements[first_id] = ReferenceAssetRequirement(
                asset_id=first_id,
                role="first_frame",
                subject_id=segment.segment_id,
                continuity_group_id=segment.continuity_group_id,
                required_for_segment_ids=[segment.segment_id],
            )
        contracts.append(
            ContinuityContract(
                continuity_group_id=segment.continuity_group_id,
                character_appearance_locks={
                    item: assets_by_id[item].prompt for item in segment.character_ids
                },
                location_id=segment.location_id,
                location_lock=assets_by_id[segment.location_id].prompt,
                prop_state_locks={
                    item: assets_by_id[item].prompt for item in segment.prop_ids
                },
                reference_asset_ids=sorted(refs),
            )
        )
    # A Wan plan may have repeated continuity IDs; collapse identical contracts.
    unique_contracts: dict[str, ContinuityContract] = {}
    for contract in contracts:
        existing = unique_contracts.get(contract.continuity_group_id)
        if existing is None:
            unique_contracts[contract.continuity_group_id] = contract
            continue
        merged_refs = sorted(set(existing.reference_asset_ids + contract.reference_asset_ids))
        merged_chars = {**existing.character_appearance_locks, **contract.character_appearance_locks}
        merged_props = {**existing.prop_state_locks, **contract.prop_state_locks}
        unique_contracts[contract.continuity_group_id] = existing.model_copy(
            update={
                "reference_asset_ids": merged_refs,
                "character_appearance_locks": merged_chars,
                "prop_state_locks": merged_props,
            }
        )
    return list(unique_contracts.values()), sorted(requirements.values(), key=lambda item: item.asset_id)


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
        nodes.append(
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
            )
        )
        trim = f"trim_{segment.segment_id}"
        validate = f"validate_{segment.segment_id}"
        nodes.extend(
            [
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
    concat = "concat_storyboard_episode"
    nodes.append(
        GenerationTaskNode(
            task_id=concat,
            task_type="concat_episode",
            depends_on=validated,
        )
    )
    nodes.append(
        GenerationTaskNode(
            task_id="validate_storyboard_episode",
            task_type="validate_episode",
            depends_on=[concat],
        )
    )
    return GenerationRenderGraph(nodes=nodes)


def _wan_prompt(episode: SeedanceStoryboardEpisode, beat: TimelineBeat) -> str:
    return (
        f"{episode.style_prompt}\n"
        f"{beat.start_seconds}-{beat.end_seconds}秒: {beat.text}\n"
        "Generate one continuous visual action. Do not render subtitles or spoken text."
    )


def _next_episode_continues(
    package: SeedanceStoryboardPackage,
    episode_id: str,
) -> bool:
    number = int(episode_id[1:])
    next_id = f"E{number + 1:02d}"
    next_episode = next((item for item in package.episodes if item.episode_id == next_id), None)
    return bool(next_episode and next_episode.continuation_source)


def _route_alias(route_id: str) -> str:
    return next(key for key, value in SUPPORTED_ROUTES.items() if value == route_id)


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
