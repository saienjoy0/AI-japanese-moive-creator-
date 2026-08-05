"""Atomic, restart-safe execution ledger for MiniMax H3 segment generation."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


H3_EXECUTION_LEDGER_SCHEMA_VERSION = "1.1.0"
H3ExecutionStatus = Literal[
    "planned",
    "assets_preparing",
    "assets_ready",
    "submitting",
    "submitted",
    "queued",
    "running",
    "succeeded",
    "downloading",
    "downloaded",
    "normalizing",
    "normalized",
    "audio_mixing",
    "muxed",
    "validated",
    "failed",
    "cancelled",
    "submission_unknown",
]


class H3ExecutionLedgerError(RuntimeError):
    """A persisted H3 execution cannot be safely resumed."""


class H3LedgerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class H3ExecutionRecord(H3LedgerModel):
    schema_version: Literal[H3_EXECUTION_LEDGER_SCHEMA_VERSION] = H3_EXECUTION_LEDGER_SCHEMA_VERSION
    request_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_id: str = Field(min_length=1)
    route_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    status: H3ExecutionStatus = "planned"
    resolution: Literal["768P", "2K"]
    duration: int = Field(ge=4, le=15)
    ratio: Literal["9:16", "adaptive"]
    prompt_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    reference_asset_hashes: list[str] = Field(default_factory=list)
    task_id: str | None = None
    provider_request_id: str | None = None
    submitted_at: datetime | None = None
    last_polled_at: datetime | None = None
    completed_at: datetime | None = None
    result_url: str | None = None
    raw_video_path: str | None = None
    raw_video_sha256: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    final_video_path: str | None = None
    final_video_sha256: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    estimated_cost_usd: Decimal = Field(ge=0)
    max_cost_usd: Decimal = Field(ge=0)
    actual_usage: dict | None = None
    external_api_calls: int = Field(default=0, ge=0, le=1)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_state(self) -> "H3ExecutionRecord":
        if self.estimated_cost_usd > self.max_cost_usd:
            raise ValueError(
                f"estimated H3 cost {self.estimated_cost_usd} USD exceeds "
                f"max_cost_usd {self.max_cost_usd} USD"
            )
        if self.task_id and self.external_api_calls != 1:
            raise ValueError("task_id requires exactly one external API submission")
        if self.status in {"submitted", "queued", "running", "succeeded", "downloading"}:
            if not self.task_id:
                raise ValueError(f"status {self.status} requires task_id")
        if self.status in {"succeeded", "downloading", "downloaded"} and not self.result_url:
            raise ValueError(f"status {self.status} requires result_url")
        if self.status == "validated" and not self.final_video_sha256:
            raise ValueError("validated status requires final_video_sha256")
        return self


class H3ExecutionLedgerStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    def load(self) -> H3ExecutionRecord | None:
        if not self.path.exists():
            return None
        return H3ExecutionRecord.model_validate_json(self.path.read_text(encoding="utf-8"))

    def load_or_create(self, record: H3ExecutionRecord) -> H3ExecutionRecord:
        existing = self.load()
        if existing is None:
            self.write(record)
            return record
        conflicts = []
        for field in (
            "request_fingerprint",
            "segment_id",
            "route_id",
            "model",
            "resolution",
            "duration",
            "ratio",
            "max_cost_usd",
        ):
            if getattr(existing, field) != getattr(record, field):
                conflicts.append(field)
        if conflicts:
            raise H3ExecutionLedgerError(
                "existing H3 ledger belongs to another request: " + ", ".join(conflicts)
            )
        return existing

    def write(self, record: H3ExecutionRecord) -> None:
        record.updated_at = datetime.now(timezone.utc)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            record.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def set_status(
        self,
        record: H3ExecutionRecord,
        status: H3ExecutionStatus,
        *,
        error: str | None = None,
    ) -> None:
        if record.status == "submission_unknown" and status == "submitting":
            raise H3ExecutionLedgerError(
                "submission_unknown cannot be resubmitted without explicit abandonment"
            )
        record.status = status
        record.error = error
        self.write(record)

    def mark_submitted(
        self,
        record: H3ExecutionRecord,
        *,
        task_id: str,
        provider_request_id: str | None,
    ) -> None:
        if record.task_id and record.task_id != task_id:
            raise H3ExecutionLedgerError("H3 task_id cannot change after submission")
        record.external_api_calls = 1
        record.task_id = task_id
        record.provider_request_id = provider_request_id
        record.submitted_at = record.submitted_at or datetime.now(timezone.utc)
        record.error = None
        record.status = "submitted"
        self.write(record)

    def mark_polled(
        self,
        record: H3ExecutionRecord,
        *,
        status: Literal["queued", "running", "succeeded", "failed", "cancelled"],
        result_url: str | None = None,
        usage: dict | None = None,
        error: str | None = None,
    ) -> None:
        record.last_polled_at = datetime.now(timezone.utc)
        record.result_url = result_url or record.result_url
        record.actual_usage = usage or record.actual_usage
        record.error = error
        record.status = status
        if status in {"succeeded", "failed", "cancelled"}:
            record.completed_at = datetime.now(timezone.utc)
        self.write(record)

    def mark_downloaded(
        self,
        record: H3ExecutionRecord,
        *,
        path: str | Path,
        sha256: str,
    ) -> None:
        record.raw_video_path = str(Path(path).resolve())
        record.raw_video_sha256 = sha256
        record.error = None
        record.status = "downloaded"
        self.write(record)

    def mark_validated(
        self,
        record: H3ExecutionRecord,
        *,
        path: str | Path,
        sha256: str,
    ) -> None:
        record.final_video_path = str(Path(path).resolve())
        record.final_video_sha256 = sha256
        record.error = None
        record.status = "validated"
        self.write(record)
