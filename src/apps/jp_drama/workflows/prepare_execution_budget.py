"""Build one provider-bound, ledger-aware CNY execution budget."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from ..assets import load_bundle
from ..generation import (
    ExecutionBudgetError,
    GenerationPlanEpisode,
    build_execution_budget,
    load_ledgers,
    write_execution_budget,
)
from ..rendering.provider_config import LiveProviderConfig, ProviderConfigurationError


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Calculate remaining and committed provider exposure after asset approvals "
            "and persistent ledger records."
        )
    )
    parser.add_argument("--generation-plan", required=True, type=Path)
    parser.add_argument("--providers", required=True, type=Path)
    parser.add_argument("--asset-bundle", type=Path)
    parser.add_argument("--ledger", action="append", default=[])
    parser.add_argument("--segment-id", action="append", dest="segment_ids")
    parser.add_argument("--hard-max-calls", required=True, type=int)
    parser.add_argument("--hard-limit-cny", required=True, type=_decimal)
    parser.add_argument("--retry-reserve-cny", type=_decimal, default=Decimal("0"))
    parser.add_argument("--candidate-reserve-cny", type=_decimal, default=Decimal("0"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output.exists() and not args.overwrite:
            raise ExecutionBudgetError(
                "output already exists; pass --overwrite to replace it"
            )
        plan = GenerationPlanEpisode.model_validate_json(
            args.generation_plan.read_text(encoding="utf-8")
        )
        config = LiveProviderConfig.load(args.providers)
        bundle = load_bundle(args.asset_bundle) if args.asset_bundle else None
        ledgers = load_ledgers([Path(item) for item in args.ledger])
        budget = build_execution_budget(
            plan,
            config,
            asset_bundle=bundle,
            ledgers=ledgers,
            segment_ids=args.segment_ids,
            hard_maximum_calls=args.hard_max_calls,
            hard_limit_cny=args.hard_limit_cny,
            retry_reserve_cny=args.retry_reserve_cny,
            candidate_reserve_cny=args.candidate_reserve_cny,
        )
        write_execution_budget(args.output, budget)
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ProviderConfigurationError,
        ExecutionBudgetError,
    ) as exc:
        print(f"execution budget error: {exc}", file=sys.stderr)
        return 1

    if args.print_report:
        print(budget.to_canonical_json())
    return 0 if budget.payment_approved else 2


if __name__ == "__main__":
    raise SystemExit(main())
