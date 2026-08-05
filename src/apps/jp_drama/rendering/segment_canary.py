"""Bridge one PR11 GenerationSegment into the proven PR8 canary path."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..preparation.models import DialogueDraft, PreparedEpisode
from .canary import select_canary_shot


SEGMENT_CANARY_PROTOCOL = "generation-segment-canary-v1"


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
    allow_experimental_multi_shot: bool = False,
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
    if segment.audio_strategy == "native_av":
        raise SegmentCanaryError(
            "wan/i2v native audio is not migrated; use external_audio_post or silent"
        )
    if len(segment.editorial_shots) > 1 and not allow_experimental_multi_shot:
        raise SegmentCanaryError(
            "segment contains multiple editorial shots; pass "
            "--allow-experimental-multi-shot only for an explicitly approved Canary"
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


def materialize_generation_segment_canary(
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    segment_id: str,
    *,
    provider_clip_seconds: int | None = None,
    allow_experimental_multi_shot: bool = False,
) -> PreparedEpisode:
    """Create a deterministic one-segment PreparedEpisode for the PR8 executor.

    Provider request duration is preserved. Editorial trim handles remain visible in
    the segment report and are not silently removed by this bridge.
    """
    segment = find_generation_segment(plan, segment_id)
    validate_segment_canary_contract(
        prepared,
        plan,
        segment,
        provider_clip_seconds=provider_clip_seconds,
        allow_experimental_multi_shot=allow_experimental_multi_shot,
    )

    parent_shot_id = segment.parent_shot_ids[0]
    selected = select_canary_shot(prepared, parent_shot_id)
    frame = selected.storyboard_frame_drafts[0]
    intent = selected.render_intents[0]

    task_types = {node.task_type for node in selected.render_graph.nodes}
    if segment.audio_strategy == "external_audio_post":
        if "generate_video" not in task_types:
            raise SegmentCanaryError(
                "source render graph has no generate_video task for external-audio segment"
            )
        if segment.dialogue_slices and "generate_tts" not in task_types:
            raise SegmentCanaryError(
                "source render graph has dialogue but no generate_tts task"
            )
    elif segment.audio_strategy == "silent":
        if not ({"generate_video", "generate_image"} & task_types):
            raise SegmentCanaryError("source render graph has no visual generation task")

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

    first_editorial = segment.editorial_shots[0]
    new_intent_id = f"{intent.intent_id}__{segment.segment_id}"
    task_id_map = {
        node.task_id: f"{node.task_id}__{segment.segment_id}"
        for node in selected.render_graph.nodes
    }
    selected.render_graph.nodes = [
        node.model_copy(
            update={
                "task_id": task_id_map[node.task_id],
                "shot_id": segment.segment_id,
                "depends_on": [task_id_map[item] for item in node.depends_on],
            }
        )
        for node in selected.render_graph.nodes
    ]
    selected.render_intents = [
        intent.model_copy(
            update={
                "intent_id": new_intent_id,
                "shot_id": segment.segment_id,
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
            "shot_size": first_editorial.framing,
            "movement": first_editorial.camera_movement,
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

    selected.character_seeds = [
        item for item in selected.character_seeds if item.seed_id in set(segment.character_ids)
    ]
    selected.location_seeds = [
        item for item in selected.location_seeds if item.seed_id == segment.location_id
    ]
    selected.prop_seeds = [
        item for item in selected.prop_seeds if item.seed_id in set(segment.prop_ids)
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
            "allow_experimental_multi_shot": allow_experimental_multi_shot,
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

    selected.mapping_trace.shots = [
        item.model_copy(
            update={
                "source_id": (
                    segment.segment_id
                    if item.source_id == parent_shot_id
                    else item.source_id
                ),
                "target_id": (
                    frame.frame_id if item.target_id == parent_shot_id else item.target_id
                ),
            }
        )
        for item in selected.mapping_trace.shots
    ]
    selected.readiness_report.episode_id = selected.episode_id
    selected.readiness_report.duration_seconds = float(
        segment.requested_duration_seconds
    )
    selected.readiness_report.shot_count = 1
    selected.readiness_report.character_count = len(selected.character_seeds)
    selected.readiness_report.location_count = len(selected.location_seeds)
    selected.readiness_report.prop_count = len(selected.prop_seeds)
    selected.readiness_report.resolved_render_intents = 1
    selected.readiness_report.total_render_intents = 1
    selected.readiness_report.estimated_total = (
        selected.budget_snapshot.estimated_total
    )
    selected.readiness_report.generation_ready = (
        selected.readiness_report.generation_ready
        and selected.budget_snapshot.within_budget
        and not selected.readiness_report.errors
    )
    return PreparedEpisode.model_validate(selected.model_dump(mode="python"))
