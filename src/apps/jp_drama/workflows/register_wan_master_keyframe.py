"""Register a reviewed Wan first frame into the approved asset bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..assets import (
    WanFirstFrameError,
    load_bundle,
    load_wan_master_reference_manifest,
    register_wan_first_frame,
    verify_wan_first_frame_ready,
    write_bundle,
)
from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a human-approved Wan first frame against its exact master "
            "references and write an updated ApprovedAssetBundle. No provider calls."
        )
    )
    parser.add_argument("--prepared-input", required=True)
    parser.add_argument("--generation-plan", required=True)
    parser.add_argument("--asset-bundle", required=True)
    parser.add_argument("--master-reference-manifest", required=True)
    parser.add_argument("--approval-manifest", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--output-bundle", required=True)
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
        master_manifest = load_wan_master_reference_manifest(
            args.master_reference_manifest
        )
        updated = register_wan_first_frame(
            bundle,
            prepared,
            plan,
            master_manifest,
            segment_id=args.segment_id,
            approval_manifest_path=args.approval_manifest,
            approved_by=args.approved_by,
        )
        first_frame, keyframe = verify_wan_first_frame_ready(
            updated,
            prepared,
            plan,
            master_manifest,
            segment_id=args.segment_id,
        )
        output = Path(args.output_bundle).resolve()
        write_bundle(output, updated)
    except (
        OSError,
        ValidationError,
        WanFirstFrameError,
        RuntimeError,
    ) as exc:
        payload = {
            "valid": False,
            "stage": "wan_first_frame_registration",
            "segment_id": args.segment_id,
            "external_api_calls": 0,
            "errors": [str(exc)],
        }
        _write_report(payload, args.report, args.print_report)
        print(f"not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY

    payload = {
        "valid": True,
        "stage": "wan_first_frame_registration",
        "segment_id": args.segment_id,
        "master_reference_manifest_digest": master_manifest.content_digest,
        "verified_against_asset_ids": master_manifest.asset_ids,
        "first_frame_asset_id": first_frame.asset_id,
        "first_frame_path": str(keyframe),
        "first_frame_sha256": first_frame.asset_sha256,
        "updated_asset_bundle": str(output),
        "updated_asset_bundle_digest": updated.content_digest,
        "approved_by": args.approved_by,
        "external_api_calls": 0,
        "next_action": "run the Wan video preflight with this updated bundle",
    }
    _write_report(payload, args.report, args.print_report)
    if not args.print_report:
        print(
            f"Wan first-frame registration: VALID\n"
            f"Segment: {args.segment_id}\n"
            f"First frame: {keyframe}\n"
            f"Updated bundle: {output}\n"
            "External API calls: 0"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
