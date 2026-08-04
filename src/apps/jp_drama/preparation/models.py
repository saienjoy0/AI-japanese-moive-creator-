"""Deterministic preparation contracts between Japanese drama data and LumenX."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain import Currency, RenderStrategy


PREPARED_SCHEMA_VERSION = "1.0.0"
COMPILER_VERSION = "1.0.0"


class PreparationModel(BaseModel):
    """Strict base model for the offline preparation layer."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class StrategyCostQuote(PreparationModel):
    strategy: RenderStrategy
    estimated_primary_cost: Decimal = Field(ge=0, decimal_places=4)
    reserved_retry_cost: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=4)

    @property
    def estimated_total_cost(self) -> Decimal:
        return self.estimated_primary_cost + self.reserved_retry_cost


class ModelProfile(PreparationModel):
    provider: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    supported_strategies: list[RenderStrategy] = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    fallback_costs: list[StrategyCostQuote] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_values(self) -> "ModelProfile":
        if len(self.supported_strategies) != len(set(self.supported_strategies)):
            raise ValueError("supported strategies must be unique")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("model capabilities must be unique")
        strategies = [quote.strategy for quote in self.fallback_costs]
        if len(strategies) != len(set(strategies)):
            raise ValueError("fallback cost strategies must be unique")
        return self

    def supports(self, strategy: RenderStrategy, required_capabilities: list[str]) -> bool:
        return strategy in self.supported_strategies and set(required_capabilities) <= set(
            self.capabilities
        )

    def fallback_cost(self, strategy: RenderStrategy) -> StrategyCostQuote | None:
        return next((quote for quote in self.fallback_costs if quote.strategy == strategy), None)


class ModelCatalog(PreparationModel):
    catalog_version: str = Field(default="1.0.0", min_length=1)
    profiles: list[ModelProfile] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_profiles(self) -> "ModelCatalog":
        keys = [(profile.provider, profile.model) for profile in self.profiles]
        if len(keys) != len(set(keys)):
            raise ValueError("model catalog provider/model pairs must be unique")
        return self

    def get(self, provider: str, model: str) -> ModelProfile | None:
        return next(
            (
                profile
                for profile in self.profiles
                if profile.provider == provider and profile.model == model
            ),
            None,
        )


class ProjectDraft(PreparationModel):
    project_id: str
    title: str
    description: str
    aspect_ratio: Literal["9:16"] = "9:16"
    fps: int = Field(ge=1)
    target_duration_seconds: float = Field(gt=0)
    episode_number: int = Field(ge=1)
    series_id: str | None = None
    language: Literal["ja-JP"] = "ja-JP"


class CharacterSeed(PreparationModel):
    seed_id: str
    source_character_id: str
    name: str
    description: str
    occupation: str | None = None
    speech_style: str | None = None
    visual_prompt: str
    negative_prompt: str | None = None


class LocationSeed(PreparationModel):
    seed_id: str
    source_location_id: str
    name: str
    description: str
    time_of_day: str | None = None
    continuity_rules: list[str] = Field(default_factory=list)
    visual_prompt: str


class PropSeed(PreparationModel):
    seed_id: str
    source_prop_id: str
    name: str
    story_function: str
    visual_prompt: str


class CameraDraft(PreparationModel):
    shot_size: str
    angle: str
    movement: str
    speed: str


class DialogueDraft(PreparationModel):
    cue_id: str
    speaker_character_id: str
    text: str
    start_seconds: float
    end_seconds: float
    emotion: str | None = None
    delivery: str | None = None


class AudioDraft(PreparationModel):
    ambience: str | None = None
    sound_effects: list[str] = Field(default_factory=list)
    bgm_cue: str | None = None
    generated_native_audio: bool = False


class StoryboardFrameDraft(PreparationModel):
    frame_id: str
    source_shot_id: str
    adapted_beat_id: str
    order: int = Field(ge=1)
    duration_seconds: float = Field(gt=0)
    location_seed_id: str
    character_seed_ids: list[str] = Field(default_factory=list)
    prop_seed_ids: list[str] = Field(default_factory=list)
    action: str
    visual_description: str
    camera: CameraDraft
    dialogue_cues: list[DialogueDraft] = Field(default_factory=list)
    audio: AudioDraft
    render_intent_id: str


