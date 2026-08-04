"""Versioned domain contracts for the Japanese AI short-drama pipeline.

The models in this module intentionally sit beside, rather than inside, the
upstream LumenX comic-generation models. They describe the editorial and
production decisions that happen before LumenX renders assets and shots.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field, model_validator


SCHEMA_VERSION = "1.0.0"

Identifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
NonEmptyText = Annotated[str, Field(min_length=1, max_length=4000)]
LanguageTag = Annotated[
    str,
    Field(
        min_length=2,
        max_length=35,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
    ),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DomainModel(BaseModel):
    """Strict base model used by all Japanese short-drama contracts."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
    )


class SourceKind(str, Enum):
    MANUAL_TEXT = "manual_text"
    URL = "url"
    TRANSCRIPT = "transcript"
    VIDEO_REFERENCE = "video_reference"


class RightsStatus(str, Enum):
    UNKNOWN = "unknown"
    REFERENCE_ONLY = "reference_only"
    LICENSED = "licensed"
    OWNED = "owned"
    PUBLIC_DOMAIN = "public_domain"


class BeatType(str, Enum):
    HOOK = "hook"
    SETUP = "setup"
    HUMILIATION = "humiliation"
    ESCALATION = "escalation"
    REVEAL = "reveal"
    REVERSAL = "reversal"
    PAYOFF = "payoff"
    CLIFFHANGER = "cliffhanger"


class SimilarityRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProductionStatus(str, Enum):
    DRAFT = "draft"
    ANALYZED = "analyzed"
    ADAPTED = "adapted"
    PLANNED = "planned"
    APPROVED = "approved"


class RenderStrategy(str, Enum):
    NATIVE_AV = "native_av"
    VIDEO_PLUS_TTS = "video_plus_tts"
    SILENT_VIDEO = "silent_video"
    STILL_MOTION = "still_motion"
    EXISTING_ASSET = "existing_asset"


class Currency(str, Enum):
    JPY = "JPY"
    CNY = "CNY"
    USD = "USD"


class SourceAsset(DomainModel):
    asset_id: Identifier
    media_type: Literal["video", "audio", "image", "text"]
    uri: NonEmptyText
    checksum_sha256: Annotated[
        str | None,
        Field(default=None, pattern=r"^[a-fA-F0-9]{64}$"),
    ] = None
    license_note: str | None = Field(default=None, max_length=1000)


