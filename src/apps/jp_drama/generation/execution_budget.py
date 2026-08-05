"""Provider-bound CNY execution budgets with ledger-aware remaining exposure."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..assets.models import ApprovedAssetBundle
from ..rendering.provider_config import LiveProviderConfig
from ..rendering.provider_ledger import CanaryProviderLedger
from .models import GenerationPlanEpisode, GenerationSegment


EXECUTION_BUDGET_SCHEMA_VERSION = "1.0.0"
BudgetComponent = Literal["first_frame", "video", "tts"]
BudgetOperationStatus = Literal["planned", "committed", "satisfied_by_asset"]


class ExecutionBudgetError(RuntimeError):
    """A provider execution budget cannot be computed safely."""


class BudgetModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class ExecutionBudgetOperation(BudgetModel):
    operation_key: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    component: BudgetComponent
    model: str = Field(min_length=1)
    status: BudgetOperationStatus
    quantity: Decimal = Field(ge=0)
    quantity_unit: Literal["image", "second", "character"]
    unit_cost_cny: Decimal = Field(ge=0)
    estimated_cost_cny: Decimal = Field(ge=0)
    remaining_api_calls: int = Field(ge=0, le=1)
    committed_api_calls: int = Field(ge=0)
    committed_cost_cny: Decimal = Field(ge=0)
    ledger_operation_ids: list[str] = Field(default_factory=list)
    asset_id: str | None = None


class ExecutionBudgetPlan(BudgetModel):
    schema_version: Literal[EXECUTION_BUDGET_SCHEMA_VERSION] = (
        EXECUTION_BUDGET_SCHEMA_VERSION
    )
    budget_plan_id: str = Field(min_length=1)
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_bundle_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    provider_route_id: str = Field(min_length=1)
    price_snapshot_id: str = Field(min_length=1)
    currency: Literal["CNY"] = "CNY"
    selected_segment_ids: list[str] = Field(min_length=1)
    hard_maximum_calls: int = Field(ge=0)
    hard_limit_cny: Decimal = Field(ge=0)
    retry_reserve_cny: Decimal = Field(default=Decimal("0"), ge=0)
    candidate_reserve_cny: Decimal = Field(default=Decimal("0"), ge=0)
    operations: list[ExecutionBudgetOperation] = Field(default_factory=list)
    remaining_api_calls: int = Field(ge=0)
    committed_api_calls: int = Field(ge=0)
    total_exposure_api_calls: int = Field(ge=0)
    remaining_cost_cny: Decimal = Field(ge=0)
    committed_cost_cny: Decimal = Field(ge=0)
    total_exposure_cny: Decimal = Field(ge=0)
    unknown_components: list[str] = Field(default_factory=list)
    within_call_limit: bool
    within_cost_limit: bool
    payment_approved: bool
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_totals(self) -> "ExecutionBudgetPlan":
        remaining_calls = sum(item.remaining_api_calls for item in self.operations)
        committed_calls = sum(item.committed_api_calls for item in self.operations)
        remaining_cost = sum(
            (item.estimated_cost_cny for item in self.operations if item.status == "planned"),
            start=Decimal("0"),
        ) + self.retry_reserve_cny + self.candidate_reserve_cny
        committed_cost = sum(
            (item.committed_cost_cny for item in self.operations),
            start=Decimal("0"),
        )
        if remaining_calls != self.remaining_api_calls:
            raise ValueError("remaining_api_calls does not match operations")
        if committed_calls != self.committed_api_calls:
            raise ValueError("committed_api_calls does not match operations")
        if self.total_exposure_api_calls != remaining_calls + committed_calls:
            raise ValueError("total_exposure_api_calls is inconsistent")
        if remaining_cost != self.remaining_cost_cny:
            raise ValueError("remaining_cost_cny does not match operations and reserves")
        if committed_cost != self.committed_cost_cny:
            raise ValueError("committed_cost_cny does not match operations")
        if self.total_exposure_cny != remaining_cost + committed_cost:
            raise ValueError("total_exposure_cny is inconsistent")
        expected_approval = (
            not self.unknown_components
            and self.within_call_limit
            and self.within_cost_limit
        )
        if self.payment_approved != expected_approval:
            raise ValueError("payment_approved is inconsistent with budget gates")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("execution budget content_digest mismatch")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "ExecutionBudgetPlan":
        provisional = cls.model_construct(
            **data,
            content_digest="sha256:" + "0" * 64,
        )
        digest = provisional.compute_content_digest()
        return cls.model_validate({**data, "content_digest": digest})

    def _content_payload(self) -> dict:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return payload

    def compute_content_digest(self) -> str:
        canonical = json.dumps(
            self._content_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def to_canonical_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


def _selected_segments(
    plan: GenerationPlanEpisode,
    segment_ids: list[str] | None,
) -> list[GenerationSegment]:
    if segment_ids is None:
        return list(plan.segments)
    requested = set(segment_ids)
    selected = [item for item in plan.segments if item.segment_id in requested]
    missing = sorted(requested - {item.segment_id for item in selected})
    if missing:
        raise ExecutionBudgetError(f"unknown segment IDs: {missing}")
    return selected


def _approved_first_frame(
    bundle: ApprovedAssetBundle | None,
    segment_id: str,
):
    if bundle is None:
        return None
    matches = [
        item
        for item in bundle.assets
        if item.role == "first_frame"
        and item.approval_status == "approved"
        and segment_id in item.required_for_segment_ids
    ]
    if len(matches) > 1:
        raise ExecutionBudgetError(
            f"multiple approved first frames exist for segment {segment_id}"
        )
    return matches[0] if matches else None


def _ledger_records(
    ledgers: list[CanaryProviderLedger],
    segment_id: str,
    component: BudgetComponent,
):
    operation_type = {
        "first_frame": "image",
        "video": "video",
        "tts": "tts",
    }[component]
    return [
        record
        for ledger in ledgers
        if ledger.shot_id == segment_id
        for record in ledger.operations.values()
        if record.operation_type == operation_type and record.consumes_submission
    ]


def _quote_operation(
    *,
    segment: GenerationSegment,
    component: BudgetComponent,
    model: str,
    quantity: Decimal,
    quantity_unit: Literal["image", "second", "character"],
    unit_cost: Decimal,
    approved_asset_id: str | None,
    ledgers: list[CanaryProviderLedger],
) -> ExecutionBudgetOperation:
    committed = _ledger_records(ledgers, segment.segment_id, component)
    if len(committed) > 1:
        raise ExecutionBudgetError(
            f"multiple committed {component} operations exist for {segment.segment_id}"
        )
    estimated = (quantity * unit_cost).quantize(Decimal("0.000000001"))
    if committed:
        record = committed[0]
        return ExecutionBudgetOperation(
            operation_key=f"{segment.segment_id}:{component}",
            segment_id=segment.segment_id,
            component=component,
            model=model,
            status="committed",
            quantity=quantity,
            quantity_unit=quantity_unit,
            unit_cost_cny=unit_cost,
            estimated_cost_cny=estimated,
            remaining_api_calls=0,
            committed_api_calls=1,
            committed_cost_cny=record.estimated_cost_cny,
            ledger_operation_ids=[record.operation_id],
            asset_id=approved_asset_id,
        )
    if component == "first_frame" and approved_asset_id is not None:
        return ExecutionBudgetOperation(
            operation_key=f"{segment.segment_id}:{component}",
            segment_id=segment.segment_id,
            component=component,
            model=model,
            status="satisfied_by_asset",
            quantity=quantity,
            quantity_unit=quantity_unit,
            unit_cost_cny=unit_cost,
            estimated_cost_cny=Decimal("0"),
            remaining_api_calls=0,
            committed_api_calls=0,
            committed_cost_cny=Decimal("0"),
            asset_id=approved_asset_id,
        )
    return ExecutionBudgetOperation(
        operation_key=f"{segment.segment_id}:{component}",
        segment_id=segment.segment_id,
        component=component,
        model=model,
        status="planned",
        quantity=quantity,
        quantity_unit=quantity_unit,
        unit_cost_cny=unit_cost,
        estimated_cost_cny=estimated,
        remaining_api_calls=1,
        committed_api_calls=0,
        committed_cost_cny=Decimal("0"),
        asset_id=approved_asset_id,
    )


def build_execution_budget(
    plan: GenerationPlanEpisode,
    config: LiveProviderConfig,
    *,
    asset_bundle: ApprovedAssetBundle | None = None,
    ledgers: list[CanaryProviderLedger] | None = None,
    segment_ids: list[str] | None = None,
    hard_maximum_calls: int,
    hard_limit_cny: Decimal,
    retry_reserve_cny: Decimal = Decimal("0"),
    candidate_reserve_cny: Decimal = Decimal("0"),
) -> ExecutionBudgetPlan:
    if asset_bundle is not None and asset_bundle.generation_plan_digest != plan.content_digest:
        raise ExecutionBudgetError("asset bundle does not match generation plan")
    if plan.provider_route_id != "wan/i2v":
        raise ExecutionBudgetError(
            f"current execution budget supports wan/i2v, not {plan.provider_route_id}"
        )
    provider = config.dashscope
    selected = _selected_segments(plan, segment_ids)
    ledger_values = list(ledgers or [])
    operations: list[ExecutionBudgetOperation] = []

    for segment in selected:
        frame = _approved_first_frame(asset_bundle, segment.segment_id)
        operations.append(
            _quote_operation(
                segment=segment,
                component="first_frame",
                model=provider.image_model,
                quantity=Decimal("1"),
                quantity_unit="image",
                unit_cost=provider.image_cost_cny,
                approved_asset_id=frame.asset_id if frame is not None else None,
                ledgers=ledger_values,
            )
        )
        operations.append(
            _quote_operation(
                segment=segment,
                component="video",
                model=provider.video_model,
                quantity=Decimal(segment.requested_duration_seconds),
                quantity_unit="second",
                unit_cost=provider.video_cost_cny_per_second,
                approved_asset_id=None,
                ledgers=ledger_values,
            )
        )
        if segment.dialogue_slices:
            character_count = sum(len(item.text) for item in segment.dialogue_slices)
            operations.append(
                _quote_operation(
                    segment=segment,
                    component="tts",
                    model=provider.tts_model,
                    quantity=Decimal(character_count),
                    quantity_unit="character",
                    unit_cost=provider.tts_cost_cny_per_10k_chars / Decimal("10000"),
                    approved_asset_id=None,
                    ledgers=ledger_values,
                )
            )

    remaining_calls = sum(item.remaining_api_calls for item in operations)
    committed_calls = sum(item.committed_api_calls for item in operations)
    remaining_cost = sum(
        (item.estimated_cost_cny for item in operations if item.status == "planned"),
        start=Decimal("0"),
    ) + retry_reserve_cny + candidate_reserve_cny
    committed_cost = sum(
        (item.committed_cost_cny for item in operations),
        start=Decimal("0"),
    )
    exposure_calls = remaining_calls + committed_calls
    exposure_cost = remaining_cost + committed_cost
    within_calls = exposure_calls <= hard_maximum_calls
    within_cost = exposure_cost <= hard_limit_cny
    selected_ids = [item.segment_id for item in selected]
    identity = hashlib.sha256(
        json.dumps(
            {
                "plan": plan.content_digest,
                "segments": selected_ids,
                "price": provider.price_snapshot_date,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]

    return ExecutionBudgetPlan.build_with_digest(
        budget_plan_id=f"execution_budget_{identity}",
        generation_plan_digest=plan.content_digest,
        asset_bundle_digest=(
            asset_bundle.content_digest if asset_bundle is not None else None
        ),
        provider_route_id=plan.provider_route_id,
        price_snapshot_id=f"dashscope-{provider.price_snapshot_date}",
        selected_segment_ids=selected_ids,
        hard_maximum_calls=hard_maximum_calls,
        hard_limit_cny=hard_limit_cny,
        retry_reserve_cny=retry_reserve_cny,
        candidate_reserve_cny=candidate_reserve_cny,
        operations=operations,
        remaining_api_calls=remaining_calls,
        committed_api_calls=committed_calls,
        total_exposure_api_calls=exposure_calls,
        remaining_cost_cny=remaining_cost,
        committed_cost_cny=committed_cost,
        total_exposure_cny=exposure_cost,
        unknown_components=[],
        within_call_limit=within_calls,
        within_cost_limit=within_cost,
        payment_approved=within_calls and within_cost,
    )


def load_ledgers(paths: list[str | Path]) -> list[CanaryProviderLedger]:
    values = []
    for path in paths:
        values.append(
            CanaryProviderLedger.model_validate_json(
                Path(path).read_text(encoding="utf-8")
            )
        )
    return values


def write_execution_budget(path: str | Path, budget: ExecutionBudgetPlan) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(budget.to_canonical_json() + "\n", encoding="utf-8")
