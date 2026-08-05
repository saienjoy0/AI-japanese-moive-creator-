"""Create a deterministic pending reference-asset and voice bundle."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from ..assets import (
    AssetBundleError,
    build_pending_asset_bundle,
    write_bundle,
)
from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the pending character, location, prop, first-frame, and voice "
            "approval bundle for a GenerationPlan."
        )
    )
    parser.add_argument("--prepared-input", required=True, type=Path)
    parser.add_argument("--generation-plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-summary", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output.exists() and not args.overwrite:
            raise AssetBundleError(
                "output already exists; pass --overwrite to replace it"
            )
        prepared = PreparedEpisode.model_validate_json(
            args.prepared_input.read_text(encoding="utf-8")
        )
        plan = GenerationPlanEpisode.model_validate_json(
            args.generation_plan.read_text(encoding="utf-8")
        )
        bundle = build_pending_asset_bundle(prepared, plan)
        write_bundle(args.output, bundle)
    except (OSError, ValidationError, AssetBundleError) as exc:
        print(f"asset bundle error: {exc}", file=sys.stderr)
        return 1

    if args.print_summary:
        role_counts: dict[str, int] = {}
        for asset in bundle.assets:
            role_counts[asset.role] = role_counts.get(asset.role, 0) + 1
        print(f"Bundle: {bundle.bundle_id}")
        print(f"Plan: {bundle.generation_plan_digest}")
        print(f"Assets: {len(bundle.assets)}")
        for role in sorted(role_counts):
            print(f"  {role}: {role_counts[role]}")
        print(f"Voice profiles: {len(bundle.voice_profiles)}")
        print("Approved assets: 0")
        print("Status: pending approvals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