class RenderIntent(PreparationModel):
    intent_id: str
    shot_id: str
    requested_strategy: RenderStrategy
    resolved_strategy: RenderStrategy
    provider: str
    model: str
    model_capabilities_required: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    fallback_strategy: RenderStrategy | None = None
    fallback_applied: bool = False
    resolution_reason: str | None = None
    estimated_primary_cost: Decimal = Field(ge=0, decimal_places=4)
    reserved_retry_cost: Decimal = Field(ge=0, decimal_places=4)
    estimated_total_cost: Decimal = Field(ge=0, decimal_places=4)

    @model_validator(mode="after")
    def validate_cost_total(self) -> "RenderIntent":
        expected = self.estimated_primary_cost + self.reserved_retry_cost
        if self.estimated_total_cost != expected:
            raise ValueError("render intent total cost must equal primary plus retry reserve")
        if self.fallback_applied and self.fallback_strategy is None:
            raise ValueError("fallback strategy is required when fallback is applied")
        return self


class RenderTaskNode(PreparationModel):
    task_id: str
    shot_id: str
    task_type: Literal[
        "generate_image",
        "generate_video",
        "generate_native_av",
        "generate_tts",
        "generate_subtitles",
        "apply_still_motion",
        "mux_audio_video",
        "finalize_shot",
    ]
    depends_on: list[str] = Field(default_factory=list)
    external_api_required: bool
    provider_required: bool


class RenderGraph(PreparationModel):
    nodes: list[RenderTaskNode]

    @model_validator(mode="after")
    def validate_graph(self) -> "RenderGraph":
        task_ids = [node.task_id for node in self.nodes]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("render task IDs must be unique")

        known = set(task_ids)
        for node in self.nodes:
            unknown = set(node.depends_on) - known
            if unknown:
                raise ValueError(f"task {node.task_id} has unknown dependencies: {sorted(unknown)}")
            if node.task_id in node.depends_on:
                raise ValueError(f"task {node.task_id} cannot depend on itself")

        remaining = {node.task_id: set(node.depends_on) for node in self.nodes}
        resolved: set[str] = set()
        while remaining:
            ready = sorted(task_id for task_id, deps in remaining.items() if deps <= resolved)
            if not ready:
                raise ValueError("render task graph contains a cycle")
            for task_id in ready:
                resolved.add(task_id)
                remaining.pop(task_id)
        return self


class ShotBudgetItem(PreparationModel):
    shot_id: str
    strategy: RenderStrategy
    primary_cost: Decimal = Field(ge=0)
    retry_cost: Decimal = Field(ge=0)
    total_cost: Decimal = Field(ge=0)


class BudgetSnapshot(PreparationModel):
    currency: Currency
    budget_limit: Decimal = Field(ge=0)
    contingency_rate: Decimal = Field(ge=0, le=1)
    shot_items: list[ShotBudgetItem]
    subtotal: Decimal = Field(ge=0)
    contingency: Decimal = Field(ge=0)
    estimated_total: Decimal = Field(ge=0)
    within_budget: bool
    hard_stop: bool


class MappingEntry(PreparationModel):
    source_id: str
    target_id: str


class MappingTrace(PreparationModel):
    characters: list[MappingEntry] = Field(default_factory=list)
    locations: list[MappingEntry] = Field(default_factory=list)
    props: list[MappingEntry] = Field(default_factory=list)
    shots: list[MappingEntry] = Field(default_factory=list)
    adapted_beats: list[MappingEntry] = Field(default_factory=list)
    mapping_coverage: float = Field(ge=0, le=1)


class ReadinessIssue(PreparationModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    shot_id: str | None = None
    field: str | None = None


class ReadinessReport(PreparationModel):
    package_id: str
    episode_id: str
    duration_seconds: float
    shot_count: int = Field(ge=0)
    character_count: int = Field(ge=0)
    location_count: int = Field(ge=0)
    prop_count: int = Field(ge=0)
    mapping_coverage: float = Field(ge=0, le=1)
    resolved_render_intents: int = Field(ge=0)
    total_render_intents: int = Field(ge=0)
    budget_limit: Decimal = Field(ge=0)
    estimated_total: Decimal = Field(ge=0)
    currency: Currency
    external_api_calls: Literal[0] = 0
    generation_ready: bool
    errors: list[ReadinessIssue] = Field(default_factory=list)
    warnings: list[ReadinessIssue] = Field(default_factory=list)


class PreparedEpisode(PreparationModel):
    schema_version: Literal[PREPARED_SCHEMA_VERSION] = PREPARED_SCHEMA_VERSION
    compiler_version: Literal[COMPILER_VERSION] = COMPILER_VERSION
    source_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    package_id: str
    episode_id: str
    project_draft: ProjectDraft
    character_seeds: list[CharacterSeed]
    location_seeds: list[LocationSeed]
    prop_seeds: list[PropSeed]
    storyboard_frame_drafts: list[StoryboardFrameDraft]
    render_intents: list[RenderIntent]
    render_graph: RenderGraph
    budget_snapshot: BudgetSnapshot
    mapping_trace: MappingTrace
    readiness_report: ReadinessReport

    def to_canonical_json(self, *, indent: int | None = 2) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )
