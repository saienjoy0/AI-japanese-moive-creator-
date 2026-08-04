"""Persistence contracts for saving PreparedEpisode as LumenX projects."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PERSISTENCE_SCHEMA_VERSION = "1.0.0"


class PersistenceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class PersistenceEntry(PersistenceModel):
    project_id: str
    source_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    package_id: str
    episode_id: str
    prepared_schema_version: str
    compiler_version: str
    project_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_series_id: str | None = None
    episode_number: int = Field(ge=1)


class PersistenceIndex(PersistenceModel):
    schema_version: Literal[PERSISTENCE_SCHEMA_VERSION] = PERSISTENCE_SCHEMA_VERSION
    projects: dict[str, PersistenceEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_project_keys(self) -> "PersistenceIndex":
        mismatched = sorted(
            key
            for key, entry in self.projects.items()
            if key != entry.project_id
        )
        if mismatched:
            raise ValueError(
                f"persistence index keys must match project IDs: {mismatched}"
            )
        return self

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"


class VerificationIssue(PersistenceModel):
    code: str
    severity: Literal["error", "warning"]
    message: str
    field: str | None = None
    frame_id: str | None = None


class VerificationReport(PersistenceModel):
    project_id: str
    verified: bool
    character_count: int = Field(ge=0)
    scene_count: int = Field(ge=0)
    prop_count: int = Field(ge=0)
    frame_count: int = Field(ge=0)
    video_task_count: Literal[0] = 0
    media_url_count: int = Field(default=0, ge=0)
    external_api_calls: Literal[0] = 0
    errors: list[VerificationIssue] = Field(default_factory=list)
    warnings: list[VerificationIssue] = Field(default_factory=list)


class PersistenceResult(PersistenceModel):
    status: Literal["dry_run", "created", "unchanged", "replaced"]
    project_id: str
    source_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    project_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    projects_file: str
    index_file: str
    files_written: list[str] = Field(default_factory=list)
    verified: bool
    external_api_calls: Literal[0] = 0
    verification: VerificationReport

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
