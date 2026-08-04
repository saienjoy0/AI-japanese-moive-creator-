"""Restart-safe provider submission and cost ledger for paid canary runs."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CANARY_LEDGER_SCHEMA_VERSION = "1.1.0"


class ProviderLedgerError(RuntimeError):
    """A provider operation ledger is invalid or would exceed its immutable limits."""


class LedgerModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ProviderOperationRecord(LedgerModel):
    operation_id: str = Field(min_length=1)
    stage: Literal["keyframe", "render"]
    shot_id: str = Field(min_length=1)
    operation_type: Literal["image", "video", "tts"]
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    status: Literal[
        "submitting",
        "submitted",
        "running",
        "succeeded",
        "failed",
        "unknown",
    ] = "submitting"
    estimated_cost_cny: Decimal = Field(default=Decimal("0"), ge=0)
    provider_task_id: str | None = None
    provider_request_id: str | None = None
    submitted_at: datetime | None = None
    completed_at: datetime | None = None
    output_sha256: str | None = None
    last_error: str | None = None

    @property
    def consumes_submission(self) -> bool:
        return self.status in {
            "submitting",
            "submitted",
            "running",
            "succeeded",
            "failed",
            "unknown",
        }


class CanaryProviderLedger(LedgerModel):
    schema_version: Literal[CANARY_LEDGER_SCHEMA_VERSION] = CANARY_LEDGER_SCHEMA_VERSION
    source_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    execution_profile: str = Field(min_length=1)
    shot_id: str = Field(min_length=1)
    max_api_calls: int = Field(ge=0, le=3)
    max_cost_cny: Decimal = Field(ge=0)
    operations: dict[str, ProviderOperationRecord] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_operation_scope(self) -> "CanaryProviderLedger":
        for key, record in self.operations.items():
            if key != record.operation_id:
                raise ValueError("operation dictionary key must match operation_id")
            if record.shot_id != self.shot_id:
                raise ValueError("all ledger operations must belong to the canary shot")
        if self.committed_api_calls > self.max_api_calls:
            raise ValueError("ledger already exceeds max_api_calls")
        if self.committed_cost_cny > self.max_cost_cny:
            raise ValueError("ledger already exceeds max_cost_cny")
        return self

    @property
    def committed_api_calls(self) -> int:
        return sum(1 for item in self.operations.values() if item.consumes_submission)

    @property
    def committed_cost_cny(self) -> Decimal:
        return sum(
            (item.estimated_cost_cny for item in self.operations.values() if item.consumes_submission),
            start=Decimal("0"),
        )


class CanaryProviderLedgerStore:
    """Atomic JSON store whose spending ceiling survives process restarts and resets."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    def load_or_create(
        self,
        *,
        source_digest: str,
        execution_profile: str,
        shot_id: str,
        max_api_calls: int,
        max_cost_cny: Decimal,
    ) -> CanaryProviderLedger:
        if self.path.exists():
            ledger = CanaryProviderLedger.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
            conflicts: list[str] = []
            if ledger.source_digest != source_digest:
                conflicts.append("source digest")
            if ledger.execution_profile != execution_profile:
                conflicts.append("provider execution profile")
            if ledger.shot_id != shot_id:
                conflicts.append("shot ID")
            if ledger.max_api_calls != max_api_calls:
                conflicts.append("max API calls")
            if ledger.max_cost_cny != max_cost_cny:
                conflicts.append("max cost")
            if conflicts:
                raise ProviderLedgerError(
                    "existing provider ledger belongs to another budget/run: "
                    + ", ".join(conflicts)
                )
            return ledger

        ledger = CanaryProviderLedger(
            source_digest=source_digest,
            execution_profile=execution_profile,
            shot_id=shot_id,
            max_api_calls=max_api_calls,
            max_cost_cny=max_cost_cny,
        )
        self.write(ledger)
        return ledger

    def write(self, ledger: CanaryProviderLedger) -> None:
        ledger.updated_at = datetime.now(timezone.utc)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            ledger.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
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

    def begin(
        self,
        ledger: CanaryProviderLedger,
        *,
        operation_id: str,
        stage: Literal["keyframe", "render"],
        operation_type: Literal["image", "video", "tts"],
        provider: str,
        model: str,
        estimated_cost_cny: Decimal,
    ) -> tuple[ProviderOperationRecord, bool]:
        existing = ledger.operations.get(operation_id)
        if existing is not None:
            return existing, False

        if ledger.committed_api_calls + 1 > ledger.max_api_calls:
            raise ProviderLedgerError(
                f"cumulative provider call ceiling reached ({ledger.max_api_calls})"
            )
        if ledger.committed_cost_cny + estimated_cost_cny > ledger.max_cost_cny:
            raise ProviderLedgerError(
                "cumulative provider cost ceiling would be exceeded: "
                f"{ledger.committed_cost_cny + estimated_cost_cny} CNY > "
                f"{ledger.max_cost_cny} CNY"
            )

        record = ProviderOperationRecord(
            operation_id=operation_id,
            stage=stage,
            shot_id=ledger.shot_id,
            operation_type=operation_type,
            provider=provider,
            model=model,
            estimated_cost_cny=estimated_cost_cny,
        )
        ledger.operations[operation_id] = record
        self.write(ledger)
        return record, True

    def mark_submitted(
        self,
        ledger: CanaryProviderLedger,
        operation_id: str,
        *,
        provider_task_id: str,
        provider_request_id: str | None = None,
    ) -> None:
        record = ledger.operations[operation_id]
        record.status = "submitted"
        record.provider_task_id = provider_task_id
        record.provider_request_id = provider_request_id
        record.submitted_at = record.submitted_at or datetime.now(timezone.utc)
        record.last_error = None
        self.write(ledger)

    def mark_succeeded(
        self,
        ledger: CanaryProviderLedger,
        operation_id: str,
        *,
        output_sha256: str | None = None,
    ) -> None:
        record = ledger.operations[operation_id]
        record.status = "succeeded"
        record.completed_at = datetime.now(timezone.utc)
        record.output_sha256 = output_sha256
        record.last_error = None
        self.write(ledger)

    def mark_unknown(
        self,
        ledger: CanaryProviderLedger,
        operation_id: str,
        error: str,
    ) -> None:
        record = ledger.operations[operation_id]
        record.status = "unknown"
        record.last_error = error
        self.write(ledger)
