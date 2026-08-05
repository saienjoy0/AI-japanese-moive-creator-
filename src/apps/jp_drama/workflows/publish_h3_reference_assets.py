"""Preflight, publish, and materialize approved H3 reference images."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from ..assets import load_bundle
from ..assets.publication import (
    H3AssetPublicationError,
    H3AssetPublicationPreflight,
    H3PublishedAssetManifest,
    OSSH3AssetPublisher,
    build_h3_asset_publication_preflight_for_episode,
    materialize_h3_canary_asset_manifest,
    publish_h3_assets,
)
from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_APPROVAL = 7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Bind approved local PNG masters to deterministic private OSS objects and "
            "short-lived HTTPS URLs for one MiniMax H3 segment. Preflight and "
            "materialize make zero storage calls."
        )
    )
    parser.add_argument(
        "--stage",
        choices=("preflight", "publish", "materialize"),
        default="preflight",
    )
    parser.add_argument("--prepared-input", type=Path)
    parser.add_argument("--generation-plan", type=Path)
    parser.add_argument("--asset-bundle", type=Path)
    parser.add_argument("--segment-id")
    parser.add_argument("--stored-preflight", type=Path)
    parser.add_argument("--published-manifest", type=Path)
    parser.add_argument("--approval-digest")
    parser.add_argument("--execute-upload", action="store_true")
    parser.add_argument("--expires-seconds", type=int, default=1800)
    parser.add_argument("--minimum-remaining-seconds", type=int, default=300)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _atomic_write(path: Path, content: str, *, overwrite: bool) -> None:
    path = path.resolve()
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    ) + "\n"


def _load_preflight(path: Path) -> H3AssetPublicationPreflight:
    return H3AssetPublicationPreflight.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve()

    if args.stage == "preflight":
        if not all(
            [
                args.prepared_input,
                args.generation_plan,
                args.asset_bundle,
                args.segment_id,
            ]
        ):
            print(
                "input error: preflight requires --prepared-input, --generation-plan, "
                "--asset-bundle, and --segment-id",
                file=sys.stderr,
            )
            return EXIT_INPUT
        try:
            prepared = PreparedEpisode.model_validate_json(
                args.prepared_input.read_text(encoding="utf-8")
            )
            plan = GenerationPlanEpisode.model_validate_json(
                args.generation_plan.read_text(encoding="utf-8")
            )
            bundle = load_bundle(args.asset_bundle)
            preflight = build_h3_asset_publication_preflight_for_episode(
                prepared,
                plan,
                bundle,
                segment_id=args.segment_id,
            )
            path = output_dir / f"{args.segment_id}.h3-assets.preflight.json"
            _atomic_write(
                path,
                preflight.to_canonical_json(),
                overwrite=args.overwrite,
            )
        except (
            OSError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            H3AssetPublicationError,
        ) as exc:
            print(f"input error: {exc}", file=sys.stderr)
            return EXIT_NOT_READY
        report = {
            "valid": True,
            "stage": "preflight",
            "segment_id": preflight.segment_id,
            "provider_route_id": preflight.provider_route_id,
            "asset_count": len(preflight.items),
            "asset_hashes": [item.local_sha256 for item in preflight.items],
            "object_relative_paths": [
                item.object_relative_path for item in preflight.items
            ],
            "preflight_file": str(path),
            "approval_digest": preflight.content_digest,
            "external_storage_calls": 0,
            "external_provider_calls": 0,
            "next_action": (
                "Review the exact assets and object paths, then run --stage publish "
                "with --execute-upload and this exact --approval-digest."
            ),
        }
        if args.print_report:
            print(_json(report), end="")
        else:
            print(
                f"Segment: {preflight.segment_id}\n"
                f"Assets: {len(preflight.items)}\n"
                f"Approval digest: {preflight.content_digest}\n"
                "External storage calls: 0\n"
                "External provider calls: 0\n"
                "Preflight: VALID"
            )
        return EXIT_OK

    if args.stored_preflight is None:
        print(
            "input error: publish/materialize requires --stored-preflight",
            file=sys.stderr,
        )
        return EXIT_INPUT
    try:
        preflight = _load_preflight(args.stored_preflight)
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if args.stage == "publish":
        if not args.execute_upload or not args.approval_digest:
            print(
                "approval error: publish requires --execute-upload and the exact "
                "--approval-digest",
                file=sys.stderr,
            )
            return EXIT_APPROVAL
        try:
            published = publish_h3_assets(
                preflight,
                approval_digest=args.approval_digest,
                execute_upload=args.execute_upload,
                publisher=OSSH3AssetPublisher(),
                expires_seconds=args.expires_seconds,
            )
            path = output_dir / f"{preflight.segment_id}.h3-assets.published.json"
            _atomic_write(
                path,
                published.to_canonical_json(),
                overwrite=args.overwrite,
            )
        except (
            OSError,
            ValidationError,
            ValueError,
            H3AssetPublicationError,
        ) as exc:
            print(f"publication error: {exc}", file=sys.stderr)
            return EXIT_APPROVAL
        report = {
            "valid": True,
            "stage": "publish",
            "segment_id": published.segment_id,
            "published_manifest": str(path),
            "published_manifest_digest": published.content_digest,
            "external_storage_uploads": published.external_storage_uploads,
            "external_storage_signatures": published.external_storage_signatures,
            "external_provider_calls": 0,
            "next_action": (
                "Immediately materialize the H3 canary asset manifest before the "
                "short-lived signed URLs approach expiry."
            ),
        }
        if args.print_report:
            print(_json(report), end="")
        else:
            print(
                f"Segment: {published.segment_id}\n"
                f"Uploads: {published.external_storage_uploads}\n"
                f"Signed URLs: {published.external_storage_signatures}\n"
                f"Published manifest: {path}\n"
                "External provider calls: 0\n"
                "Status: PUBLISHED"
            )
        return EXIT_OK

    if args.published_manifest is None:
        print(
            "input error: materialize requires --published-manifest",
            file=sys.stderr,
        )
        return EXIT_INPUT
    try:
        published = H3PublishedAssetManifest.model_validate_json(
            args.published_manifest.read_text(encoding="utf-8")
        )
        h3_manifest = materialize_h3_canary_asset_manifest(
            preflight,
            published,
            minimum_remaining_seconds=args.minimum_remaining_seconds,
        )
        path = output_dir / f"{preflight.segment_id}.h3-canary-assets.json"
        _atomic_write(
            path,
            _json(h3_manifest.model_dump(mode="json", exclude_none=True)),
            overwrite=args.overwrite,
        )
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        H3AssetPublicationError,
    ) as exc:
        print(f"materialization error: {exc}", file=sys.stderr)
        return EXIT_NOT_READY
    report = {
        "valid": True,
        "stage": "materialize",
        "segment_id": h3_manifest.segment_id,
        "asset_count": len(h3_manifest.assets),
        "h3_asset_manifest": str(path),
        "external_storage_calls": 0,
        "external_provider_calls": 0,
    }
    if args.print_report:
        print(_json(report), end="")
    else:
        print(
            f"Segment: {h3_manifest.segment_id}\n"
            f"Assets: {len(h3_manifest.assets)}\n"
            f"H3 manifest: {path}\n"
            "External storage calls: 0\n"
            "External provider calls: 0\n"
            "Status: READY"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
