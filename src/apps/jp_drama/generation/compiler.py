"""Deterministic PreparedEpisode to GenerationPlanEpisode compiler."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal

from ..preparation.models import PreparedEpisode, StoryboardFrameDraft
from ..rendering.provider_core import (
    DialogueLine,
    ProviderCapabilities,
    ProviderCapabilitiesRequired,
    ReferenceAsset,
    ShotGenerationSpec,
)
from ..rendering.provider_registry import ProviderRegistry
from .models import (
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
from .policy import ProviderSegmentationProfile


class GenerationCompilationError(RuntimeError):
    """The source package is structurally unsuitable for deterministic segmentation."""


@dataclass(frozen=True)
class _AtomicUnit:
    frame: StoryboardFrameDraft
    local_start_frame: int
    local_end_frame: int
    global_start_frame: int
    global_end_frame: int

    @property
    def frame_count(self) -> int:
        return self.local_end_frame - self.local_start_frame


_STRUCTURAL_BLOCKERS = {
    "mapping_incomplete",
    "render_intents_unresolved",
    "render_graph_invalid",
    "cost_strategy_mismatch",
}


def compile_generation_plan(
    prepared: PreparedEpisode,
    *,
    profile: ProviderSegmentationProfile,
    registry: ProviderRegistry,
) -> GenerationPlanEpisode:
    """Compile an offline, deterministic generation plan without provider calls."""
    blockers = [
        issue.code
        for issue in prepared.readiness_report.errors
        if issue.code in _STRUCTURAL_BLOCKERS
    ]
    if blockers:
        raise GenerationCompilationError(
            "prepared episode contains structural blockers: " + ", ".join(sorted(blockers))
        )

    adapter = registry.require(profile.route_id)
    capabilities = adapter.capabilities()
    fps = prepared.project_draft.fps
    target_frame_count = round(prepared.project_draft.target_duration_seconds * fps)
    source_digest = _digest_text(prepared.to_canonical_json(indent=None))
    frame_global_start = 0
    segments: list[GenerationSegment] = []
    planning_issues: list[GenerationReadinessIssue] = []
    route_issues: list[GenerationReadinessIssue] = []
    quality_warnings: list[GenerationReadinessIssue] = []

    frames = sorted(prepared.storyboard_frame_drafts, key=lambda item: item.order)
    for frame in frames:
        frame_count = round(frame.duration_seconds * fps)
        units = _extract_atomic_units(frame, frame_global_start, fps)
        complexity = _score_complexity(frame)
        band = profile.policy.band_for(complexity.level)
        for unit in units:
            if unit.frame_count > band.maximum_seconds * fps:
                planning_issues.append(
                    GenerationReadinessIssue(
                        code="insufficient_segmentation_evidence",
                        scope="planning",
                        severity="error",
                        message=(
                            "A source interval exceeds the policy maximum but contains no "
                            "explicit dialogue or storyboard boundary. The compiler did not "
                            "invent an action boundary."
                        ),
                        source_shot_id=frame.source_shot_id,
                    )
                )
        groups = _group_units(
            units,
            complexity=complexity,
            capabilities=capabilities,
            profile=profile,
            fps=fps,
        )
        for group in groups:
            segment = _build_segment(
                prepared,
                frame,
                group,
                order=len(segments) + 1,
                profile=profile,
                capabilities=capabilities,
                fps=fps,
            )
            segments.append(segment)
            if segment.requested_duration_seconds == 15:
                quality_warnings.append(
                    GenerationReadinessIssue(
                        code="maximum_duration_segment",
                        scope="quality",
                        severity="warning",
                        message="A 15-second segment requires real-provider Canary validation.",
                        segment_id=segment.segment_id,
                        source_shot_id=frame.source_shot_id,
                    )
                )
            if len(segment.character_ids) >= 3:
                quality_warnings.append(
                    GenerationReadinessIssue(
                        code="many_characters",
                        scope="quality",
                        severity="warning",
                        message="Three or more characters can reduce identity consistency.",
                        segment_id=segment.segment_id,
                        source_shot_id=frame.source_shot_id,
                    )
                )
        frame_global_start += frame_count

    if frame_global_start != target_frame_count:
        planning_issues.append(
            GenerationReadinessIssue(
                code="editorial_duration_mismatch",
                scope="planning",
                severity="error",
                message=(
                    f"source storyboard frames total {frame_global_start} frames but the "
                    f"episode target is {target_frame_count} frames"
                ),
            )
        )

    _validate_dialogue_coverage(prepared, segments, planning_issues)
    for segment in segments:
        spec = segment_to_generation_spec(segment, source_digest, capabilities)
        report = adapter.validate(spec)
        for issue in report.errors:
            route_issues.append(
                GenerationReadinessIssue(
                    code=issue.code,
                    scope="route",
                    severity="error",
                    message=issue.message,
                    segment_id=segment.segment_id,
                    source_shot_id=segment.parent_shot_ids[0],
                )
            )
        if len(segment.editorial_shots) > 1 and not capabilities.multi_shot:
            route_issues.append(
                GenerationReadinessIssue(
                    code="route_multi_shot_not_migrated",
                    scope="route",
                    severity="error",
                    message=(
                        f"{profile.route_id} is currently registered without multi-shot "
                        "execution support; the plan is valid but cannot yet execute through "
                        "that adapter."
                    ),
                    segment_id=segment.segment_id,
                    source_shot_id=segment.parent_shot_ids[0],
                )
            )

    continuity_contracts, requirements = _build_continuity(prepared, segments)
    if any(item.generation_status == "missing" for item in requirements):
        quality_warnings.append(
            GenerationReadinessIssue(
                code="reference_assets_missing",
                scope="planning",
                severity="warning",
                message=(
                    "Reference assets are structurally specified but have not been "
                    "generated or approved."
                ),
            )
        )
    render_graph = _build_render_graph(segments, capabilities)
    cost_plan = _build_cost_plan(
        segments,
        source_digest=source_digest,
        capabilities=capabilities,
        profile=profile,
        registry=registry,
        route_issues=route_issues,
    )

    planning_ready = not planning_issues
    execution_route_ready = planning_ready and not route_issues
    report = GenerationReadinessReport(
        planning_ready=planning_ready,
        execution_route_ready=execution_route_ready,
        media_quality_validated=False,
        external_api_calls=0,
        errors=[*planning_issues, *route_issues],
        warnings=quality_warnings,
    )
    plan_id = _stable_id(
        "generation_plan",
        source_digest,
        profile.policy_digest,
        profile.route_id,
    )
    return GenerationPlanEpisode.build_with_digest(
        generation_plan_episode_id=plan_id,
        source_episode_id=prepared.episode_id,
        source_prepared_episode_digest=source_digest,
        policy_digest=profile.policy_digest,
        provider_profile_id=profile.profile_id,
        provider_route_id=profile.route_id,
        timeline_fps=fps,
        target_frame_count=target_frame_count,
        target_duration_seconds=_seconds(target_frame_count, fps),
        segments=segments,
        continuity_contracts=continuity_contracts,
        reference_asset_requirements=requirements,
        render_graph=render_graph,
        cost_plan=cost_plan,
        readiness_report=report,
    )


def segment_to_generation_spec(
    segment: GenerationSegment,
    source_digest: str,
    capabilities: ProviderCapabilities,
) -> ShotGenerationSpec:
    """Bridge a PR11 segment to the existing PR9 provider-neutral request contract."""
    use_i2v = capabilities.image_to_video and not capabilities.text_to_video
    if segment.provider_route_id == "wan/i2v":
        use_i2v = True
    references: list[ReferenceAsset] = []
    if use_i2v:
        first_frame_id = next(
            (item for item in segment.reference_asset_ids if item.startswith("ref_first_")),
            f"ref_first_{segment.segment_id}",
        )
        references.append(
            ReferenceAsset(
                asset_id=first_frame_id,
                uri=f"pending://{first_frame_id}",
                role="first_frame",
                order=0,
                subject_id=segment.segment_id,
            )
        )
    elif capabilities.reference_to_video:
        for order, asset_id in enumerate(segment.reference_asset_ids):
            if asset_id.startswith("ref_char_"):
                role = "character"
            elif asset_id.startswith("ref_loc_"):
                role = "location"
            elif asset_id.startswith("ref_prop_"):
                role = "prop"
            else:
                continue
            references.append(
                ReferenceAsset(
                    asset_id=asset_id,
                    uri=f"pending://{asset_id}",
                    role=role,
                    order=order,
                )
            )
    dialogue = [
        DialogueLine(
            cue_id=item.source_dialogue_id,
            speaker_character_id=item.speaker_character_id,
            text=item.text,
            start_seconds=float(_seconds(item.start_frame, segment.timeline_fps)),
            end_seconds=float(_seconds(item.end_frame, segment.timeline_fps)),
        )
        for item in segment.dialogue_slices
    ]
    required = ProviderCapabilitiesRequired(
        modality="video",
        text_to_video=not use_i2v,
        image_to_video=use_i2v,
        reference_to_video=bool(references) and not use_i2v,
        native_audio=segment.audio_strategy == "native_av",
        multi_shot=len(segment.editorial_shots) > 1,
    )
    return ShotGenerationSpec(
        source_digest=source_digest,
        shot_id=segment.segment_id,
        task_id=f"task_video_{segment.segment_id}",
        modality="video",
        duration_seconds=segment.requested_duration_seconds,
        aspect_ratio="9:16",
        resolution="720P",
        prompt=segment.prompt_bundle.timed_shot_prompt,
        dialogue=dialogue,
        references=references,
        audio_strategy=(
            "native_av"
            if segment.audio_strategy == "native_av"
            else "external_audio_post"
            if segment.audio_strategy == "external_audio_post"
            else "silent"
        ),
        required_capabilities=required,
    )


def _extract_atomic_units(
    frame: StoryboardFrameDraft,
    global_start: int,
    fps: int,
) -> list[_AtomicUnit]:
    frame_count = round(frame.duration_seconds * fps)
    boundaries = {0, frame_count}
    for cue in frame.dialogue_cues:
        boundaries.add(max(0, min(frame_count, round(cue.start_seconds * fps))))
        boundaries.add(max(0, min(frame_count, round(cue.end_seconds * fps))))
    ordered = sorted(boundaries)
    return [
        _AtomicUnit(
            frame=frame,
            local_start_frame=start,
            local_end_frame=end,
            global_start_frame=global_start + start,
            global_end_frame=global_start + end,
        )
        for start, end in zip(ordered, ordered[1:])
        if end > start
    ]


def _group_units(
    units: list[_AtomicUnit],
    *,
    complexity: SegmentComplexity,
    capabilities: ProviderCapabilities,
    profile: ProviderSegmentationProfile,
    fps: int,
) -> list[list[_AtomicUnit]]:
    band = profile.policy.band_for(complexity.level)
    maximum_seconds = min(band.maximum_seconds, math.floor(capabilities.max_duration_seconds))
    if (
        complexity.level == "low"
        and profile.policy.allow_low_complexity_15_seconds
        and len(units[0].frame.character_seed_ids) <= profile.policy.max_characters_for_15_seconds
        and (
            not profile.policy.strict_lip_sync_shortens_segment
            or not units[0].frame.dialogue_cues
        )
        and not units[0].frame.prop_seed_ids
        and units[0].frame.camera.movement in {"static", "none"}
    ):
        maximum_seconds = min(15, math.floor(capabilities.max_duration_seconds))
    max_frames = maximum_seconds * fps
    groups: list[list[_AtomicUnit]] = []
    current: list[_AtomicUnit] = []
    current_frames = 0
    for unit in units:
        would_exceed = current and current_frames + unit.frame_count > max_frames
        shot_limit = current and len(current) >= profile.policy.max_internal_editorial_shots
        if would_exceed or shot_limit:
            groups.append(current)
            current = []
            current_frames = 0
        current.append(unit)
        current_frames += unit.frame_count
    if current:
        groups.append(current)

    provider_min_frames = math.ceil(capabilities.min_duration_seconds * fps)
    if len(groups) > 1 and _group_frames(groups[-1]) < provider_min_frames:
        combined_count = len(groups[-2]) + len(groups[-1])
        combined_frames = _group_frames(groups[-2]) + _group_frames(groups[-1])
        provider_max_frames = math.floor(capabilities.max_duration_seconds * fps)
        if (
            combined_count <= profile.policy.max_internal_editorial_shots
            and combined_frames <= provider_max_frames
        ):
            groups[-2].extend(groups[-1])
            groups.pop()
    return groups


def _build_segment(
    prepared: PreparedEpisode,
    frame: StoryboardFrameDraft,
    group: list[_AtomicUnit],
    *,
    order: int,
    profile: ProviderSegmentationProfile,
    capabilities: ProviderCapabilities,
    fps: int,
) -> GenerationSegment:
    global_start = group[0].global_start_frame
    global_end = group[-1].global_end_frame
    local_start = group[0].local_start_frame
    local_end = group[-1].local_end_frame
    editorial_frames = global_end - global_start
    requested_seconds = max(
        math.ceil(capabilities.min_duration_seconds),
        math.ceil(editorial_frames / fps),
    )
    requested_seconds = min(requested_seconds, math.floor(capabilities.max_duration_seconds))
    requested_frames = requested_seconds * fps
    handle_frames = requested_frames - editorial_frames
    used_start = handle_frames // 2
    used_end = used_start + editorial_frames
    segment_id = _stable_id(
        "segment",
        prepared.source_digest,
        profile.policy_digest,
        profile.route_id,
        frame.source_shot_id,
        str(local_start),
        str(local_end),
    )
    continuity_group_id = _continuity_group_id(frame, profile.route_id)
    dialogue_slices: list[DialogueSlice] = []
    cue_to_slice: dict[str, str] = {}
    for cue in frame.dialogue_cues:
        cue_start = round(cue.start_seconds * fps)
        cue_end = round(cue.end_seconds * fps)
        if cue_start >= local_start and cue_end <= local_end:
            slice_id = _stable_id("dialogue", segment_id, cue.cue_id)
            cue_to_slice[cue.cue_id] = slice_id
            dialogue_slices.append(
                DialogueSlice(
                    dialogue_slice_id=slice_id,
                    source_dialogue_id=cue.cue_id,
                    speaker_character_id=cue.speaker_character_id,
                    text=cue.text,
                    start_frame=cue_start - local_start,
                    end_frame=cue_end - local_start,
                    lip_sync_required=True,
                    can_continue_over_reaction_shot=True,
                )
            )

    editorial_shots: list[EditorialShot] = []
    cursor = 0
    for shot_order, unit in enumerate(group, start=1):
        unit_cues = []
        speakers = []
        for cue in frame.dialogue_cues:
            cue_start = round(cue.start_seconds * fps)
            cue_end = round(cue.end_seconds * fps)
            overlaps = cue_start < unit.local_end_frame and cue_end > unit.local_start_frame
            if overlaps and cue.cue_id in cue_to_slice:
                unit_cues.append(cue_to_slice[cue.cue_id])
                speakers.append(cue.speaker_character_id)
        editorial_shots.append(
            EditorialShot(
                editorial_shot_id=_stable_id(
                    "editorial",
                    segment_id,
                    str(shot_order),
                    str(unit.local_start_frame),
                    str(unit.local_end_frame),
                ),
                segment_id=segment_id,
                order_within_segment=shot_order,
                start_frame=cursor,
                end_frame=cursor + unit.frame_count,
                framing=frame.camera.shot_size,
                camera_movement=frame.camera.movement,
                visual_action=frame.action,
                emotion=None,
                character_ids=list(frame.character_seed_ids),
                active_speaker_id=speakers[0] if len(set(speakers)) == 1 else None,
                dialogue_slice_ids=unit_cues,
                source_beat_ids=[frame.adapted_beat_id],
                source_shot_ids=[frame.source_shot_id],
            )
        )
        cursor += unit.frame_count

    complexity = _score_complexity(frame, dialogue_count=len(dialogue_slices))
    reference_ids = _reference_ids(
        continuity_group_id,
        segment_id,
        frame.character_seed_ids,
        frame.location_seed_id,
        frame.prop_seed_ids,
        requires_first_frame=(
            capabilities.image_to_video and not capabilities.text_to_video
        )
        or profile.route_id == "wan/i2v",
    )
    source_intent = next(
        item for item in prepared.render_intents if item.shot_id == frame.source_shot_id
    )
    wants_native = frame.audio.generated_native_audio or source_intent.resolved_strategy.value == "native_av"
    audio_strategy = (
        "native_av"
        if wants_native and capabilities.native_audio
        else "external_audio_post"
        if dialogue_slices
        else "silent"
    )
    prompt_bundle = _prompt_bundle(frame, editorial_shots, dialogue_slices, fps)
    return GenerationSegment(
        segment_id=segment_id,
        order=order,
        parent_shot_ids=[frame.source_shot_id],
        continuity_group_id=continuity_group_id,
        provider_route_id=profile.route_id,
        timeline_fps=fps,
        editorial_start_frame=global_start,
        editorial_end_frame=global_end,
        editorial_frame_count=editorial_frames,
        editorial_duration_seconds=_seconds(editorial_frames, fps),
        requested_duration_seconds=requested_seconds,
        used_start_frame=used_start,
        used_end_frame=used_end,
        complexity=complexity,
        editorial_shots=editorial_shots,
        character_ids=list(frame.character_seed_ids),
        location_id=frame.location_seed_id,
        prop_ids=list(frame.prop_seed_ids),
        dialogue_slices=dialogue_slices,
        reference_asset_ids=reference_ids,
        prompt_bundle=prompt_bundle,
        audio_strategy=audio_strategy,
        transition_in="cut" if order == 1 else "continuation",
        transition_out="continuation",
    )


def _score_complexity(
    frame: StoryboardFrameDraft,
    *,
    dialogue_count: int | None = None,
) -> SegmentComplexity:
    character_count = len(frame.character_seed_ids)
    character_score = 0 if character_count <= 1 else 2 if character_count == 2 else 4
    actual_dialogue_count = len(frame.dialogue_cues) if dialogue_count is None else dialogue_count
    dialogue_score = 3 if actual_dialogue_count else 0
    if len({cue.speaker_character_id for cue in frame.dialogue_cues}) > 1:
        dialogue_score += 1
    camera_score = 0 if frame.camera.movement in {"static", "none"} else 1
    action_score = 1 if frame.action else 0
    continuity_score = 1 if character_count >= 2 else 0
    object_score = 1 if frame.prop_seed_ids else 0
    score = (
        character_score
        + dialogue_score
        + camera_score
        + action_score
        + continuity_score
        + object_score
    )
    level = "low" if score <= 3 else "medium" if score <= 7 else "high" if score <= 11 else "very_high"
    reasons = [f"{character_count} character(s)"]
    if actual_dialogue_count:
        reasons.append(f"{actual_dialogue_count} dialogue cue(s)")
    if camera_score:
        reasons.append(f"camera movement: {frame.camera.movement}")
    if object_score:
        reasons.append("explicit prop interaction")
    return SegmentComplexity(
        score=score,
        level=level,
        character_complexity=character_score,
        action_complexity=action_score,
        dialogue_complexity=dialogue_score,
        camera_complexity=camera_score,
        continuity_complexity=continuity_score,
        object_interaction_complexity=object_score,
        reasons=reasons,
    )


def _prompt_bundle(
    frame: StoryboardFrameDraft,
    shots: list[EditorialShot],
    dialogue: list[DialogueSlice],
    fps: int,
) -> PromptBundle:
    timed = ["複数ショットを時間指定どおりに生成する。"]
    for index, shot in enumerate(shots, start=1):
        start = _seconds(shot.start_frame, fps)
        end = _seconds(shot.end_frame, fps)
        timed.append(
            f"第{index}ショット[{start}-{end}秒] {shot.framing}: "
            f"{shot.visual_action} カメラは{shot.camera_movement}。"
        )
    dialogue_prompt = None
    if dialogue:
        dialogue_prompt = " | ".join(
            f"{item.speaker_character_id}: {item.text}" for item in dialogue
        )
    audio_parts = [part for part in [frame.audio.ambience, *frame.audio.sound_effects] if part]
    return PromptBundle(
        narrative_summary=frame.visual_description,
        visual_prompt=frame.visual_description,
        motion_prompt=frame.action,
        camera_prompt=(
            f"{frame.camera.shot_size}, {frame.camera.angle}, "
            f"{frame.camera.movement}, {frame.camera.speed}"
        ),
        timed_shot_prompt="\n".join(timed),
        dialogue_prompt=dialogue_prompt,
        audio_prompt="; ".join(audio_parts) or None,
        negative_constraints=[
            "人物の顔・髪型・衣装を変えない",
            "場所の配置と照明方向を変えない",
            "字幕・ロゴ・透かしを映像へ直接描画しない",
            "重複人物、壊れた手、黒画面を避ける",
        ],
    )


def _build_continuity(
    prepared: PreparedEpisode,
    segments: list[GenerationSegment],
) -> tuple[list[ContinuityContract], list[ReferenceAssetRequirement]]:
    character_map = {item.seed_id: item for item in prepared.character_seeds}
    location_map = {item.seed_id: item for item in prepared.location_seeds}
    prop_map = {item.seed_id: item for item in prepared.prop_seeds}
    by_group: dict[str, list[GenerationSegment]] = {}
    for segment in segments:
        by_group.setdefault(segment.continuity_group_id, []).append(segment)
    contracts: list[ContinuityContract] = []
    requirement_map: dict[str, dict] = {}
    for group_id, group_segments in sorted(by_group.items()):
        first = group_segments[0]
        location = location_map[first.location_id]
        refs = sorted({asset for segment in group_segments for asset in segment.reference_asset_ids})
        contracts.append(
            ContinuityContract(
                continuity_group_id=group_id,
                character_appearance_locks={
                    character_id: character_map[character_id].visual_prompt
                    for character_id in first.character_ids
                },
                location_id=first.location_id,
                location_lock=location.visual_prompt,
                prop_state_locks={
                    prop_id: f"present and visually stable: {prop_map[prop_id].visual_prompt}"
                    for prop_id in first.prop_ids
                },
                lighting=location.description,
                time_of_day=location.time_of_day,
                weather=None,
                screen_direction=None,
                reference_asset_ids=refs,
            )
        )
        for segment in group_segments:
            for asset_id in segment.reference_asset_ids:
                if asset_id.startswith("ref_char_"):
                    role = "character"
                    subject = next(
                        item for item in segment.character_ids if _subject_token(item) in asset_id
                    )
                elif asset_id.startswith("ref_loc_"):
                    role = "location"
                    subject = segment.location_id
                elif asset_id.startswith("ref_prop_"):
                    role = "prop"
                    subject = next(item for item in segment.prop_ids if _subject_token(item) in asset_id)
                else:
                    role = "first_frame"
                    subject = segment.segment_id
                record = requirement_map.setdefault(
                    asset_id,
                    {
                        "asset_id": asset_id,
                        "role": role,
                        "subject_id": subject,
                        "continuity_group_id": group_id,
                        "required_for_segment_ids": [],
                        "generation_status": "missing",
                    },
                )
                record["required_for_segment_ids"].append(segment.segment_id)
    requirements = [
        ReferenceAssetRequirement.model_validate(
            {
                **item,
                "required_for_segment_ids": sorted(set(item["required_for_segment_ids"])),
            }
        )
        for _, item in sorted(requirement_map.items())
    ]
    return contracts, requirements


def _build_render_graph(
    segments: list[GenerationSegment],
    capabilities: ProviderCapabilities,
) -> GenerationRenderGraph:
    nodes: list[GenerationTaskNode] = []
    final_tasks: list[str] = []
    use_i2v = capabilities.image_to_video and not capabilities.text_to_video
    for segment in segments:
        prefix = segment.segment_id
        ref_task = f"task_refs_{prefix}"
        nodes.append(
            GenerationTaskNode(
                task_id=ref_task,
                segment_id=segment.segment_id,
                task_type="prepare_references",
            )
        )
        visual_dependencies = [ref_task]
        if use_i2v or segment.provider_route_id == "wan/i2v":
            first_frame = f"task_first_{prefix}"
            nodes.append(
                GenerationTaskNode(
                    task_id=first_frame,
                    segment_id=segment.segment_id,
                    task_type="generate_first_frame",
                    depends_on=[ref_task],
                    external_api_required=True,
                    provider_route_id="wan/image" if segment.provider_route_id == "wan/i2v" else segment.provider_route_id,
                )
            )
            visual_dependencies = [first_frame]
        video_task = f"task_video_{prefix}"
        nodes.append(
            GenerationTaskNode(
                task_id=video_task,
                segment_id=segment.segment_id,
                task_type=(
                    "generate_native_av"
                    if segment.audio_strategy == "native_av"
                    else "generate_video"
                ),
                depends_on=visual_dependencies,
                external_api_required=True,
                provider_route_id=segment.provider_route_id,
            )
        )
        subtitle_task = f"task_subtitle_{prefix}"
        nodes.append(
            GenerationTaskNode(
                task_id=subtitle_task,
                segment_id=segment.segment_id,
                task_type="generate_subtitles",
                depends_on=[],
            )
        )
        mux_dependencies = [video_task, subtitle_task]
        if segment.audio_strategy == "external_audio_post":
            tts_task = f"task_tts_{prefix}"
            nodes.append(
                GenerationTaskNode(
                    task_id=tts_task,
                    segment_id=segment.segment_id,
                    task_type="generate_tts",
                    external_api_required=True,
                    provider_route_id="existing/qwen3-tts",
                )
            )
            mux_dependencies.append(tts_task)
        mux_task = f"task_mux_{prefix}"
        nodes.append(
            GenerationTaskNode(
                task_id=mux_task,
                segment_id=segment.segment_id,
                task_type="mux_segment",
                depends_on=mux_dependencies,
            )
        )
        trim_task = f"task_trim_{prefix}"
        nodes.append(
            GenerationTaskNode(
                task_id=trim_task,
                segment_id=segment.segment_id,
                task_type="trim_segment",
                depends_on=[mux_task],
            )
        )
        validate_task = f"task_validate_{prefix}"
        nodes.append(
            GenerationTaskNode(
                task_id=validate_task,
                segment_id=segment.segment_id,
                task_type="validate_segment",
                depends_on=[trim_task],
            )
        )
        final_tasks.append(validate_task)
    concat_task = "task_episode_concat"
    nodes.append(
        GenerationTaskNode(
            task_id=concat_task,
            task_type="concat_episode",
            depends_on=final_tasks,
        )
    )
    nodes.append(
        GenerationTaskNode(
            task_id="task_episode_validate",
            task_type="validate_episode",
            depends_on=[concat_task],
        )
    )
    return GenerationRenderGraph(nodes=nodes)


def _build_cost_plan(
    segments: list[GenerationSegment],
    *,
    source_digest: str,
    capabilities: ProviderCapabilities,
    profile: ProviderSegmentationProfile,
    registry: ProviderRegistry,
    route_issues: list[GenerationReadinessIssue],
) -> GenerationCostPlan:
    adapter = registry.require(profile.route_id)
    items: list[GenerationCostItem] = []
    totals: dict[str, Decimal] = {}
    unknown: list[str] = []
    snapshots: set[str] = set()
    reference_image_calls = 0
    video_calls = len(segments)
    tts_calls = 0
    native_audio_calls = 0
    use_i2v = capabilities.image_to_video and not capabilities.text_to_video
    if profile.route_id == "wan/i2v":
        use_i2v = True

    for segment in segments:
        spec = segment_to_generation_spec(segment, source_digest, capabilities)
        validation = adapter.validate(spec)
        estimate = adapter.estimate_cost(spec) if validation.valid else None
        amount = estimate.native_cost.amount if estimate and estimate.native_cost else None
        currency = estimate.native_cost.currency if estimate and estimate.native_cost else None
        confidence = estimate.confidence if estimate else "unknown"
        snapshot = estimate.price_snapshot_id if estimate else None
        items.append(
            GenerationCostItem(
                segment_id=segment.segment_id,
                component="video",
                calls=1,
                amount=amount,
                currency=currency,
                confidence=confidence,
                price_snapshot_id=snapshot,
            )
        )
        if amount is None or currency is None:
            unknown.append(f"video:{segment.segment_id}")
        else:
            totals[currency] = totals.get(currency, Decimal("0")) + amount
        if snapshot:
            snapshots.add(snapshot)

        if use_i2v:
            reference_image_calls += 1
            image_adapter = registry.get("wan/image")
            image_amount = None
            image_currency = None
            image_confidence = "unknown"
            image_snapshot = None
            if image_adapter is not None:
                image_spec = ShotGenerationSpec(
                    source_digest=source_digest,
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
                image_estimate = image_adapter.estimate_cost(image_spec)
                if image_estimate.native_cost:
                    image_amount = image_estimate.native_cost.amount
                    image_currency = image_estimate.native_cost.currency
                    image_confidence = image_estimate.confidence
                    image_snapshot = image_estimate.price_snapshot_id
            items.append(
                GenerationCostItem(
                    segment_id=segment.segment_id,
                    component="reference_image",
                    calls=1,
                    amount=image_amount,
                    currency=image_currency,
                    confidence=image_confidence,
                    price_snapshot_id=image_snapshot,
                )
            )
            if image_amount is None or image_currency is None:
                unknown.append(f"reference_image:{segment.segment_id}")
            else:
                totals[image_currency] = totals.get(image_currency, Decimal("0")) + image_amount
            if image_snapshot:
                snapshots.add(image_snapshot)

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
                    calls=1,
                    amount=Decimal("0"),
                    currency=currency,
                    confidence="exact" if currency else "unknown",
                    price_snapshot_id=snapshot,
                )
            )

    if profile.hard_budget_currency and profile.hard_budget_amount is not None:
        currency = profile.hard_budget_currency
        if unknown:
            route_issues.append(
                GenerationReadinessIssue(
                    code="hard_budget_has_unknown_costs",
                    scope="route",
                    severity="error",
                    message="A hard budget cannot be enforced while cost components are unknown.",
                )
            )
        elif totals.get(currency, Decimal("0")) > Decimal(str(profile.hard_budget_amount)):
            route_issues.append(
                GenerationReadinessIssue(
                    code="hard_budget_exceeded",
                    scope="route",
                    severity="error",
                    message=(
                        f"Known {currency} cost {totals.get(currency, Decimal('0'))} exceeds "
                        f"the hard budget {profile.hard_budget_amount}."
                    ),
                )
            )

    # Native AV is included in the video submission; retain the logical usage
    # metric without double-counting a second external API call.
    expected = reference_image_calls + video_calls + tts_calls
    return GenerationCostPlan(
        reference_image_calls=reference_image_calls,
        video_calls=video_calls,
        tts_calls=tts_calls,
        native_audio_calls=native_audio_calls,
        expected_calls=expected,
        hard_maximum_calls=expected * 2,
        items=items,
        totals_by_currency=totals,
        unknown_cost_components=sorted(set(unknown)),
        pricing_snapshot_dates=sorted(snapshots),
    )


def _validate_dialogue_coverage(
    prepared: PreparedEpisode,
    segments: list[GenerationSegment],
    issues: list[GenerationReadinessIssue],
) -> None:
    source_ids = [
        cue.cue_id
        for frame in prepared.storyboard_frame_drafts
        for cue in frame.dialogue_cues
    ]
    assigned = [
        dialogue.source_dialogue_id
        for segment in segments
        for dialogue in segment.dialogue_slices
    ]
    missing = sorted(set(source_ids) - set(assigned))
    duplicated = sorted(item for item in set(assigned) if assigned.count(item) > 1)
    if missing:
        issues.append(
            GenerationReadinessIssue(
                code="dialogue_missing",
                scope="planning",
                severity="error",
                message=f"source dialogue was not assigned: {missing}",
            )
        )
    if duplicated:
        issues.append(
            GenerationReadinessIssue(
                code="dialogue_duplicated",
                scope="planning",
                severity="error",
                message=f"source dialogue was assigned more than once: {duplicated}",
            )
        )


def _continuity_group_id(frame: StoryboardFrameDraft, route_id: str) -> str:
    return _stable_id(
        "continuity",
        route_id,
        frame.location_seed_id,
        ",".join(sorted(frame.character_seed_ids)),
        ",".join(sorted(frame.prop_seed_ids)),
    )


def _reference_ids(
    group_id: str,
    segment_id: str,
    characters: list[str],
    location: str,
    props: list[str],
    *,
    requires_first_frame: bool,
) -> list[str]:
    values = [
        f"ref_char_{_subject_token(item)}_{_short_hash(group_id, item)}"
        for item in characters
    ]
    values.append(f"ref_loc_{_subject_token(location)}_{_short_hash(group_id, location)}")
    values.extend(
        f"ref_prop_{_subject_token(item)}_{_short_hash(group_id, item)}"
        for item in props
    )
    if requires_first_frame:
        values.append(f"ref_first_{_short_hash(segment_id, 'first_frame')}")
    return values


def _group_frames(group: list[_AtomicUnit]) -> int:
    return sum(item.frame_count for item in group)


def _seconds(frames: int, fps: int) -> Decimal:
    return (Decimal(frames) / Decimal(fps)).quantize(Decimal("0.001"))


def _digest_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _stable_id(prefix: str, *parts: str) -> str:
    material = "|".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(material).hexdigest()[:16]}"


def _short_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10]


def _subject_token(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())[:24] or "subject"
