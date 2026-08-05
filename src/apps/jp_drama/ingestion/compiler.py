"""Deterministic compiler from StructuredScriptDraft to EpisodePackage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from itertools import cycle

from ..domain import (
    AdaptedCharacter,
    AudioPlan,
    BeatMapping,
    BeatSheet,
    BeatType,
    CameraPlan,
    CostPlan,
    DialogueCue,
    DramaticBeat,
    EpisodePackage,
    EpisodePlan,
    JapaneseAdaptation,
    LocationPlan,
    OriginalityAssessment,
    ProductionStatus,
    RenderStrategy,
    RightsStatus,
    Shot,
    ShotCostEstimate,
    ShotPlan,
    SimilarityRisk,
    SourceKind,
    SourceRecord,
)
from .models import (
    ScriptActionBeatDraft,
    ScriptBeatDraft,
    ScriptDialogueDraft,
    StructuredScriptDraft,
)


@dataclass(frozen=True)
class CompilationOptions:
    rights_status: RightsStatus = RightsStatus.UNKNOWN
    target_audience: str = "日本の縦型ショートドラマ視聴者"
    provider: str = "provider-a"
    model: str = "model-v1"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    usage_constraints: tuple[str, ...] = (
        "第三者作品の固有名詞・特徴的台詞・画面構成を無断流用しない",
    )


@dataclass(frozen=True)
class _ActionShotSource:
    beat: ScriptBeatDraft
    action: ScriptActionBeatDraft


def compile_structured_script(
    draft: StructuredScriptDraft,
    *,
    normalized_script: str,
    options: CompilationOptions | None = None,
) -> EpisodePackage:
    selected = options or CompilationOptions()
    digest = _digest_text(normalized_script)
    suffix = digest.split(":", 1)[1][:12]

    source_id = f"source_{suffix}"
    beat_sheet_id = f"beats_{suffix}"
    adaptation_id = f"adapt_{suffix}"
    episode_id = f"episode_{suffix}"
    package_id = f"pkg_{suffix}"
    series_id = f"series_{suffix}"

    beat_types = _beat_types(len(draft.beats))
    dramatic_beats = [
        DramaticBeat(
            beat_id=beat.beat_id,
            order=beat.order,
            beat_type=beat_types[index],
            summary=beat.summary,
            source_evidence=beat.action,
            must_transform=False,
        )
        for index, beat in enumerate(draft.beats)
    ]

    characters = [
        AdaptedCharacter(
            character_id=item.character_id,
            display_name=item.name,
            dramatic_role=item.description,
            occupation=item.occupation,
            speech_style=item.speech_style,
            visual_notes="、".join(item.visual_traits) or item.description,
        )
        for item in draft.characters
    ]
    locations = [
        LocationPlan(
            location_id=item.scene_id,
            name=item.location_name,
            description=item.description,
            time_of_day=item.time_of_day,
            continuity_rules=item.continuity_rules,
        )
        for item in draft.scenes
    ]
    beat_mappings = [
        BeatMapping(
            source_beat_id=beat.beat_id,
            adapted_beat_id=f"adapted_{beat.beat_id}",
            adapted_summary=beat.summary,
            transformation_notes=(
                "ユーザー提供の日本語台本を、意味境界付きActionBeat、動作、台詞へ構造化"
            ),
        )
        for beat in draft.beats
    ]

    action_sources = [
        _ActionShotSource(beat=beat, action=action)
        for beat in draft.beats
        for action in beat.action_beats
    ]
    durations = _distribute_weighted_duration(
        draft.target_duration_seconds,
        [item.action.estimated_duration_seconds for item in action_sources],
    )
    shots = [
        _build_shot(source, duration, order)
        for order, (source, duration) in enumerate(
            zip(action_sources, durations),
            start=1,
        )
    ]

    estimates = []
    for shot in shots:
        if shot.render_strategy is RenderStrategy.STILL_MOTION:
            primary = Decimal("250")
        else:
            primary = Decimal("400")
        estimates.append(
            ShotCostEstimate(
                shot_id=shot.shot_id,
                render_strategy=shot.render_strategy,
                provider=selected.provider,
                model=selected.model,
                estimated_primary_cost=primary,
                reserved_retry_cost=Decimal("100"),
                max_attempts=2,
                fallback_strategy=(
                    RenderStrategy.STILL_MOTION
                    if shot.render_strategy is RenderStrategy.VIDEO_PLUS_TTS
                    else None
                ),
            )
        )

    subtotal = sum(
        (
            item.estimated_primary_cost + item.reserved_retry_cost
            for item in estimates
        ),
        start=Decimal("0"),
    )
    budget_limit = (subtotal * Decimal("1.20")).quantize(Decimal("0.01"))

    opening_hook = _hook_text(draft.beats[0])
    closing_hook = _hook_text(draft.beats[-1])

    return EpisodePackage(
        package_id=package_id,
        created_at=selected.created_at,
        source=SourceRecord(
            source_id=source_id,
            kind=SourceKind.MANUAL_TEXT,
            original_language="ja-JP",
            title=draft.title,
            synopsis=draft.synopsis,
            transcript=normalized_script,
            captured_at=selected.created_at,
            rights_status=selected.rights_status,
            provenance_notes="PR15 semantic ActionBeat Japanese script ingestion",
            usage_constraints=list(selected.usage_constraints),
        ),
        beat_sheet=BeatSheet(
            beat_sheet_id=beat_sheet_id,
            source_id=source_id,
            core_premise=draft.synopsis,
            audience_hook=opening_hook,
            emotional_promise=closing_hook,
            beats=dramatic_beats,
            extracted_character_roles=[
                f"{item.name}: {item.description}" for item in draft.characters
            ],
            status=ProductionStatus.ANALYZED,
        ),
        adaptation=JapaneseAdaptation(
            adaptation_id=adaptation_id,
            source_id=source_id,
            beat_sheet_id=beat_sheet_id,
            working_title=draft.title,
            logline=draft.synopsis,
            target_audience=selected.target_audience,
            setting=" / ".join(item.location_name for item in draft.scenes),
            characters=characters,
            beat_mappings=beat_mappings,
            originality=OriginalityAssessment(
                similarity_risk=SimilarityRisk.LOW,
                retained_elements=["ユーザー提供台本の物語構造と台詞"],
                transformed_elements=[
                    "場面の構造化",
                    "意味境界付きActionBeat分割",
                    "生成向け視覚記述",
                    "台詞タイミング",
                ],
                prohibited_copy_elements=[
                    "権利未確認の第三者作品に固有の表現"
                ],
                approved=True,
            ),
            restrictions=list(selected.usage_constraints),
            status=ProductionStatus.ADAPTED,
        ),
        episode=EpisodePlan(
            episode_id=episode_id,
            adaptation_id=adaptation_id,
            series_id=series_id,
            episode_number=1,
            title=draft.title,
            target_duration_seconds=draft.target_duration_seconds,
            aspect_ratio="9:16",
            fps=30,
            narrative_goal=draft.synopsis,
            opening_hook_text=opening_hook,
            closing_hook_text=closing_hook,
            character_ids=[item.character_id for item in draft.characters],
            locations=locations,
            props=[],
            status=ProductionStatus.PLANNED,
        ),
        shot_plan=ShotPlan(
            shot_plan_id=f"shots_{suffix}",
            episode_id=episode_id,
            target_duration_seconds=draft.target_duration_seconds,
            shots=shots,
            duration_tolerance_seconds=0.01,
        ),
        cost_plan=CostPlan(
            cost_plan_id=f"cost_{suffix}",
            episode_id=episode_id,
            currency="JPY",
            budget_limit=budget_limit,
            contingency_rate=Decimal("0.10"),
            hard_stop=True,
            shot_estimates=estimates,
        ),
    )


def _digest_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _beat_types(count: int) -> list[BeatType]:
    if count < 3:
        raise ValueError("at least three beats are required")
    middle_cycle = cycle(
        [
            BeatType.SETUP,
            BeatType.ESCALATION,
            BeatType.REVEAL,
            BeatType.REVERSAL,
        ]
    )
    result = [BeatType.HOOK]
    result.extend(next(middle_cycle) for _ in range(count - 2))
    result.append(BeatType.CLIFFHANGER)
    return result


def _distribute_weighted_duration(
    total_seconds: float,
    weights: list[float],
) -> list[float]:
    if not weights or any(weight <= 0 for weight in weights):
        raise ValueError("ActionBeat duration weights must be positive")
    total_ms = round(total_seconds * 1000)
    decimal_weights = [Decimal(str(item)) for item in weights]
    weight_sum = sum(decimal_weights, start=Decimal("0"))
    exact = [Decimal(total_ms) * weight / weight_sum for weight in decimal_weights]
    allocated = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in exact]
    remainder = total_ms - sum(allocated)
    fractional_order = sorted(
        range(len(exact)),
        key=lambda index: (-(exact[index] - allocated[index]), index),
    )
    for index in fractional_order[:remainder]:
        allocated[index] += 1
    durations = [milliseconds / 1000 for milliseconds in allocated]
    if any(duration < 0.5 or duration > 15.0 for duration in durations):
        raise ValueError(
            "scaled ActionBeat duration must stay between 0.5 and 15 seconds; "
            "add or rebalance semantic ActionBeats"
        )
    if round(sum(durations) * 1000) != total_ms:
        raise ValueError("ActionBeat duration allocation must preserve episode duration")
    return durations


def _build_shot(
    source: _ActionShotSource,
    duration: float,
    order: int,
) -> Shot:
    beat = source.beat
    action = source.action
    assigned_dialogue = [
        beat.dialogue[index - 1]
        for index in action.dialogue_indexes
    ]
    dialogue = _dialogue_cues(
        assigned_dialogue,
        duration,
        beat_order=beat.order,
        action_order=action.order,
    )
    strategy = (
        RenderStrategy.VIDEO_PLUS_TTS
        if dialogue
        else RenderStrategy.STILL_MOTION
    )
    shot_sizes = ["full", "medium", "medium_close_up", "close_up"]
    movements = ["static", "push_in", "pan_right", "follow"]
    camera_note = action.camera_hint or beat.camera_hint

    return Shot(
        shot_id=f"shot_{beat.order:02d}_{action.order:02d}",
        order=order,
        adapted_beat_id=f"adapted_{beat.beat_id}",
        duration_seconds=duration,
        location_id=beat.scene_id,
        character_ids=action.character_ids,
        prop_ids=[],
        action=action.visual_action,
        visual_description=(
            f"{beat.summary}。{action.summary}。{action.visual_action}"
            + (f" カメラ意図: {camera_note}" if camera_note else "")
        ),
        dialogue=dialogue,
        camera=CameraPlan(
            shot_size=shot_sizes[(order - 1) % len(shot_sizes)],
            angle="eye_level",
            movement=movements[(order - 1) % len(movements)],
            speed="slow" if action.boundary_after == "reaction" else "normal",
        ),
        audio=AudioPlan(
            ambience=beat.ambience,
            sound_effects=beat.sound_effects,
            bgm_cue="ショートドラマの緊張と展開に合わせる",
            generated_native_audio=False,
        ),
        render_strategy=strategy,
        continuity_notes=[
            "同一人物の顔・髪型・衣装を前後ショットで維持する",
            "同一場所のレイアウトと照明方向を維持する",
            *action.continuity_effects,
        ],
        generation_notes=(
            f"ActionBeat={action.action_beat_id}; "
            f"boundary_before={action.boundary_before}; "
            f"boundary_after={action.boundary_after}; "
            f"confidence={action.confidence:.3f}"
        ),
    )


def _dialogue_cues(
    dialogue: list[ScriptDialogueDraft],
    duration: float,
    *,
    beat_order: int,
    action_order: int,
) -> list[DialogueCue]:
    if not dialogue:
        return []
    usable_start = min(0.8, max(0.1, duration * 0.15))
    usable_end = max(usable_start + 0.5, duration - min(0.4, duration * 0.1))
    segment = (usable_end - usable_start) / len(dialogue)
    cues = []
    for index, item in enumerate(dialogue):
        start = usable_start + segment * index
        cue_duration = min(4.0, max(0.5, segment * 0.75))
        end = min(usable_end, start + cue_duration)
        if end <= start:
            end = min(duration, start + 0.5)
        cues.append(
            DialogueCue(
                cue_id=(
                    f"cue_{beat_order:02d}_{action_order:02d}_{index + 1:02d}"
                ),
                speaker_character_id=item.speaker_character_id,
                text=item.text,
                start_seconds=round(start, 3),
                end_seconds=round(end, 3),
                emotion=item.emotion,
                delivery=item.delivery,
            )
        )
    return cues


def _hook_text(beat: ScriptBeatDraft) -> str:
    if beat.dialogue:
        return beat.dialogue[-1].text
    return beat.summary
