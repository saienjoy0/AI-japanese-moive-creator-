"""Immutable provider execution plans compiled from PreparedEpisode."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from ..preparation.models import PreparedEpisode, RenderTaskNode, StoryboardFrameDraft
from .provider_core import (
    CostEstimate,
    DialogueLine,
    ProviderCapabilitiesRequired,
    ProviderCoreError,
    ProviderCoreModel,
    ShotGenerationSpec,
)
from .provider_registry import ProviderRegistry


EXECUTION_PLAN_SCHEMA_VERSION = "1.0.0"
RoutingMode = Literal["pinned", "ordered_fallback"]


class ProviderPlanningError(ProviderCoreError):
    """No safe provider route can satisfy a planned generation task."""


class ProviderProfile(ProviderCoreModel):
    profile_id: str = Field(min_length=1)
    routing_mode: RoutingMode = "pinned"
    route_priority: list[str] = Field(min_length=1)
    route_by_shot: dict[str, str] = Field(default_factory=dict)
    fallback_requires_approval: bool = True
    max_cost_cny: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_routes(self) -> "ProviderProfile":
        if len(self.route_priority) != len(set(self.route_priority)):
            raise ValueError("route_priority values must be unique")
        if self.routing_mode == "pinned" and len(self.route_priority) != 1:
            raise ValueError("pinned routing requires exactly one route_priority entry")
        return self


class ExecutionTaskPlan(ProviderCoreModel):
    task_id: str
    shot_id: str
    route_id: str
    request_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generation_spec: ShotGenerationSpec
    estimated_cost: CostEstimate
    fallback_route_id: str | None = None
    fallback_requires_approval: bool = True


class ExecutionPlan(ProviderCoreModel):
    schema_version: Literal[EXECUTION_PLAN_SCHEMA_VERSION] = EXECUTION_PLAN_SCHEMA_VERSION
    source_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    profile_id: str
    routing_mode: RoutingMode
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tasks: dict[str, ExecutionTaskPlan] = Field(min_length=1)
    estimated_total_cny: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_task_map(self) -> "ExecutionPlan":
        for key, item in self.tasks.items():
            if key != item.task_id:
                raise ValueError("execution task key must match task_id")
            if item.generation_spec.source_digest != self.source_digest:
                raise ValueError("all execution tasks must use the plan source_digest")
        return self

    def to_canonical_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )

    @property
    def execution_plan_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("created_at", None)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class ProviderExecutionPlanner:
    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

    def plan(
        self,
        prepared: PreparedEpisode,
        profile: ProviderProfile,
    ) -> ExecutionPlan:
        frames = {
            frame.source_shot_id: frame
            for frame in prepared.storyboard_frame_drafts
        }
        tasks: dict[str, ExecutionTaskPlan] = {}
        total_cny = Decimal("0")
        all_costs_cny = True

        for node in prepared.render_graph.nodes:
            if not node.external_api_required or node.task_type not in {
                "generate_image",
                "generate_video",
                "generate_native_av",
            }:
                continue
            frame = frames[node.shot_id]
            spec = _build_generation_spec(prepared, frame, node)
            route_id, fallback_route_id = self._select_route(profile, spec)
            adapter = self.registry.require(route_id)
            report = adapter.validate(spec)
            if not report.valid:
                messages = "; ".join(issue.message for issue in report.errors)
                raise ProviderPlanningError(
                    f"selected route {route_id} rejected {node.task_id}: {messages}"
                )
            estimate = adapter.estimate_cost(spec)
            if estimate.native_cost is None or estimate.native_cost.currency != "CNY":
                all_costs_cny = False
            else:
                total_cny += estimate.native_cost.amount
            tasks[node.task_id] = ExecutionTaskPlan(
                task_id=node.task_id,
                shot_id=node.shot_id,
                route_id=route_id,
                request_fingerprint=adapter.prepare(spec).request_fingerprint,
                generation_spec=spec,
                estimated_cost=estimate,
                fallback_route_id=fallback_route_id,
                fallback_requires_approval=profile.fallback_requires_approval,
            )

        estimated_total_cny = total_cny if all_costs_cny else None
        if profile.max_cost_cny is not None:
            if estimated_total_cny is None:
                raise ProviderPlanningError(
                    "cannot enforce max_cost_cny because at least one selected route "
                    "has unknown or non-CNY pricing"
                )
            if estimated_total_cny > profile.max_cost_cny:
                raise ProviderPlanningError(
                    f"execution plan cost {estimated_total_cny} CNY exceeds "
                    f"profile limit {profile.max_cost_cny} CNY"
                )

        return ExecutionPlan(
            source_digest=prepared.source_digest,
            profile_id=profile.profile_id,
            routing_mode=profile.routing_mode,
            tasks=tasks,
            estimated_total_cny=estimated_total_cny,
        )

    def _select_route(
        self,
        profile: ProviderProfile,
        spec: ShotGenerationSpec,
    ) -> tuple[str, str | None]:
        explicit = profile.route_by_shot.get(spec.shot_id)
        candidates = [explicit] if explicit else list(profile.route_priority)
        compatible: list[str] = []
        rejected: list[str] = []
        for route_id in candidates:
            adapter = self.registry.require(route_id)
            report = adapter.validate(spec)
            if report.valid:
                compatible.append(route_id)
            else:
                rejected.append(
                    f"{route_id}: "
                    + "; ".join(item.message for item in report.errors)
                )

        if not compatible:
            raise ProviderPlanningError(
                f"no compatible provider route for {spec.task_id}: "
                + " | ".join(rejected)
            )
        if profile.routing_mode == "pinned" or explicit:
            return compatible[0], None
        fallback = compatible[1] if len(compatible) > 1 else None
        return compatible[0], fallback


def _build_generation_spec(
    prepared: PreparedEpisode,
    frame: StoryboardFrameDraft,
    node: RenderTaskNode,
) -> ShotGenerationSpec:
    modality = "image" if node.task_type == "generate_image" else "video"
    if node.task_type == "generate_native_av":
        audio_strategy = "native_av"
    elif frame.dialogue_cues:
        audio_strategy = "external_audio_post"
    else:
        audio_strategy = "silent"

    required = ProviderCapabilitiesRequired(
        modality=modality,
        text_to_video=node.task_type == "generate_native_av",
        image_to_video=node.task_type == "generate_video",
        native_audio=audio_strategy == "native_av",
    )

    camera = frame.camera
    prompt_parts = [
        frame.visual_description,
        f"Action: {frame.action}",
        (
            "Camera: "
            f"{camera.shot_size}, {camera.angle}, {camera.movement}, {camera.speed}"
        ),
    ]
    if frame.audio.ambience:
        prompt_parts.append(f"Ambience: {frame.audio.ambience}")
    if frame.audio.sound_effects:
        prompt_parts.append("Sound effects: " + ", ".join(frame.audio.sound_effects))
    if frame.dialogue_cues:
        prompt_parts.append(
            "Dialogue: "
            + " | ".join(
                f"{cue.speaker_character_id}: {cue.text}"
                for cue in frame.dialogue_cues
            )
        )

    dialogue = [
        DialogueLine(
            cue_id=cue.cue_id,
            speaker_character_id=cue.speaker_character_id,
            text=cue.text,
            start_seconds=cue.start_seconds,
            end_seconds=cue.end_seconds,
            emotion=cue.emotion,
            delivery=cue.delivery,
        )
        for cue in frame.dialogue_cues
    ]

    return ShotGenerationSpec(
        source_digest=prepared.source_digest,
        shot_id=frame.source_shot_id,
        task_id=node.task_id,
        modality=modality,
        duration_seconds=frame.duration_seconds,
        aspect_ratio=prepared.project_draft.aspect_ratio,
        resolution="720P",
        prompt="\n".join(prompt_parts),
        dialogue=dialogue,
        references=[],
        audio_strategy=audio_strategy,
        required_capabilities=required,
    )
