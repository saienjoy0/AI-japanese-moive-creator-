"""Assemble approved SegmentArtifact files into one plan-bound manifest."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from ..generation.models import GenerationPlanEpisode
from ..production import SegmentArtifact
from ..production.importer import SegmentImportError, build_artifact_manifest


EXIT_OK = 0
EXIT_INPUT = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a complete ordered SegmentArtifactManifest for one GenerationPlan. "
            "The repeated --artifact order must exactly match the plan."
        )
    )
    parser.add_argument("--generation-plan", required=True, type=Path)
    parser.add_argument("--artifact", required=True, action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = GenerationPlanEpisode.model_validate_json(
            args.generation_plan.read_text(encoding="utf-8")
        )
        artifacts = [
            SegmentArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            for path in args.artifact
        ]
        manifest = build_artifact_manifest(plan, artifacts)
        _atomic_write(
            args.output,
            manifest.to_canonical_json(),
            overwrite=args.overwrite,
        )
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        SegmentImportError,
    ) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    report = {
        "valid": True,
        "generation_plan_digest": plan.content_digest,
        "segment_count": len(artifacts),
        "segment_order": [item.segment_id for item in artifacts],
        "manifest_file": str(args.output.resolve()),
        "manifest_digest": manifest.content_digest,
        "external_api_calls": 0,
    }
    if args.print_report:
        print(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            end="",
        )
    else:
        print(
            f"Segments: {len(artifacts)}\n"
            f"Manifest: {args.output.resolve()}\n"
            f"Digest: {manifest.content_digest}\n"
            "External API calls: 0\n"
            "Status: VALID"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
