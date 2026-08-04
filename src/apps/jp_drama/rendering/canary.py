"""Helpers for producing an isolated one-shot PreparedEpisode canary."""

from __future__ import annotations

import hashlib
from decimal import Decimal

from ..preparation.models import PreparedEpisode


def select_canary_shot(
    prepared: PreparedEpisode,
    shot_id: str,
    *,
    target_duration_seconds: float | None = None,
) -> PreparedEpisode:
    """Return an isolated one-shot package with independent ownership identity."""
    selected = prepared.model_copy(deep=True)
    frames = [frame for frame in selected.storyboard_frame_drafts if frame.source_shot_id == shot_id]
    if len(frames) != 1:
        raise ValueError(f"unknown or duplicate shot ID: {shot_id}")
    frame = frames[0]
    intents = [intent for intent in selected.render_intents if intent.shot_id == shot_id]
    if len(intents) != 1:
        raise ValueError(f"shot has no unique render intent: {shot_id}")
    nodes = [node for node in selected.render_graph.nodes if node.shot_id == shot_id]
    if not nodes:
        raise ValueError(f"shot has no render tasks: {shot_id}")

    if target_duration_seconds is not None:
        if target_duration_seconds <= 0:
            raise ValueError("target_duration_seconds must be greater than zero")
        canary_duration = min(frame.duration_seconds, target_duration_seconds)
        frame.duration_seconds = canary_duration
        trimmed_cues = []
        for cue in frame.dialogue_cues:
            if cue.start_seconds >= canary_duration:
                continue
            cue.end_seconds = min(cue.end_seconds, canary_duration)
            if cue.end_seconds > cue.start_seconds:
                trimmed_cues.append(cue)
        frame.dialogue_cues = trimmed_cues

    suffix = f"canary_{shot_id}"
    selected.source_digest = "sha256:" + hashlib.sha256(
        f"{prepared.source_digest}|{suffix}|v2|{frame.duration_seconds:.3f}".encode("utf-8")
    ).hexdigest()
    selected.project_draft.project_id = f"{prepared.project_draft.project_id}_{suffix}"
    selected.project_draft.title = f"{prepared.project_draft.title} [Canary {shot_id}]"
    selected.project_draft.target_duration_seconds = frame.duration_seconds
    selected.episode_id = f"{prepared.episode_id}_{suffix}"
    selected.storyboard_frame_drafts = frames
    selected.render_intents = intents
    selected.render_graph.nodes = nodes

    selected.mapping_trace.shots = [
        item
        for item in selected.mapping_trace.shots
        if item.source_id == shot_id or item.target_id in {frame.frame_id, shot_id}
    ]

    selected.budget_snapshot.shot_items = [
        item for item in selected.budget_snapshot.shot_items if item.shot_id == shot_id
    ]
    subtotal = sum(
        (item.total_cost for item in selected.budget_snapshot.shot_items),
        start=Decimal("0"),
    )
    contingency = subtotal * selected.budget_snapshot.contingency_rate
    total = subtotal + contingency
    selected.budget_snapshot.subtotal = subtotal
    selected.budget_snapshot.contingency = contingency
    selected.budget_snapshot.estimated_total = total
    selected.budget_snapshot.within_budget = total <= selected.budget_snapshot.budget_limit

    selected.readiness_report.episode_id = selected.episode_id
    selected.readiness_report.duration_seconds = frame.duration_seconds
    selected.readiness_report.shot_count = 1
    selected.readiness_report.resolved_render_intents = 1
    selected.readiness_report.total_render_intents = 1
    selected.readiness_report.estimated_total = total
    selected.readiness_report.generation_ready = (
        selected.readiness_report.generation_ready
        and selected.budget_snapshot.within_budget
        and not selected.readiness_report.errors
    )
    return selected
