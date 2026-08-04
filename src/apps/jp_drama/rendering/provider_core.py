"""Provider-neutral contracts for Japanese short-drama generation."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROVIDER_CORE_SCHEMA_VERSION = "1.0.0"

AudioStrategy = Literal[
    "native_av",
    "driving_audio",
    "external_audio_post",
    "silent",
]
ReferenceRole = Literal[
    "character",
    "costume",
    "location",
    "prop",
    "storyboard",
    "first_frame",
    "last_frame",
    "motion_reference",
    "video_reference",
    "driving_audio",
    "voice_reference",
]
GenerationModality = Literal["image", "video", "speech"]
ProviderExecutionMode = Literal["automatic", "manual"]


class ProviderCoreError(RuntimeError):
    """A provider-neutral request or operation is invalid."""


class ProviderCoreModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class DialogueLine(ProviderCoreModel):
    cue_id: str = Field(min_length=1)
    speaker_character_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    emotion: str | None = None
    delivery: str | None = None

    @model_validator(mode="after")
    def validate_timing(self) -> "DialogueLine":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("dialogue end_seconds must be greater than start_seconds")
        return self


class ReferenceAsset(ProviderCoreModel):
    asset_id: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    role: ReferenceRole
    order: int = Field(ge=0)
    subject_id: str | None = None


class ProviderCapabilitiesRequired(ProviderCoreModel):
    modality: GenerationModality
    text_to_video: bool = False
    image_to_video: bool = False
    reference_to_video: bool = False
    first_last_frame: bool = False
    video_continuation: bool = False
    native_audio: bool = False
    driving_audio: bool = False
    reference_voice: bool = False
    multi_shot: bool = False
    video_editing: bool = False


class ProviderCapabilities(ProviderCoreModel):
    route_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
    execution_mode: ProviderExecutionMode
    modalities: list[GenerationModality] = Field(min_length=1)
    text_to_video: bool = False
    image_to_video: bool = False
    reference_to_video: bool = False
    first_last_frame: bool = False
    video_continuation: bool = False
    native_audio: bool = False
    driving_audio: bool = False
    reference_voice: bool = False
    multi_shot: bool = False
    video_editing: bool = False
    min_duration_seconds: float = Field(default=1, gt=0)
    max_duration_seconds: float = Field(default=15, gt=0)
    max_reference_images: int = Field(default=0, ge=0)
    max_reference_videos: int = Field(default=0, ge=0)
    max_reference_audios: int = Field(default=0, ge=0)
    supported_aspect_ratios: list[str] = Field(default_factory=lambda: ["9:16"])
    supported_resolutions: list[str] = Field(default_factory=lambda: ["720P"])

    @model_validator(mode="after")
    def validate_ranges(self) -> "ProviderCapabilities":
        if self.max_duration_seconds < self.min_duration_seconds:
            raise ValueError("max_duration_seconds must be at least min_duration_seconds")
        if len(self.modalities) != len(set(self.modalities)):
            raise ValueError("provider modalities must be unique")
        return self

    def supports(self, requirements: ProviderCapabilitiesRequired) -> bool:
        if requirements.modality not in self.modalities:
            return False
        for field_name in (
            "text_to_video",
            "image_to_video",
            "reference_to_video",
            "first_last_frame",
            "video_continuation",
            "native_audio",
            "driving_audio",
            "reference_voice",
            "multi_shot",
            "video_editing",
        ):
            if getattr(requirements, field_name) and not getattr(self, field_name):
                return False
        return True


class ShotGenerationSpec(ProviderCoreModel):
    schema_version: Literal[PROVIDER_CORE_SCHEMA_VERSION] = PROVIDER_CORE_SCHEMA_VERSION
    source_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    shot_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    modality: GenerationModality
    duration_seconds: float = Field(gt=0)
    aspect_ratio: Literal["9:16"] = "9:16"
    resolution: Literal["720P", "1080P"] = "720P"
    prompt: str = Field(min_length=1)
    negative_prompt: str | None = None
    dialogue: list[DialogueLine] = Field(default_factory=list)
    references: list[ReferenceAsset] = Field(default_factory=list)
    audio_strategy: AudioStrategy = "silent"
    required_capabilities: ProviderCapabilitiesRequired

    @model_validator(mode="after")
    def validate_spec(self) -> "ShotGenerationSpec":
        if self.required_capabilities.modality != self.modality:
            raise ValueError("required capability modality must match shot modality")
        cue_ids = [item.cue_id for item in self.dialogue]
        if len(cue_ids) != len(set(cue_ids)):
            raise ValueError("dialogue cue IDs must be unique")
        asset_ids = [item.asset_id for item in self.references]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("reference asset IDs must be unique")
        orders = [item.order for item in self.references]
        if len(orders) != len(set(orders)):
            raise ValueError("reference asset order values must be unique")
        if any(item.end_seconds > self.duration_seconds for item in self.dialogue):
            raise ValueError("dialogue timing must fit inside the shot duration")
        if self.audio_strategy == "driving_audio" and not any(
            item.role == "driving_audio" for item in self.references
        ):
            raise ValueError("driving_audio strategy requires a driving_audio reference")
        if self.audio_strategy == "native_av" and self.modality != "video":
            raise ValueError("native_av strategy requires video modality")
        return self

    @property
    def request_fingerprint(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class ValidationIssue(ProviderCoreModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ValidationReport(ProviderCoreModel):
    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)

    @classmethod
    def success(cls, *warnings: ValidationIssue) -> "ValidationReport":
        return cls(valid=True, warnings=list(warnings))

    @classmethod
    def failure(cls, *errors: ValidationIssue) -> "ValidationReport":
        return cls(valid=False, errors=list(errors))


class Money(ProviderCoreModel):
    amount: Decimal = Field(ge=0)
    currency: str = Field(min_length=3, max_length=8)


class CostEstimate(ProviderCoreModel):
    native_cost: Money | None = None
    confidence: Literal["exact", "estimated", "unknown"] = "unknown"
    price_snapshot_id: str | None = None


class PreparedProviderRequest(ProviderCoreModel):
    route_id: str
    operation_id: str
    request_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    payload: dict[str, Any]


class ProviderSubmission(ProviderCoreModel):
    route_id: str
    operation_id: str
    status: Literal["submitted", "running", "succeeded", "awaiting_operator"]
    provider_task_id: str | None = None
    provider_request_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderPollResult(ProviderCoreModel):
    route_id: str
    operation_id: str
    status: Literal["running", "succeeded", "failed", "awaiting_operator"]
    provider_task_id: str | None = None
    output_uris: list[str] = Field(default_factory=list)
    error: str | None = None


class ProviderArtifact(ProviderCoreModel):
    path: str
    sha256: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    media_type: str = Field(min_length=1)
    raw: bool = True


class ProviderArtifactSet(ProviderCoreModel):
    route_id: str
    operation_id: str
    artifacts: list[ProviderArtifact]


class ProviderDescriptor(ProviderCoreModel):
    route_id: str
    provider: str
    model: str
    origin_vendor: str
    execution_mode: ProviderExecutionMode


@runtime_checkable
class ProviderAdapter(Protocol):
    def descriptor(self) -> ProviderDescriptor:
        ...

    def capabilities(self) -> ProviderCapabilities:
        ...

    def validate(self, request: ShotGenerationSpec) -> ValidationReport:
        ...

    def estimate_cost(self, request: ShotGenerationSpec) -> CostEstimate:
        ...

    def prepare(self, request: ShotGenerationSpec) -> PreparedProviderRequest:
        ...

    def submit(self, request: PreparedProviderRequest) -> ProviderSubmission:
        ...

    def poll(self, submission: ProviderSubmission) -> ProviderPollResult:
        ...

    def download(
        self,
        result: ProviderPollResult,
        output_dir: str | Path,
    ) -> ProviderArtifactSet:
        ...