class SourceRecord(DomainModel):
    source_id: Identifier
    kind: SourceKind
    original_language: LanguageTag = "zh-CN"
    title: NonEmptyText
    synopsis: NonEmptyText
    transcript: str | None = Field(default=None, max_length=100_000)
    source_url: HttpUrl | None = None
    captured_at: datetime = Field(default_factory=utc_now)
    rights_status: RightsStatus = RightsStatus.REFERENCE_ONLY
    provenance_notes: str | None = Field(default=None, max_length=4000)
    usage_constraints: list[NonEmptyText] = Field(default_factory=list)
    assets: list[SourceAsset] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_assets(self) -> "SourceRecord":
        asset_ids = [asset.asset_id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("source asset IDs must be unique")
        return self


class DramaticBeat(DomainModel):
    beat_id: Identifier
    order: int = Field(ge=1, le=50)
    beat_type: BeatType
    summary: NonEmptyText
    emotion_before: str | None = Field(default=None, max_length=100)
    emotion_after: str | None = Field(default=None, max_length=100)
    source_evidence: str | None = Field(default=None, max_length=4000)
    must_transform: bool = True


class BeatSheet(DomainModel):
    beat_sheet_id: Identifier
    source_id: Identifier
    core_premise: NonEmptyText
    audience_hook: NonEmptyText
    emotional_promise: NonEmptyText
    beats: list[DramaticBeat] = Field(min_length=3, max_length=50)
    extracted_character_roles: list[NonEmptyText] = Field(default_factory=list)
    success_signals: list[NonEmptyText] = Field(default_factory=list)
    status: ProductionStatus = ProductionStatus.ANALYZED

    @model_validator(mode="after")
    def validate_beat_sequence(self) -> "BeatSheet":
        beat_ids = [beat.beat_id for beat in self.beats]
        if len(beat_ids) != len(set(beat_ids)):
            raise ValueError("beat IDs must be unique")

        orders = [beat.order for beat in self.beats]
        expected = list(range(1, len(self.beats) + 1))
        if orders != expected:
            raise ValueError(f"beat order must be contiguous and sorted: expected {expected}")

        if self.beats[0].beat_type is not BeatType.HOOK:
            raise ValueError("the first dramatic beat must be a hook")
        if self.beats[-1].beat_type not in {BeatType.PAYOFF, BeatType.CLIFFHANGER}:
            raise ValueError("the last dramatic beat must be a payoff or cliffhanger")
        return self


class AdaptedCharacter(DomainModel):
    character_id: Identifier
    display_name: NonEmptyText
    dramatic_role: NonEmptyText
    age_band: str | None = Field(default=None, max_length=100)
    occupation: str | None = Field(default=None, max_length=200)
    relationship_notes: str | None = Field(default=None, max_length=1000)
    speech_style: str | None = Field(default=None, max_length=1000)
    visual_notes: str | None = Field(default=None, max_length=2000)


class BeatMapping(DomainModel):
    source_beat_id: Identifier
    adapted_beat_id: Identifier
    adapted_summary: NonEmptyText
    transformation_notes: NonEmptyText


class AdaptationChange(DomainModel):
    category: Literal[
        "setting",
        "character",
        "relationship",
        "institution",
        "dialogue",
        "plot",
        "visual",
        "other",
    ]
    original_element: NonEmptyText
    japanese_element: NonEmptyText
    reason: NonEmptyText


class OriginalityAssessment(DomainModel):
    similarity_risk: SimilarityRisk
    retained_elements: list[NonEmptyText] = Field(default_factory=list)
    transformed_elements: list[NonEmptyText] = Field(default_factory=list)
    prohibited_copy_elements: list[NonEmptyText] = Field(default_factory=list)
    reviewer_notes: str | None = Field(default=None, max_length=4000)
    approved: bool = False

    @model_validator(mode="after")
    def high_risk_cannot_be_approved(self) -> "OriginalityAssessment":
        if self.similarity_risk is SimilarityRisk.HIGH and self.approved:
            raise ValueError("a high-similarity adaptation cannot be approved")
        return self


class JapaneseAdaptation(DomainModel):
    adaptation_id: Identifier
    source_id: Identifier
    beat_sheet_id: Identifier
    working_title: NonEmptyText
    logline: NonEmptyText
    target_audience: NonEmptyText
    setting: NonEmptyText
    characters: list[AdaptedCharacter] = Field(min_length=1, max_length=12)
    beat_mappings: list[BeatMapping] = Field(min_length=3, max_length=50)
    cultural_changes: list[AdaptationChange] = Field(default_factory=list)
    originality: OriginalityAssessment
    restrictions: list[NonEmptyText] = Field(default_factory=list)
    status: ProductionStatus = ProductionStatus.ADAPTED

    @model_validator(mode="after")
    def validate_adaptation_ids(self) -> "JapaneseAdaptation":
        character_ids = [character.character_id for character in self.characters]
        if len(character_ids) != len(set(character_ids)):
            raise ValueError("adapted character IDs must be unique")

        source_beat_ids = [mapping.source_beat_id for mapping in self.beat_mappings]
        if len(source_beat_ids) != len(set(source_beat_ids)):
            raise ValueError("each source beat may be mapped only once")

        adapted_beat_ids = [mapping.adapted_beat_id for mapping in self.beat_mappings]
        if len(adapted_beat_ids) != len(set(adapted_beat_ids)):
            raise ValueError("adapted beat IDs must be unique")
        return self


class LocationPlan(DomainModel):
    location_id: Identifier
    name: NonEmptyText
    description: NonEmptyText
    time_of_day: str | None = Field(default=None, max_length=100)
    continuity_rules: list[NonEmptyText] = Field(default_factory=list)


class PropPlan(DomainModel):
    prop_id: Identifier
    name: NonEmptyText
    story_function: NonEmptyText
    visual_notes: str | None = Field(default=None, max_length=1000)


class EpisodePlan(DomainModel):
    episode_id: Identifier
    adaptation_id: Identifier
    series_id: Identifier
    episode_number: int = Field(ge=1)
    title: NonEmptyText
    target_duration_seconds: float = Field(default=60.0, ge=30.0, le=90.0)
    aspect_ratio: Literal["9:16"] = "9:16"
    fps: Literal[24, 25, 30, 60] = 30
    narrative_goal: NonEmptyText
    opening_hook_text: NonEmptyText
    closing_hook_text: NonEmptyText
    character_ids: list[Identifier] = Field(min_length=1, max_length=12)
    locations: list[LocationPlan] = Field(min_length=1, max_length=12)
    props: list[PropPlan] = Field(default_factory=list, max_length=30)
    status: ProductionStatus = ProductionStatus.PLANNED

    @model_validator(mode="after")
    def validate_episode_references(self) -> "EpisodePlan":
        if len(self.character_ids) != len(set(self.character_ids)):
            raise ValueError("episode character IDs must be unique")

        location_ids = [location.location_id for location in self.locations]
        if len(location_ids) != len(set(location_ids)):
            raise ValueError("episode location IDs must be unique")

        prop_ids = [prop.prop_id for prop in self.props]
        if len(prop_ids) != len(set(prop_ids)):
            raise ValueError("episode prop IDs must be unique")
        return self


class DialogueCue(DomainModel):
    cue_id: Identifier
    speaker_character_id: Identifier
    text: NonEmptyText
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    emotion: str | None = Field(default=None, max_length=100)
    delivery: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_timing(self) -> "DialogueCue":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("dialogue cue end must be after start")
        return self


class CameraPlan(DomainModel):
    shot_size: Literal[
        "extreme_close_up",
        "close_up",
        "medium_close_up",
        "medium",
        "full",
        "long",
        "extreme_long",
    ] = "medium"
    angle: Literal[
        "eye_level",
        "high",
        "low",
        "birds_eye",
        "worms_eye",
        "over_shoulder",
        "dutch",
        "pov",
    ] = "eye_level"
    movement: Literal[
        "static",
        "push_in",
        "pull_out",
        "pan_left",
        "pan_right",
        "tilt_up",
        "tilt_down",
        "orbit",
        "follow",
        "handheld",
        "zoom_in",
        "zoom_out",
    ] = "static"
    speed: Literal["slow", "normal", "fast"] = "normal"


class AudioPlan(DomainModel):
    ambience: str | None = Field(default=None, max_length=1000)
    sound_effects: list[NonEmptyText] = Field(default_factory=list)
    bgm_cue: str | None = Field(default=None, max_length=1000)
    generated_native_audio: bool = False


class Shot(DomainModel):
    shot_id: Identifier
    order: int = Field(ge=1, le=100)
    adapted_beat_id: Identifier
    duration_seconds: float = Field(gt=0, le=20)
    location_id: Identifier
    character_ids: list[Identifier] = Field(default_factory=list, max_length=12)
    prop_ids: list[Identifier] = Field(default_factory=list, max_length=30)
    action: NonEmptyText
    visual_description: NonEmptyText
    dialogue: list[DialogueCue] = Field(default_factory=list, max_length=20)
    camera: CameraPlan = Field(default_factory=CameraPlan)
    audio: AudioPlan = Field(default_factory=AudioPlan)
    render_strategy: RenderStrategy
    reference_asset_ids: list[Identifier] = Field(default_factory=list)
    continuity_notes: list[NonEmptyText] = Field(default_factory=list)
    generation_notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_shot(self) -> "Shot":
        if len(self.character_ids) != len(set(self.character_ids)):
            raise ValueError("shot character IDs must be unique")
        if len(self.prop_ids) != len(set(self.prop_ids)):
            raise ValueError("shot prop IDs must be unique")

        cue_ids = [cue.cue_id for cue in self.dialogue]
        if len(cue_ids) != len(set(cue_ids)):
            raise ValueError("dialogue cue IDs must be unique within a shot")

        for cue in self.dialogue:
            if cue.end_seconds > self.duration_seconds:
                raise ValueError(
                    f"dialogue cue {cue.cue_id} ends after shot {self.shot_id}"
                )
            if cue.speaker_character_id not in self.character_ids:
                raise ValueError(
                    f"dialogue speaker {cue.speaker_character_id} is not in shot characters"
                )
        return self


class ShotPlan(DomainModel):
    shot_plan_id: Identifier
    episode_id: Identifier
    target_duration_seconds: float = Field(ge=30, le=90)
    shots: list[Shot] = Field(min_length=1, max_length=30)
    duration_tolerance_seconds: float = Field(default=1.0, ge=0, le=5)

    @computed_field
    @property
    def total_duration_seconds(self) -> float:
        return round(sum(shot.duration_seconds for shot in self.shots), 3)

    @model_validator(mode="after")
    def validate_shot_sequence(self) -> "ShotPlan":
        shot_ids = [shot.shot_id for shot in self.shots]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("shot IDs must be unique")

        orders = [shot.order for shot in self.shots]
        expected = list(range(1, len(self.shots) + 1))
        if orders != expected:
            raise ValueError(f"shot order must be contiguous and sorted: expected {expected}")

        difference = abs(self.total_duration_seconds - self.target_duration_seconds)
        if difference > self.duration_tolerance_seconds:
            raise ValueError(
                "shot durations do not match the target duration "
                f"({self.total_duration_seconds}s vs {self.target_duration_seconds}s)"
            )
        return self


class ShotCostEstimate(DomainModel):
    shot_id: Identifier
    render_strategy: RenderStrategy
    provider: NonEmptyText
    model: NonEmptyText
    estimated_primary_cost: Decimal = Field(ge=0, decimal_places=4)
    reserved_retry_cost: Decimal = Field(default=Decimal("0"), ge=0, decimal_places=4)
    max_attempts: int = Field(default=1, ge=1, le=5)
    fallback_strategy: RenderStrategy | None = None
    notes: str | None = Field(default=None, max_length=1000)

    @computed_field
    @property
    def estimated_total_cost(self) -> Decimal:
        return self.estimated_primary_cost + self.reserved_retry_cost


class CostPlan(DomainModel):
    cost_plan_id: Identifier
    episode_id: Identifier
    currency: Currency = Currency.JPY
    budget_limit: Decimal = Field(ge=0, decimal_places=2)
    contingency_rate: Decimal = Field(
        default=Decimal("0.15"),
        ge=0,
        le=1,
        decimal_places=4,
    )
    hard_stop: bool = True
    shot_estimates: list[ShotCostEstimate] = Field(min_length=1, max_length=30)

    @computed_field
    @property
    def subtotal(self) -> Decimal:
        return sum(
            (estimate.estimated_total_cost for estimate in self.shot_estimates),
            start=Decimal("0"),
        )

    @computed_field
    @property
    def contingency(self) -> Decimal:
        return (self.subtotal * self.contingency_rate).quantize(Decimal("0.01"))

    @computed_field
    @property
    def estimated_total(self) -> Decimal:
        return self.subtotal + self.contingency

    @computed_field
    @property
    def within_budget(self) -> bool:
        return self.estimated_total <= self.budget_limit

    @model_validator(mode="after")
    def validate_cost_plan(self) -> "CostPlan":
        shot_ids = [estimate.shot_id for estimate in self.shot_estimates]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("each shot may have only one cost estimate")
        if self.hard_stop and not self.within_budget:
            raise ValueError(
                f"estimated cost {self.estimated_total} exceeds budget {self.budget_limit}"
            )
        return self


class EpisodePackage(DomainModel):
    package_id: Identifier
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    created_at: datetime = Field(default_factory=utc_now)
    source: SourceRecord
    beat_sheet: BeatSheet
    adaptation: JapaneseAdaptation
    episode: EpisodePlan
    shot_plan: ShotPlan
    cost_plan: CostPlan

    @model_validator(mode="after")
    def validate_pipeline_links(self) -> "EpisodePackage":
        if self.beat_sheet.source_id != self.source.source_id:
            raise ValueError("beat sheet must reference the package source")
        if self.adaptation.source_id != self.source.source_id:
            raise ValueError("adaptation must reference the package source")
        if self.adaptation.beat_sheet_id != self.beat_sheet.beat_sheet_id:
            raise ValueError("adaptation must reference the package beat sheet")
        if self.episode.adaptation_id != self.adaptation.adaptation_id:
            raise ValueError("episode must reference the package adaptation")
        if self.shot_plan.episode_id != self.episode.episode_id:
            raise ValueError("shot plan must reference the package episode")
        if self.cost_plan.episode_id != self.episode.episode_id:
            raise ValueError("cost plan must reference the package episode")
        if self.shot_plan.target_duration_seconds != self.episode.target_duration_seconds:
            raise ValueError("episode and shot plan target durations must match")

        source_beat_ids = {beat.beat_id for beat in self.beat_sheet.beats}
        mapped_source_ids = {
            mapping.source_beat_id for mapping in self.adaptation.beat_mappings
        }
        if mapped_source_ids != source_beat_ids:
            missing = sorted(source_beat_ids - mapped_source_ids)
            extra = sorted(mapped_source_ids - source_beat_ids)
            raise ValueError(
                f"adaptation beat mappings must cover the beat sheet; missing={missing}, extra={extra}"
            )

        adapted_beat_ids = {
            mapping.adapted_beat_id for mapping in self.adaptation.beat_mappings
        }
        shot_beat_ids = {shot.adapted_beat_id for shot in self.shot_plan.shots}
        if not shot_beat_ids <= adapted_beat_ids:
            raise ValueError(
                f"shots reference unknown adapted beats: {sorted(shot_beat_ids - adapted_beat_ids)}"
            )

        adaptation_character_ids = {
            character.character_id for character in self.adaptation.characters
        }
        episode_character_ids = set(self.episode.character_ids)
        if not episode_character_ids <= adaptation_character_ids:
            raise ValueError(
                "episode references characters absent from the Japanese adaptation"
            )

        location_ids = {location.location_id for location in self.episode.locations}
        prop_ids = {prop.prop_id for prop in self.episode.props}
        for shot in self.shot_plan.shots:
            if not set(shot.character_ids) <= episode_character_ids:
                raise ValueError(
                    f"shot {shot.shot_id} references characters outside the episode cast"
                )
            if shot.location_id not in location_ids:
                raise ValueError(
                    f"shot {shot.shot_id} references unknown location {shot.location_id}"
                )
            if not set(shot.prop_ids) <= prop_ids:
                raise ValueError(
                    f"shot {shot.shot_id} references props outside the episode plan"
                )

        shot_ids = {shot.shot_id for shot in self.shot_plan.shots}
        cost_shot_ids = {
            estimate.shot_id for estimate in self.cost_plan.shot_estimates
        }
        if cost_shot_ids != shot_ids:
            missing = sorted(shot_ids - cost_shot_ids)
            extra = sorted(cost_shot_ids - shot_ids)
            raise ValueError(
                f"cost plan must cover every shot exactly once; missing={missing}, extra={extra}"
            )
        return self

    def to_canonical_json(self, *, indent: int = 2) -> str:
        """Serialize the complete production contract in stable JSON form."""

        return self.model_dump_json(
            indent=indent,
            exclude_none=True,
            exclude_computed_fields=True,
            round_trip=True,
        )
