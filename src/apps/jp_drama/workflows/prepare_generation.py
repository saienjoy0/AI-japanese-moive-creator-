"""CLI for deterministic, zero-paid-call adaptive generation planning."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..generation import (
    GenerationCompilationError,
    ProviderSegmentationProfile,
    compile_generation_plan,
    write_generation_artifacts,
)
from ..preparation.models import PreparedEpisode
from ..rendering.provider_config import LiveProviderConfig
from ..rendering.provider_registry import (
    MockProviderAdapter,
    ProviderRegistry,
    build_default_provider_registry,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile PreparedEpisode into deterministic adaptive generation segments "
            "without submitting provider jobs."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--live-provider-config", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    parser.add_argument("--require-route-ready", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prepared = PreparedEpisode.model_validate_json(
            args.input.read_text(encoding="utf-8")
        )
        profile = ProviderSegmentationProfile.load(args.profile)
        registry = _build_registry(profile, args.live_provider_config)
        plan = compile_generation_plan(
            prepared,
            profile=profile,
            registry=registry,
        )
        paths = write_generation_artifacts(
            plan,
            args.output_dir,
            overwrite=args.overwrite,
        )
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        GenerationCompilationError,
    ) as exc:
        print(f"generation planning error: {exc}", file=sys.stderr)
        return 1

    if args.print_report:
        print(paths["summary.txt"].read_text(encoding="utf-8"), end="")
    if not plan.readiness_report.planning_ready:
        return 2
    if args.require_route_ready and not plan.readiness_report.execution_route_ready:
        return 3
    return 0


def _build_registry(
    profile: ProviderSegmentationProfile,
    live_provider_config: Path | None,
) -> ProviderRegistry:
    if profile.route_id == "mock/video":
        registry = ProviderRegistry()
        registry.register(MockProviderAdapter())
        return registry
    if live_provider_config is None:
        raise ValueError(
            "--live-provider-config is required for non-mock provider profiles"
        )
    config = LiveProviderConfig.load(live_provider_config)
    return build_default_provider_registry(config)


if __name__ == "__main__":
    raise SystemExit(main())
