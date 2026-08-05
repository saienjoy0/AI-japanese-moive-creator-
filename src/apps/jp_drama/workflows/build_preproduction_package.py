"""Build a portable zero-call package immediately before media generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..preproduction import PreproductionPackageError, build_preproduction_package


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_BUILD = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build asset, voice, first-frame, provider, and Canary preparation "
            "artifacts from an imported multi-episode production contract."
        )
    )
    parser.add_argument("--series-output", required=True)
    parser.add_argument("--source-series-plan", required=True)
    parser.add_argument("--source-asset-catalog", required=True)
    parser.add_argument("--live-provider-config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = build_preproduction_package(
            series_output=args.series_output,
            source_series_plan=args.source_series_plan,
            source_asset_catalog=args.source_asset_catalog,
            live_provider_config=args.live_provider_config,
            output_dir=args.output_dir,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, ValidationError, PreproductionPackageError) as exc:
        print(f"preproduction build error: {exc}", file=sys.stderr)
        return EXIT_BUILD

    payload = {
        "valid": True,
        "package_id": manifest.package_id,
        "title": manifest.title,
        "episode_count": manifest.episode_count,
        "segment_count": manifest.segment_count,
        "base_master_asset_count": manifest.base_master_asset_count,
        "variant_review_asset_count": manifest.variant_review_asset_count,
        "voice_identity_count": manifest.voice_identity_count,
        "first_frame_count": manifest.first_frame_count,
        "provider_route_count": manifest.provider_route_count,
        "contract_ready": manifest.contract_ready,
        "provider_plans_ready": manifest.provider_plans_ready,
        "master_assets_ready": manifest.master_assets_ready,
        "voices_ready": manifest.voices_ready,
        "first_frames_ready": manifest.first_frames_ready,
        "video_generation_ready": manifest.video_generation_ready,
        "blocker_codes": [item.code for item in manifest.blockers],
        "content_digest": manifest.content_digest,
        "output_dir": str(Path(args.output_dir).resolve()),
        "external_api_calls": 0,
    }
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.print_report:
        print(content, end="")
    else:
        print(
            f"Preproduction package: {manifest.package_id}\n"
            f"Episodes / segments: {manifest.episode_count} / {manifest.segment_count}\n"
            f"Master assets: {manifest.base_master_asset_count}\n"
            f"Voices: {manifest.voice_identity_count}\n"
            f"First frames: {manifest.first_frame_count}\n"
            f"Output: {Path(args.output_dir).resolve()}\n"
            "External API calls: 0\n"
            "Video generation ready: NO"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
