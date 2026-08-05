"""Deterministically select an executable single-shot Canary segment."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    GenerationPlanEpisode,
    GenerationReadinessIssue,
    GenerationSegment,
)


SUPERSEDED_SINGLE_SHOT_ISSUES = frozenset(
    {
        "route_multi_shot_not_migrated",
        "insufficient_segmentation_evidence",
    }
)


class CandidateSelectionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class RejectedCandidate(CandidateSelectionModel):
    segment_id: str = Field(min_length=1)
    reason_codes: list[str] = Field(min_length=1)
    messages: list[str] = Field(min_length=1)


class CandidateSelectionDecision(CandidateSelectionModel):
    selection_policy_id: Literal["wan-single-shot-canary-v1"] = (
        "wan-single-shot-canary-v1"
    )
    selected_segment_id: str | None = None
    eligible_segment_ids: list[str] = Field(default_factory=list)
    rejected_segments: list[RejectedCandidate] = Field(default_factory=list)

    @property
    def selected(self) -> bool:
        return self.selected_segment_id is not None


class CandidateSelectionError(ValueError):
    """No segment satisfies the paid Canary contract."""


def readiness_issue_blocks_segment(
    issue: GenerationReadinessIssue,
    segment: GenerationSegment,
) -> bool:
    """Return whether a plan-level issue still applies after segment hard checks.

    Segment-scoped issues never leak to sibling segments that share a source shot.
    A complete single EditorialShot can also be used as a bounded Canary without
    inventing an internal action boundary; the full-plan error remains preserved in
    the GenerationPlan artifact and still blocks full-episode execution.
    """
    if issue.segment_id is not None:
        applies = issue.segment_id == segment.segment_id
    else:
        applies = (
            issue.source_shot_id is not None
            and issue.source_shot_id in segment.parent_shot_ids
        )
    if not applies:
        return False
    if (
        issue.code in SUPERSEDED_SINGLE_SHOT_ISSUES
        and len(segment.editorial_shots) == 1
    ):
        return False
    return True


def _segment_rejections(
    plan: GenerationPlanEpisode,
    segment: GenerationSegment,
    *,
    provider_clip_seconds: int,
) -> tuple[list[str], list[str]]:
    codes: list[str] = []
    messages: list[str] = []

    def reject(code: str, message: str) -> None:
        codes.append(code)
        messages.append(message)

    if segment.provider_route_id != "wan/i2v":
        reject(
            "unsupported_route",
            f"route {segment.provider_route_id} is not the current paid Canary route",
        )
    if len(segment.parent_shot_ids) != 1:
        reject(
            "multiple_parent_shots",
            "the current Canary requires exactly one source shot",
        )
    if len(segment.editorial_shots) != 1:
        reject(
            "provider_multi_shot_not_supported",
            "Wan Canary requires exactly one EditorialShot per provider generation",
        )
    if segment.audio_strategy not in {"external_audio_post", "silent"}:
        reject(
            "audio_strategy_not_migrated",
            f"audio strategy {segment.audio_strategy} is not migrated to Wan Canary",
        )
    if segment.requested_duration_seconds > provider_clip_seconds:
        reject(
            "provider_duration_exceeded",
            f"requested {segment.requested_duration_seconds}s exceeds provider limit "
            f"{provider_clip_seconds}s",
        )

    provider_frames = segment.requested_duration_seconds * segment.timeline_fps
    trim_valid = (
        segment.used_start_frame >= 0
        and segment.used_end_frame <= provider_frames
        and segment.used_end_frame > segment.used_start_frame
        and segment.used_end_frame - segment.used_start_frame
        == segment.editorial_frame_count
    )
    if not trim_valid:
        reject(
            "invalid_trim_window",
            "the editorial interval cannot be represented by the provider clip",
        )

    for issue in plan.readiness_report.errors:
        if readiness_issue_blocks_segment(issue, segment):
            reject(
                f"readiness_{issue.code}",
                issue.message,
            )

    return codes, messages


def select_safe_canary_candidate(
    plan: GenerationPlanEpisode,
    *,
    provider_clip_seconds: int,
) -> CandidateSelectionDecision:
    """Return one stable Wan single-shot candidate or an auditable rejection set."""
    if provider_clip_seconds <= 0:
        raise ValueError("provider_clip_seconds must be greater than zero")

    eligible: list[GenerationSegment] = []
    rejected: list[RejectedCandidate] = []
    for segment in plan.segments:
        codes, messages = _segment_rejections(
            plan,
            segment,
            provider_clip_seconds=provider_clip_seconds,
        )
        if codes:
            rejected.append(
                RejectedCandidate(
                    segment_id=segment.segment_id,
                    reason_codes=codes,
                    messages=messages,
                )
            )
        else:
            eligible.append(segment)

    ordered = sorted(
        eligible,
        key=lambda item: (
            item.requested_duration_seconds,
            item.order,
            item.segment_id,
        ),
    )
    return CandidateSelectionDecision(
        selected_segment_id=ordered[0].segment_id if ordered else None,
        eligible_segment_ids=[item.segment_id for item in ordered],
        rejected_segments=rejected,
    )


def require_safe_canary_candidate(
    plan: GenerationPlanEpisode,
    *,
    provider_clip_seconds: int,
) -> tuple[GenerationSegment, CandidateSelectionDecision]:
    decision = select_safe_canary_candidate(
        plan,
        provider_clip_seconds=provider_clip_seconds,
    )
    if decision.selected_segment_id is None:
        summary = "; ".join(
            f"{item.segment_id}: {', '.join(item.reason_codes)}"
            for item in decision.rejected_segments
        )
        raise CandidateSelectionError(
            "no safe Wan single-shot Canary segment is available"
            + (f": {summary}" if summary else "")
        )
    segment = next(
        item
        for item in plan.segments
        if item.segment_id == decision.selected_segment_id
    )
    return segment, decision
