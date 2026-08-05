"""Strict deterministic contracts for adaptive generation segmentation."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


GENERATION_PLAN_SCHEMA_VERSION = "1.0.0"
GENERATION_COMPILER_VERSION = "1.0.0"

ComplexityLevel = Literal["low", "medium", "high", "very_high"]
AudioStrategy = Literal["native_av", "external_audio_post", "silent"]
IssueScope = Literal["structural", "planning", "route", "quality"]
TaskType = Literal[
    "prepare_references",
    "generate_first_frame",
    "generate_video",
    "generate_native_av",
    "generate_tts",
    "generate_subtitles",
    "mux_segment",
    "trim_segment",
    "validate_segment",
    "concat_episode",
    "validate_episode",
]


class GenerationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class DialogueSlice(GenerationModel):
    dialogue_slice_id: str = Field(min_length=1)
    source_dialogue_id: str = Field(min_length=1)
    speaker_character_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    lip_sync_required: bool = True
    can_continue_over_reaction_shot: bool = True

    @model_validator(mode="after")
    def validate_frames(self) -> "DialogueSlice":
        if self.end_frame <= self.start_frame:
            raise ValueError("dialogue end_frame must be greater than start_frame")
        return self


class EditorialShot(GenerationModel):
    editorial_shot_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    order_within_segment: int = Field(ge=1)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    framing: str = Field(min_length=1)
    camera_movement: str = Field(min_length=1)
    visual_action: str = Field(min_length=1)
    emotion: str | None = None
    character_ids: list[str] = Field(default_factory=list)
    active_speaker_id: str | None = None
    dialogue_slice_ids: list[str] = Field(default_factory=list)
    source_beat_ids: list[str] = Field(default_factory=list)
    source_shot_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_frames(self) -> "EditorialShot":
        if self.end_frame <= self.start_frame:
            raise ValueError("editorial shot end_frame must be greater than start_frame")
        return self


class SegmentComplexity(GenerationModel):
    score: int = Field(ge=0)
    level: ComplexityLevel
    character_complexity: int = Field(ge=0)
    action_complexity: int = Field(ge=0)
    dialogue_complexity: int = Field(ge=0)
    camera_complexity: int = Field(ge=0)
    continuity_complexity: int = Field(ge=0)
    object_interaction_complexity: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)


class PromptBundle(GenerationModel):
    narrative_summary: str = Field(min_length=1)
    visual_prompt: str = Field(min_length=1)
    motion_prompt: str = Field(min_length=1)
    camera_prompt: str = Field(min_length=1)
    timed_shot_prompt: str = Field(min_length=1)
    dialogue_prompt: str | None = None
    audio_prompt: str | None = None
    negative_constraints: list[str] = Field(default_factory=list)


class ContinuityContract(GenerationModel):
    continuity_group_id: str = Field(min_length=1)
    character_appearance_locks: dict[str, str] = Field(default_factory=dict)
    location_id: str = Field(min_length=1)
    location_lock: str = Field(min_length=1)
    prop_state_locks: dict[str, str] = Field(default_factory=dict)
    lighting: str | None = None
    time_of_day: str | None = None
    weather: str | None = None
    screen_direction: str | None = None
    reference_asset_ids: list[str] = Field(default_factory=list)


class ReferenceAssetRequirement(GenerationModel):
    asset_id: str = Field(min_length=1)
    role: Literal["character", "location", "prop", "first_frame"]
    subject_id: str = Field(min_length=1)
    continuity_group_id: str = Field(min_length=1)
    required_for_segment_ids: list[str] = Field(min_length=1)
    generation_status: Literal["missing", "available"] = "missing"


class GenerationSegment(GenerationModel):
    segment_id: str = Field(min_length=1)
    order: int = Field(ge=1)
    parent_shot_ids: list[str] = Field(min_length=1)
    continuity_group_id: str = Field(min_length=1)
    provider_route_id: str = Field(min_length=1)
    timeline_fps: int = Field(ge=1)
    editorial_start_frame: int = Field(ge=0)
    editorial_end_frame: int = Field(gt=0)
    editorial_frame_count: int = Field(gt=0)
    editorial_duration_seconds: Decimal = Field(gt=0)
    requested_duration_seconds: int = Field(ge=1)
    used_start_frame: int = Field(ge=0)
    used_end_frame: int = Field(gt=0)
    complexity: SegmentComplexity
    editorial_shots: list[EditorialShot] = Field(min_length=1)
    character_ids: list[str] = Field(default_factory=list)
    location_id: str = Field(min_length=1)
    prop_ids: list[str] = Field(default_factory=list)
    dialogue_slices: list[DialogueSlice] = Field(default_factory=list)
    reference_asset_ids: list[str] = Field(default_factory=list)
    prompt_bundle: PromptBundle
    audio_strategy: AudioStrategy
    transition_in: Literal["cut", "continuation"] = "cut"
    transition_out: Literal["cut", "continuation"] = "cut"

    @model_validator(mode="after")
    def validate_timeline(self) -> "GenerationSegment":
        if self.editorial_end_frame - self.editorial_start_frame != self.editorial_frame_count:
            raise ValueError("segment global frame range must equal editorial_frame_count")
        if self.used_end_frame - self.used_start_frame != self.editorial_frame_count:
            raise ValueError("segment used range must equal editorial_frame_count")
        if self.used_end_frame > self.requested_duration_seconds * self.timeline_fps:
            raise ValueError("segment used range exceeds requested provider duration")
        expected = 0
        for order, shot in enumerate(self.editorial_shots, start=1):
            if shot.segment_id != self.segment_id:
                raise ValueError("editorial shot segment_id must match its segment")
            if shot.order_within_segment != order:
                raise ValueError("editorial shot order must be contiguous")
            if shot.start_frame != expected:
                raise ValueError("editorial shots must be contiguous without gaps")
            expected = shot.end_frame
        if expected != self.editorial_frame_count:
            raise ValueError("editorial shots must cover the complete segment")
        slice_ids = {item.dialogue_slice_id for item in self.dialogue_slices}
        for shot in self.editorial_shots:
            if not set(shot.dialogue_slice_ids) <= slice_ids:
                raise ValueError("editorial shot references an unknown dialogue slice")
        if any(item.end_frame > self.editorial_frame_count for item in self.dialogue_slices):
            raise ValueError("dialogue slices must fit inside the used editorial range")
        return self


class GenerationTaskNode(GenerationModel):
    task_id: str = Field(min_length=1)
    segment_id: str | None = None
    task_type: TaskType
    depends_on: list[str] = Field(default_factory=list)
    external_api_required: bool = False
    provider_route_id: str | None = None


class GenerationRenderGraph(GenerationModel):
    nodes: list[GenerationTaskNode] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> "GenerationRenderGraph":
        task_ids = [node.task_id for node in self.nodes]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("generation task IDs must be unique")
        known = set(task_ids)
        remaining: dict[str, set[str]] = {}
        for node in self.nodes:
            unknown = set(node.depends_on) - known
            if unknown:
                raise ValueError(f"task {node.task_id} has unknown dependencies: {sorted(unknown)}")
            if node.task_id in node.depends_on:
                raise ValueError(f"task {node.task_id} cannot depend on itself")
            remaining[node.task_id] = set(node.depends_on)
        resolved: set[str] = set()
        while remaining:
            ready = sorted(task_id for task_id, deps in remaining.items() if deps <= resolved)
            if not ready:
                raise ValueError("generation render graph contains a cycle")
            for task_id in ready:
                resolved.add(task_id)
                remaining.pop(task_id)
        return self


class GenerationCostItem(GenerationModel):
    segment_id: str = Field(min_length=1)
    component: Literal["reference_image", "video", "tts", "native_audio"]
    calls: int = Field(ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    confidence: Literal["exact", "estimated", "unknown"] = "unknown"
    price_snapshot_id: str | None = None


class GenerationCostPlan(GenerationModel):
    reference_image_calls: int = Field(ge=0)
    video_calls: int = Field(ge=0)
    tts_calls: int = Field(ge=0)
    native_audio_calls: int = Field(ge=0)
    expected_calls: int = Field(ge=0)
    hard_maximum_calls: int = Field(ge=0)
    items: list[GenerationCostItem] = Field(default_factory=list)
    totals_by_currency: dict[str, Decimal] = Field(default_factory=dict)
    unknown_cost_components: list[str] = Field(default_factory=list)
    pricing_snapshot_dates: list[str] = Field(default_factory=list)


class GenerationReadinessIssue(GenerationModel):
    code: str = Field(min_length=1)
    scope: IssueScope
    severity: Literal["error", "warning"]
    message: str = Field(min_length=1)
    segment_id: str | None = None
    source_shot_id: str | None = None


class GenerationReadinessReport(GenerationModel):
    planning_ready: bool
    execution_route_ready: bool
    media_quality_validated: Literal[False] = False
    external_api_calls: Literal[0] = 0
    errors: list[GenerationReadinessIssue] = Field(default_factory=list)
    warnings: list[GenerationReadinessIssue] = Field(default_factory=list)


class GenerationPlanEpisode(GenerationModel):
    schema_version: Literal[GENERATION_PLAN_SCHEMA_VERSION] = GENERATION_PLAN_SCHEMA_VERSION
    compiler_version: Literal[GENERATION_COMPILER_VERSION] = GENERATION_COMPILER_VERSION
    generation_plan_episode_id: str = Field(min_length=1)
    source_episode_id: str = Field(min_length=1)
    source_prepared_episode_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    policy_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    provider_profile_id: str = Field(min_length=1)
    provider_route_id: str = Field(min_length=1)
    timeline_fps: int = Field(ge=1)
    target_frame_count: int = Field(gt=0)
    target_duration_seconds: Decimal = Field(gt=0)
    segments: list[GenerationSegment] = Field(min_length=1)
    continuity_contracts: list[ContinuityContract] = Field(default_factory=list)
    reference_asset_requirements: list[ReferenceAssetRequirement] = Field(default_factory=list)
    render_graph: GenerationRenderGraph
    cost_plan: GenerationCostPlan
    readiness_report: GenerationReadinessReport
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_episode(self) -> "GenerationPlanEpisode":
        expected_start = 0
        for order, segment in enumerate(self.segments, start=1):
            if segment.order != order:
                raise ValueError("segment order must be contiguous")
            if segment.editorial_start_frame != expected_start:
                raise ValueError("segments must be contiguous without gaps")
            expected_start = segment.editorial_end_frame
        if expected_start != self.target_frame_count:
            raise ValueError("segment frame total must equal target_frame_count")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("content_digest does not match canonical plan content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "GenerationPlanEpisode":
        provisional = cls.model_construct(
            **data,
            content_digest="sha256:" + "0" * 64,
        )
        digest = provisional.compute_content_digest()
        return cls.model_validate({**data, "content_digest": digest})

    def _content_payload(self) -> dict:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return payload

    def compute_content_digest(self) -> str:
        canonical = json.dumps(
            self._content_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def to_canonical_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )
