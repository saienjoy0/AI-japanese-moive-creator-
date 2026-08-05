"""Prepare a hash-bound Wan master-reference manifest without provider calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..assets import (
    WanMasterReferenceError,
    build_wan_master_reference_manifest,
    load_bundle,
    write_wan_master_reference_manifest,
)
from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create the approved character/location/prop reference manifest used by "
            "Wan first-frame generation. This command never calls a provider."
        )
    )
    parser.add_argument("--prepared-input", required=True)
    parser.add_argument("--generation-plan", required=True)
    parser.add_argument("--asset-bundle", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--manifest-output", required=True)
    parser.add_argument("--report")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _write_report(payload: dict[str, object], path: str | None, print_report: bool) -> None:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path:
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    if print_report:
        print(content, end="")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prepared = PreparedEpisode.model_validate_json(
            Path(args.prepared_input).read_text(encoding="utf-8")
        )
        plan = GenerationPlanEpisode.model_validate_json(
            Path(args.generation_plan).read_text(encoding="utf-8")
        )
        bundle = load_bundle(args.asset_bundle)
        manifest = build_wan_master_reference_manifest(
            prepared,
            plan,
            bundle,
            segment_id=args.segment_id,
        )
        destination = write_wan_master_reference_manifest(
            manifest,
            args.manifest_output,
        )
    except (OSError, ValidationError, WanMasterReferenceError) as exc:
        payload = {
            "valid": False,
            "stage": "wan_master_reference_preflight",
            "segment_id": args.segment_id,
            "external_api_calls": 0,
            "errors": [str(exc)],
        }
        _write_report(payload, args.report, args.print_report)
        print(f"not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY

    payload = {
        "valid": True,
        "stage": "wan_master_reference_preflight",
        "segment_id": manifest.segment_id,
        "provider_route_id": manifest.provider_route_id,
        "generation_plan_digest": manifest.generation_plan_digest,
        "master_asset_set_digest": manifest.master_asset_set_digest,
        "master_reference_manifest": str(destination),
        "master_reference_manifest_digest": manifest.content_digest,
        "reference_asset_ids": manifest.asset_ids,
        "reference_asset_hashes": manifest.asset_hashes,
        "reference_count": len(manifest.references),
        "external_api_calls": 0,
        "next_action": "generate one Wan first frame with this exact manifest, then review it",
    }
    _write_report(payload, args.report, args.print_report)
    if not args.print_report:
        print(
            f"Wan master-reference preflight: VALID\n"
            f"Segment: {manifest.segment_id}\n"
            f"References: {len(manifest.references)}\n"
            f"Manifest: {destination}\n"
            "External API calls: 0"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
