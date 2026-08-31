"""Persistent contracts for restart-safe full-episode generation and composition."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


FULL_EPISODE_RUN_SCHEMA_VERSION = "1.0.0"
FULL_EPISODE_REPORT_SCHEMA_VERSION = "1.0.0"

SegmentRunStatus = Literal[
    "pending",
    "rendering",
    "rendered",
    "trimming",
    "trimmed",
    "validated",
    "failed",
]
EpisodeRunStatus = Literal[
    "planned",
    "rendering",
    "composing",
    "succeeded",
    "failed",
]


class FullEpisodeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class FullEpisodeSegmentState(FullEpisodeModel):
    segment_id: str = Field(min_length=1)
    order: int = Field(ge=1)
    editorial_start_frame: int = Field(ge=0)
    editorial_end_frame: int = Field(gt=0)
    editorial_frame_count: int = Field(gt=0)
    requested_duration_seconds: int = Field(gt=0)
    used_start_frame: int = Field(ge=0)
    used_end_frame: int = Field(gt=0)
    status: SegmentRunStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    source_output_file: str | None = None
    source_output_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    trimmed_output_file: str | None = None
    trimmed_output_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    ledger_file: str | None = None
    segment_report_file: str | None = None
    last_error: str | None = None

    @model_validator(mode="after")
    def validate_frames(self) -> "FullEpisodeSegmentState":
        if self.editorial_end_frame - self.editorial_start_frame != self.editorial_frame_count:
            raise ValueError("segment editorial frame range must equal editorial_frame_count")
        if self.used_end_frame - self.used_start_frame != self.editorial_frame_count:
            raise ValueError("segment used range must equal editorial_frame_count")
        return self


class FullEpisodeRunState(FullEpisodeModel):
    schema_version: Literal[FULL_EPISODE_RUN_SCHEMA_VERSION] = (
        FULL_EPISODE_RUN_SCHEMA_VERSION
    )
    run_id: str = Field(min_length=1)
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    prepared_episode_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_bundle_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    execution_budget_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    target_fps: int = Field(gt=0)
    target_frame_count: int = Field(gt=0)
    target_width: int = Field(gt=0)
    target_height: int = Field(gt=0)
    output_file: str = Field(min_length=1)
    status: EpisodeRunStatus = "planned"
    segment_order: list[str] = Field(min_length=1)
    segments: dict[str, FullEpisodeSegmentState] = Field(min_length=1)
    final_output_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_segment_scope(self) -> "FullEpisodeRunState":
        if len(self.segment_order) != len(set(self.segment_order)):
            raise ValueError("segment_order must contain unique segment IDs")
        if set(self.segment_order) != set(self.segments):
            raise ValueError("segment_order must exactly match segment state keys")
        expected_order = list(range(1, len(self.segment_order) + 1))
        actual_order = [self.segments[item].order for item in self.segment_order]
        if actual_order != expected_order:
            raise ValueError("segment state order must be contiguous")
        if self.target_width * 16 != self.target_height * 9:
            raise ValueError("full-episode target dimensions must be 9:16")
        return self

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"


class SegmentMediaValidation(FullEpisodeModel):
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


class FullEpisodeValidationReport(FullEpisodeModel):
    schema_version: Literal[FULL_EPISODE_REPORT_SCHEMA_VERSION] = (
        FULL_EPISODE_REPORT_SCHEMA_VERSION
    )
    run_id: str = Field(min_length=1)
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
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
    segment_validations: list[SegmentMediaValidation] = Field(min_length=1)
    external_api_calls: int = Field(default=0, ge=0)
    valid: bool
    errors: list[str] = Field(default_factory=list)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @classmethod
    def build_with_digest(cls, **data: object) -> "FullEpisodeValidationReport":
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
        digest = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
        return cls.model_validate({**data, "content_digest": digest})

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
