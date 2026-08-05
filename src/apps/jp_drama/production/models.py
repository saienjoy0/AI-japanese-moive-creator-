"""Strict contracts for the single production entry and provider-neutral segment artifacts."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PRODUCTION_ENTRY_SCHEMA_VERSION = "1.0.0"
SEGMENT_ARTIFACT_SCHEMA_VERSION = "1.0.0"
EPISODE_COMPOSE_REPORT_SCHEMA_VERSION = "1.0.0"


class ProductionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class SegmentArtifact(ProductionModel):
    """One validated provider or operator-produced MP4 bound to one plan segment."""

    schema_version: Literal[SEGMENT_ARTIFACT_SCHEMA_VERSION] = (
        SEGMENT_ARTIFACT_SCHEMA_VERSION
    )
    segment_id: str = Field(min_length=1)
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    provider_route_id: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    frame_count: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    audio_present: bool
    approval_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    ledger_path: str | None = None
    imported_by: str | None = None
    valid: bool = True
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_success(self) -> "SegmentArtifact":
        if self.valid and self.errors:
            raise ValueError("valid segment artifact cannot contain errors")
        if self.valid and self.approval_digest is None:
            raise ValueError("valid segment artifact requires approval_digest")
        if not self.valid and not self.errors:
            raise ValueError("invalid segment artifact requires at least one error")
        return self


class SegmentArtifactManifest(ProductionModel):
    """Complete immutable set of segment outputs for one GenerationPlanEpisode."""

    schema_version: Literal[PRODUCTION_ENTRY_SCHEMA_VERSION] = (
        PRODUCTION_ENTRY_SCHEMA_VERSION
    )
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    artifacts: list[SegmentArtifact] = Field(min_length=1)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> "SegmentArtifactManifest":
        segment_ids = [item.segment_id for item in self.artifacts]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("segment artifact IDs must be unique")
        for artifact in self.artifacts:
            if artifact.generation_plan_digest != self.generation_plan_digest:
                raise ValueError(
                    f"artifact {artifact.segment_id} belongs to another GenerationPlan"
                )
        if self.content_digest != self.compute_content_digest():
            raise ValueError("segment artifact manifest digest does not match content")
        return self

    @classmethod
    def build_with_digest(
        cls,
        *,
        generation_plan_digest: str,
        artifacts: list[SegmentArtifact],
    ) -> "SegmentArtifactManifest":
        provisional = cls.model_construct(
            generation_plan_digest=generation_plan_digest,
            artifacts=artifacts,
            content_digest="sha256:" + "0" * 64,
        )
        return cls.model_validate(
            {
                "generation_plan_digest": generation_plan_digest,
                "artifacts": artifacts,
                "content_digest": provisional.compute_content_digest(),
            }
        )

    def compute_content_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def to_canonical_json(self) -> str:
        return (
            json.dumps(
                self.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )


class SegmentComposeValidation(ProductionModel):
    segment_id: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    trimmed_file: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    trimmed_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    expected_frames: int = Field(gt=0)
    actual_frames: int = Field(gt=0)
    expected_duration_seconds: float = Field(gt=0)
    actual_duration_seconds: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    video_streams: int = Field(ge=0)
    audio_streams: int = Field(ge=0)
    valid: bool
    errors: list[str] = Field(default_factory=list)


class EpisodeComposeReport(ProductionModel):
    schema_version: Literal[EPISODE_COMPOSE_REPORT_SCHEMA_VERSION] = (
        EPISODE_COMPOSE_REPORT_SCHEMA_VERSION
    )
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_manifest_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    output_file: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    expected_frame_count: int = Field(gt=0)
    actual_frame_count: int = Field(gt=0)
    video_streams: int = Field(ge=0)
    audio_streams: int = Field(ge=0)
    black_duration_seconds: float = Field(ge=0)
    segment_order: list[str] = Field(min_length=1)
    segment_validations: list[SegmentComposeValidation] = Field(min_length=1)
    external_api_calls: Literal[0] = 0
    valid: bool
    errors: list[str] = Field(default_factory=list)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @classmethod
    def build_with_digest(cls, **data: object) -> "EpisodeComposeReport":
        provisional = cls.model_construct(
            **data,
            content_digest="sha256:" + "0" * 64,
        )
        payload = provisional.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls.model_validate(
            {
                **data,
                "content_digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            }
        )

    def to_canonical_json(self) -> str:
        return (
            json.dumps(
                self.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )


class ProductionPreflightReport(ProductionModel):
    schema_version: Literal[PRODUCTION_ENTRY_SCHEMA_VERSION] = (
        PRODUCTION_ENTRY_SCHEMA_VERSION
    )
    prepared_episode_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_bundle_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    route_id: str = Field(min_length=1)
    segment_count: int = Field(gt=0)
    target_frame_count: int = Field(gt=0)
    timeline_fps: int = Field(gt=0)
    target_duration_seconds: float = Field(gt=0)
    planning_ready: bool
    execution_route_ready: bool
    asset_ready: bool
    paid_execution_enabled: Literal[False] = False
    external_api_calls: Literal[0] = 0
    valid: bool
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    next_action: str = Field(min_length=1)
