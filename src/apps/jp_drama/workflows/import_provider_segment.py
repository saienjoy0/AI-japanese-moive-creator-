"""Preflight and approve one provider/operator MP4 as a SegmentArtifact."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from ..generation.models import GenerationPlanEpisode
from ..production.importer import (
    SegmentEvidence,
    SegmentImportApproval,
    SegmentImportError,
    SegmentImportPreflight,
    approve_segment_import,
    inspect_segment_import,
    revalidate_segment_import,
)


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_APPROVAL = 7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect and human-approve one Wan, MiniMax H3, or manual Seedance MP4 "
            "as the common hash-bound SegmentArtifact. No provider calls are made."
        )
    )
    parser.add_argument("--generation-plan", required=True, type=Path)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--input", required=True, type=Path, help="Provider/operator MP4")
    parser.add_argument(
        "--evidence-kind",
        required=True,
        choices=("seedance_operator", "wan_canary", "minimax_h3_canary"),
    )
    parser.add_argument("--provider-report", type=Path)
    parser.add_argument("--provider-ledger", type=Path)
    parser.add_argument("--provider-approval-manifest", type=Path)
    parser.add_argument("--operator-notes")
    parser.add_argument(
        "--stage",
        choices=("preflight", "approve"),
        default="preflight",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stored-preflight", type=Path)
    parser.add_argument("--preflight-digest")
    parser.add_argument("--approved-by")
    parser.add_argument("--approval-note")
    parser.add_argument("--max-black-seconds", type=float, default=0.25)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_plan(path: Path) -> GenerationPlanEpisode:
    return GenerationPlanEpisode.model_validate_json(path.read_text(encoding="utf-8"))


def _evidence(args: argparse.Namespace) -> SegmentEvidence:
    return SegmentEvidence(
        kind=args.evidence_kind,
        report_path=(
            str(args.provider_report.resolve()) if args.provider_report else None
        ),
        ledger_path=(
            str(args.provider_ledger.resolve()) if args.provider_ledger else None
        ),
        approval_manifest_path=(
            str(args.provider_approval_manifest.resolve())
            if args.provider_approval_manifest
            else None
        ),
        operator_notes=args.operator_notes,
    )


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_black_seconds < 0 or args.max_black_seconds > 5:
        print(
            "input error: --max-black-seconds must be between 0 and 5",
            file=sys.stderr,
        )
        return EXIT_INPUT
    try:
        plan = _load_plan(args.generation_plan)
        evidence = _evidence(args)
        current = inspect_segment_import(
            plan,
            segment_id=args.segment_id,
            output_path=args.input.resolve(),
            evidence=evidence,
            max_black_seconds=args.max_black_seconds,
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

    output_dir = args.output_dir.resolve()
    preflight_path = output_dir / f"{args.segment_id}.import.preflight.json"
    approval_path = output_dir / f"{args.segment_id}.import.approval.json"
    artifact_path = output_dir / f"{args.segment_id}.segment_artifact.json"

    if args.stage == "preflight":
        try:
            _atomic_write(
                preflight_path,
                current.to_canonical_json(),
                overwrite=args.overwrite,
            )
        except OSError as exc:
            print(f"input error: {exc}", file=sys.stderr)
            return EXIT_INPUT
        payload = current.model_dump(mode="json", exclude_none=True)
        payload["preflight_file"] = str(preflight_path)
        payload["next_action"] = (
            "Review the MP4, then run --stage approve with --stored-preflight, "
            "the exact --preflight-digest, and --approved-by."
            if current.valid
            else "Correct the media or evidence and rerun preflight."
        )
        if args.print_report:
            print(_json(payload), end="")
        else:
            print(
                f"Segment: {current.segment_id}\n"
                f"Route: {current.provider_route_id}\n"
                f"Evidence: {current.evidence_kind}\n"
                f"MP4 SHA: {current.media.output_sha256}\n"
                f"Duration: {current.media.duration_seconds:.3f}s\n"
                f"Preflight digest: {current.content_digest}\n"
                "External API calls: 0\n"
                f"Valid: {'YES' if current.valid else 'NO'}"
            )
        return EXIT_OK if current.valid else EXIT_NOT_READY

    if not args.stored_preflight or not args.preflight_digest or not args.approved_by:
        print(
            "approval error: approve requires --stored-preflight, "
            "--preflight-digest, and --approved-by",
            file=sys.stderr,
        )
        return EXIT_APPROVAL
    try:
        stored = SegmentImportPreflight.model_validate_json(
            args.stored_preflight.read_text(encoding="utf-8")
        )
        if stored.content_digest != args.preflight_digest:
            raise SegmentImportError("supplied preflight digest does not match stored preflight")
        if stored.content_digest != current.content_digest:
            raise SegmentImportError("current media/evidence differs from stored preflight")
        revalidate_segment_import(
            plan,
            stored,
            evidence=evidence,
            max_black_seconds=args.max_black_seconds,
        )
        approval, artifact = approve_segment_import(
            plan,
            stored,
            approved_by=args.approved_by,
            approval_note=args.approval_note,
        )
        _atomic_write(
            approval_path,
            approval.to_canonical_json(),
            overwrite=args.overwrite,
        )
        _atomic_write(
            artifact_path,
            _json(artifact.model_dump(mode="json", exclude_none=True)),
            overwrite=args.overwrite,
        )
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        SegmentImportError,
    ) as exc:
        print(f"approval error: {exc}", file=sys.stderr)
        return EXIT_APPROVAL

    payload = {
        "valid": True,
        "stage": "approve",
        "segment_id": artifact.segment_id,
        "provider_route_id": artifact.provider_route_id,
        "approval_file": str(approval_path),
        "approval_digest": approval.content_digest,
        "artifact_file": str(artifact_path),
        "artifact_sha256": artifact.output_sha256,
        "external_api_calls": 0,
    }
    if args.print_report:
        print(_json(payload), end="")
    else:
        print(
            f"Segment: {artifact.segment_id}\n"
            f"Approval digest: {approval.content_digest}\n"
            f"Artifact: {artifact_path}\n"
            "External API calls: 0\n"
            "Status: APPROVED"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
