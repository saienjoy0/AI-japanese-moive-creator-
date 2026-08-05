"""Bridge one PR11 GenerationSegment into the proven PR8 canary path."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..preparation.models import (
    DialogueDraft,
    MappingEntry,
    MappingTrace,
    PreparedEpisode,
    RenderGraph,
    RenderTaskNode,
)
from .canary import select_canary_shot


SEGMENT_CANARY_PROTOCOL = "generation-segment-canary-v2"


class SegmentCanaryError(ValueError):
    """A generation segment cannot be represented by the current Wan canary."""


def prepared_content_digest(prepared: PreparedEpisode) -> str:
    """Return the digest PR11 stores for its source PreparedEpisode."""
    canonical = prepared.to_canonical_json(indent=None).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def find_generation_segment(
    plan: GenerationPlanEpisode,
    segment_id: str,
) -> GenerationSegment:
    matches = [item for item in plan.segments if item.segment_id == segment_id]
    if len(matches) != 1:
        raise SegmentCanaryError(f"unknown or duplicate generation segment: {segment_id}")
    return matches[0]


def validate_segment_canary_contract(
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    segment: GenerationSegment,
    *,
    provider_clip_seconds: int | None = None,
) -> None:
    """Fail before any paid submission when the selected segment is not executable."""
    digest = prepared_content_digest(prepared)
    if plan.source_prepared_episode_digest != digest:
        raise SegmentCanaryError(
            "generation plan does not belong to the supplied PreparedEpisode"
        )
    if segment.provider_route_id != "wan/i2v":
        raise SegmentCanaryError(
            f"current paid canary supports only wan/i2v, not {segment.provider_route_id}"
        )
    if len(segment.parent_shot_ids) != 1:
        raise SegmentCanaryError(
            "current canary requires exactly one parent source shot per segment"
        )
    if len(segment.editorial_shots) != 1:
        raise SegmentCanaryError(
            "Wan Canary requires exactly one EditorialShot per provider generation"
        )
    if segment.audio_strategy == "native_av":
        raise SegmentCanaryError(
            "wan/i2v native audio is not migrated; use external_audio_post or silent"
        )

    provider_frame_count = segment.requested_duration_seconds * segment.timeline_fps
    if (
        segment.used_start_frame < 0
        or segment.used_end_frame > provider_frame_count
        or segment.used_end_frame <= segment.used_start_frame
        or segment.used_end_frame - segment.used_start_frame
        != segment.editorial_frame_count
    ):
        raise SegmentCanaryError(
            "segment trim window is invalid for the requested provider clip; refusing "
            "to execute an oversized or truncated editorial interval"
        )
    if provider_clip_seconds is not None:
        if provider_clip_seconds <= 0:
            raise SegmentCanaryError("provider_clip_seconds must be greater than zero")
        if segment.requested_duration_seconds > provider_clip_seconds:
            raise SegmentCanaryError(
                f"segment requests {segment.requested_duration_seconds}s but the current "
                f"provider configuration allows {provider_clip_seconds}s; refusing to trim "
                "the narrative segment silently"
            )

    for issue in plan.readiness_report.errors:
        applies_to_segment = issue.segment_id == segment.segment_id
        applies_to_source_shot = (
            issue.source_shot_id is not None
            and issue.source_shot_id in segment.parent_shot_ids
        )
        if applies_to_segment or applies_to_source_shot:
            raise SegmentCanaryError(
                f"selected segment has unresolved readiness error {issue.code}: "
                f"{issue.message}"
            )


def _rebuild_mapping_trace(
    selected: PreparedEpisode,
    *,
    segment: GenerationSegment,
    frame_id: str,
    adapted_beat_id: str,
) -> MappingTrace:
    adapted = [
        item
        for item in selected.mapping_trace.adapted_beats
        if item.target_id == adapted_beat_id or item.source_id == adapted_beat_id
    ]
    if not adapted:
        adapted = [
            MappingEntry(
                source_id=adapted_beat_id,
                target_id=adapted_beat_id,
            )
        ]
    return MappingTrace(
        characters=[
            MappingEntry(
                source_id=item.source_character_id,
                target_id=item.seed_id,
            )
            for item in selected.character_seeds
        ],
        locations=[
            MappingEntry(
                source_id=item.source_location_id,
                target_id=item.seed_id,
            )
            for item in selected.location_seeds
        ],
        props=[
            MappingEntry(
                source_id=item.source_prop_id,
                target_id=item.seed_id,
            )
            for item in selected.prop_seeds
        ],
        shots=[
            MappingEntry(
                source_id=segment.segment_id,
                target_id=frame_id,
            )
        ],
        adapted_beats=adapted,
        mapping_coverage=1.0,
    )


def _node_by_type(selected: PreparedEpisode, task_type: str) -> RenderTaskNode:
    matches = [
        node for node in selected.render_graph.nodes if node.task_type == task_type
    ]
    if len(matches) != 1:
        raise SegmentCanaryError(
            f"source render graph has no unique {task_type} task"
        )
    return matches[0]


def _rebuild_render_graph(
    selected: PreparedEpisode,
    segment: GenerationSegment,
) -> tuple[RenderGraph, list[str]]:
    """Rebuild tasks so a silent slice cannot inherit paid TTS from its parent shot."""
    visual = _node_by_type(selected, "generate_video")
    visual_id = f"{visual.task_id}__{segment.segment_id}"
    visual_node = visual.model_copy(
        update={
            "task_id": visual_id,
            "shot_id": segment.segment_id,
            "depends_on": [],
        }
    )

    if segment.audio_strategy == "silent":
        source_final = _node_by_type(selected, "finalize_shot")
        final_node = source_final.model_copy(
            update={
                "task_id": f"{source_final.task_id}__{segment.segment_id}",
                "shot_id": segment.segment_id,
                "depends_on": [visual_id],
            }
        )
        return RenderGraph(nodes=[visual_node, final_node]), [
            "generate_video",
            "finalize_shot",
        ]

    required_types = [
        "generate_video",
        "generate_tts",
        "generate_subtitles",
        "mux_audio_video",
        "finalize_shot",
    ]
    originals = {task_type: _node_by_type(selected, task_type) for task_type in required_types}
    task_id_map = {
        node.task_id: f"{node.task_id}__{segment.segment_id}"
        for node in originals.values()
    }
    rebuilt = [
        node.model_copy(
            update={
                "task_id": task_id_map[node.task_id],
                "shot_id": segment.segment_id,
                "depends_on": [task_id_map[item] for item in node.depends_on],
            }
        )
        for task_type in required_types
        for node in [originals[task_type]]
    ]
    return RenderGraph(nodes=rebuilt), required_types


def materialize_generation_segment_canary(
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    segment_id: str,
    *,
    provider_clip_seconds: int | None = None,
) -> PreparedEpisode:
    """Create a deterministic one-segment PreparedEpisode for the PR8 executor."""
    segment = find_generation_segment(plan, segment_id)
    validate_segment_canary_contract(
        prepared,
        plan,
        segment,
        provider_clip_seconds=provider_clip_seconds,
    )

    parent_shot_id = segment.parent_shot_ids[0]
    selected = select_canary_shot(prepared, parent_shot_id)
    frame = selected.storyboard_frame_drafts[0]
    intent = selected.render_intents[0]

    task_types = {node.task_type for node in selected.render_graph.nodes}
    if "generate_video" not in task_types:
        raise SegmentCanaryError(
            "source render graph has no generate_video task for wan/i2v"
        )
    if segment.audio_strategy == "external_audio_post":
        if segment.dialogue_slices and "generate_tts" not in task_types:
            raise SegmentCanaryError(
                "source render graph has dialogue but no generate_tts task"
            )

    fps = segment.timeline_fps
    handle_offset_seconds = segment.used_start_frame / fps
    dialogue = [
        DialogueDraft(
            cue_id=item.source_dialogue_id,
            speaker_character_id=item.speaker_character_id,
            text=item.text,
            start_seconds=handle_offset_seconds + item.start_frame / fps,
            end_seconds=handle_offset_seconds + item.end_frame / fps,
            emotion=None,
            delivery=None,
        )
        for item in segment.dialogue_slices
    ]

    editorial = segment.editorial_shots[0]
    new_intent_id = f"{intent.intent_id}__{segment.segment_id}"
    rebuilt_graph, rebuilt_tasks = _rebuild_render_graph(selected, segment)
    selected.render_graph = rebuilt_graph
    selected.render_intents = [
        intent.model_copy(
            update={
                "intent_id": new_intent_id,
                "shot_id": segment.segment_id,
                "tasks": rebuilt_tasks,
            }
        )
    ]

    frame.frame_id = f"frame_{segment.segment_id}"
    frame.source_shot_id = segment.segment_id
    frame.order = 1
    frame.duration_seconds = float(segment.requested_duration_seconds)
    frame.character_seed_ids = list(segment.character_ids)
    frame.location_seed_id = segment.location_id
    frame.prop_seed_ids = list(segment.prop_ids)
    frame.action = "\n".join(
        part
        for part in (
            segment.prompt_bundle.timed_shot_prompt,
            segment.prompt_bundle.motion_prompt,
        )
        if part
    )
    frame.visual_description = segment.prompt_bundle.visual_prompt
    frame.camera = frame.camera.model_copy(
        update={
            "shot_size": editorial.framing,
            "movement": editorial.camera_movement,
        }
    )
    frame.dialogue_cues = dialogue
    frame.audio = frame.audio.model_copy(
        update={
            "ambience": segment.prompt_bundle.audio_prompt or frame.audio.ambience,
            "generated_native_audio": False,
        }
    )
    frame.render_intent_id = new_intent_id

    character_ids = set(segment.character_ids)
    prop_ids = set(segment.prop_ids)
    selected.character_seeds = [
        item for item in selected.character_seeds if item.seed_id in character_ids
    ]
    selected.location_seeds = [
        item for item in selected.location_seeds if item.seed_id == segment.location_id
    ]
    selected.prop_seeds = [
        item for item in selected.prop_seeds if item.seed_id in prop_ids
    ]

    selected.budget_snapshot.shot_items = [
        item.model_copy(update={"shot_id": segment.segment_id})
        for item in selected.budget_snapshot.shot_items
    ]
    subtotal = sum(
        (item.total_cost for item in selected.budget_snapshot.shot_items),
        start=Decimal("0"),
    )
    contingency = subtotal * selected.budget_snapshot.contingency_rate
    selected.budget_snapshot.subtotal = subtotal
    selected.budget_snapshot.contingency = contingency
    selected.budget_snapshot.estimated_total = subtotal + contingency
    selected.budget_snapshot.within_budget = (
        selected.budget_snapshot.estimated_total
        <= selected.budget_snapshot.budget_limit
    )

    identity_payload = json.dumps(
        {
            "protocol": SEGMENT_CANARY_PROTOCOL,
            "prepared_source_digest": prepared.source_digest,
            "generation_plan_digest": plan.content_digest,
            "segment_id": segment.segment_id,
            "provider_route_id": segment.provider_route_id,
            "requested_duration_seconds": segment.requested_duration_seconds,
            "audio_strategy": segment.audio_strategy,
            "tasks": rebuilt_tasks,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    selected.source_digest = f"sha256:{hashlib.sha256(identity_payload).hexdigest()}"
    selected.episode_id = f"{prepared.episode_id}_segment_{segment.segment_id}"
    selected.project_draft.project_id = (
        f"{prepared.project_draft.project_id}_segment_{segment.segment_id}"
    )
    selected.project_draft.title = (
        f"{prepared.project_draft.title} [Segment Canary {segment.segment_id}]"
    )
    selected.project_draft.target_duration_seconds = float(
        segment.requested_duration_seconds
    )
    selected.storyboard_frame_drafts = [frame]
    selected.mapping_trace = _rebuild_mapping_trace(
        selected,
        segment=segment,
        frame_id=frame.frame_id,
        adapted_beat_id=frame.adapted_beat_id,
    )

    selected.readiness_report.episode_id = selected.episode_id
    selected.readiness_report.duration_seconds = float(
        segment.requested_duration_seconds
    )
    selected.readiness_report.shot_count = 1
    selected.readiness_report.character_count = len(selected.character_seeds)
    selected.readiness_report.location_count = len(selected.location_seeds)
    selected.readiness_report.prop_count = len(selected.prop_seeds)
    selected.readiness_report.mapping_coverage = 1.0
    selected.readiness_report.resolved_render_intents = 1
    selected.readiness_report.total_render_intents = 1
    selected.readiness_report.estimated_total = (
        selected.budget_snapshot.estimated_total
    )
    selected.readiness_report.errors = [
        issue
        for issue in selected.readiness_report.errors
        if issue.shot_id in {None, parent_shot_id}
    ]
    selected.readiness_report.warnings = [
        issue
        for issue in selected.readiness_report.warnings
        if issue.shot_id in {None, parent_shot_id}
    ]
    selected.readiness_report.generation_ready = (
        selected.readiness_report.generation_ready
        and selected.budget_snapshot.within_budget
        and not selected.readiness_report.errors
    )
    return PreparedEpisode.model_validate(selected.model_dump(mode="python"))
