"""Validate approved assets and voices before paid provider stages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..assets import (
    AssetBundleError,
    assess_asset_readiness,
    load_bundle,
)
from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify hash-bound assets and voices for a generation stage."
    )
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--prepared-input", required=True, type=Path)
    parser.add_argument("--generation-plan", required=True, type=Path)
    parser.add_argument(
        "--stage",
        choices=("preflight", "keyframe", "approve", "render", "full_episode"),
        required=True,
    )
    parser.add_argument("--segment-id", action="append", dest="segment_ids")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--print-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle = load_bundle(args.bundle)
        prepared = PreparedEpisode.model_validate_json(
            args.prepared_input.read_text(encoding="utf-8")
        )
        plan = GenerationPlanEpisode.model_validate_json(
            args.generation_plan.read_text(encoding="utf-8")
        )
        report = assess_asset_readiness(
            bundle,
            prepared,
            plan,
            stage=args.stage,
            segment_ids=args.segment_ids,
        )
    except (OSError, ValidationError, AssetBundleError) as exc:
        print(f"asset validation error: {exc}", file=sys.stderr)
        return 1

    content = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(content, encoding="utf-8")
    if args.print_report:
        print(content, end="")
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
