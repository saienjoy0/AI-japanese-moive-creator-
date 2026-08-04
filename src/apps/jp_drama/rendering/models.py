"""Execution-state contracts for the provider-free Japanese-drama renderer."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


RENDER_STATE_SCHEMA_VERSION = "1.0.0"


class RenderExecutionModel(BaseModel):
    """Strict base model for persisted PR6 execution state."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


TaskStatus = Literal["pending", "running", "succeeded", "failed"]
ShotStatus = Literal["pending", "running", "succeeded", "failed"]


class TaskExecutionState(RenderExecutionModel):
    task_id: str
    shot_id: str
    task_type: str
    status: TaskStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    input_fingerprint: str
    output_files: list[str] = Field(default_factory=list)
    last_error: str | None = None
    reused: bool = False


class ShotExecutionState(RenderExecutionModel):
    shot_id: str
    order: int = Field(ge=1)
    status: ShotStatus = "pending"
    task_ids: list[str]
    final_video: str | None = None


class RenderRunState(RenderExecutionModel):
    schema_version: Literal[RENDER_STATE_SCHEMA_VERSION] = RENDER_STATE_SCHEMA_VERSION
    source_digest: str
    project_id: str
    output_file: str
    graph_fingerprint: str
    task_order: list[str]
    final_shot_order: list[str]
    task_states: dict[str, TaskExecutionState]
    shot_states: dict[str, ShotExecutionState]
    persistence_status: str | None = None
    final_output_fingerprint: str | None = None
    external_api_calls: Literal[0] = 0

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"


class RenderValidationReport(RenderExecutionModel):
    output_file: str
    width: int
    height: int
    aspect_ratio: str
    fps: float
    duration_seconds: float
    video_streams: int
    audio_streams: int
    black_duration_seconds: float
    subtitle_artifacts: int
    shot_order: list[str]
    source_digest: str
    graph_fingerprint: str
    external_api_calls: Literal[0] = 0
    valid: bool
    errors: list[str] = Field(default_factory=list)

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
