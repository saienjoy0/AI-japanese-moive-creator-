"""Apply hash-bound image and distinct voice approvals to an asset bundle."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from ..assets import (
    AssetBundleError,
    apply_asset_approvals,
    load_bindings,
    load_bundle,
    write_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Approve supplied reference files and character voice identities."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--bindings", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--approved-at")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-summary", action="store_true")
    return parser


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output.exists() and not args.overwrite:
            raise AssetBundleError(
                "output already exists; pass --overwrite to replace it"
            )
        bundle = load_bundle(args.input)
        bindings = load_bindings(args.bindings)
        approved = apply_asset_approvals(
            bundle,
            bindings,
            approved_at=_timestamp(args.approved_at),
        )
        write_bundle(args.output, approved)
    except (OSError, ValueError, ValidationError, AssetBundleError) as exc:
        print(f"asset approval error: {exc}", file=sys.stderr)
        return 1

    if args.print_summary:
        approved_assets = sum(
            item.approval_status == "approved" for item in approved.assets
        )
        approved_voices = sum(
            item.approval_status == "approved"
            for item in approved.voice_profiles
        )
        print(f"Bundle: {approved.bundle_id}")
        print(f"Approved assets: {approved_assets}/{len(approved.assets)}")
        print(
            f"Approved voices: {approved_voices}/{len(approved.voice_profiles)}"
        )
        print(f"Digest: {approved.content_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
