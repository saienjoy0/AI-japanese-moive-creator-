"""Recalculate the final offline budget after render strategy resolution."""

from __future__ import annotations

from decimal import Decimal

from ..domain import CostPlan
from .models import BudgetSnapshot, ReadinessIssue, RenderIntent, ShotBudgetItem


def build_budget_snapshot(
    cost_plan: CostPlan,
    intents: list[RenderIntent],
) -> tuple[BudgetSnapshot, list[ReadinessIssue]]:
    shot_items = [
        ShotBudgetItem(
            shot_id=intent.shot_id,
            strategy=intent.resolved_strategy,
            primary_cost=intent.estimated_primary_cost,
            retry_cost=intent.reserved_retry_cost,
            total_cost=intent.estimated_total_cost,
        )
        for intent in intents
    ]
    subtotal = sum((item.total_cost for item in shot_items), start=Decimal("0"))
    contingency = (subtotal * cost_plan.contingency_rate).quantize(Decimal("0.01"))
    estimated_total = subtotal + contingency
    within_budget = estimated_total <= cost_plan.budget_limit

    snapshot = BudgetSnapshot(
        currency=cost_plan.currency,
        budget_limit=cost_plan.budget_limit,
        contingency_rate=cost_plan.contingency_rate,
        shot_items=shot_items,
        subtotal=subtotal,
        contingency=contingency,
        estimated_total=estimated_total,
        within_budget=within_budget,
        hard_stop=cost_plan.hard_stop,
    )

    if within_budget:
        return snapshot, []

    severity = "error" if cost_plan.hard_stop else "warning"
    issue = ReadinessIssue(
        code="budget_exceeded",
        severity=severity,
        message=(
            f"resolved estimated cost {estimated_total} exceeds budget "
            f"{cost_plan.budget_limit} {cost_plan.currency.value}"
        ),
        field="cost_plan.budget_limit",
    )
    return snapshot, [issue]
