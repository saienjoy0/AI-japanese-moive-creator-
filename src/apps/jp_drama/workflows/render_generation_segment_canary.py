"""Run one PR11 GenerationSegment through the PR8 Wan canary safely."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode
from ..rendering.provider_config import LiveProviderConfig, ProviderConfigurationError
from ..rendering.segment_canary import (
    SEGMENT_CANARY_PROTOCOL,
    SegmentCanaryError,
    find_generation_segment,
    materialize_generation_segment_canary,
)
from .render_canary_episode import main as render_canary_main


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_PROVIDER = 6


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize one adaptive GenerationSegment and delegate execution to the "
            "restart-safe, approval-gated Wan 2.7 canary."
        )
    )
    parser.add_argument("--prepared-input", required=True, help="PreparedEpisode JSON")
    parser.add_argument("--generation-plan", required=True, help="GenerationPlanEpisode JSON")
    parser.add_argument("--segment-id", required=True, help="Exactly one generation segment ID")
    parser.add_argument("--output", required=True, help="Final one-segment Canary MP4")
    parser.add_argument("--providers", required=True, help="Live provider configuration JSON")
    parser.add_argument(
        "--stage",
        choices=("preflight", "keyframe", "approve", "render"),
        default="preflight",
    )
    parser.add_argument("--approved-keyframe")
    parser.add_argument("--approval-manifest")
    parser.add_argument("--keyframe-output")
    parser.add_argument("--ledger-file")
    parser.add_argument("--work-dir")
    parser.add_argument("--projects-file")
    parser.add_argument("--index-file")
    parser.add_argument("--materialized-prepared")
    parser.add_argument("--report")
    parser.add_argument("--max-api-calls", type=int, default=3)
    parser.add_argument("--max-cost-cny", type=_decimal, default=Decimal("5.0"))
    parser.add_argument(
        "--allow-experimental-multi-shot",
        action="store_true",
        help=(
            "Explicitly permit a segment containing multiple editorial shots. This is "
            "experimental because the current Wan route does not claim multi-shot support."
        ),
    )
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_prepared(path: Path) -> PreparedEpisode:
    return PreparedEpisode.model_validate_json(path.read_text(encoding="utf-8"))


def _load_plan(path: Path) -> GenerationPlanEpisode:
    return GenerationPlanEpisode.model_validate_json(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _materialized_path(output: Path, segment_id: str) -> Path:
    return output.parent / f".{output.stem}_{segment_id}_prepared.json"


def _delegate_report_path(output: Path, segment_id: str) -> Path:
    return output.parent / f".{output.stem}_{segment_id}_delegate_report.json"


def _append_optional(arguments: list[str], flag: str, value: str | None) -> None:
    if value:
        arguments.extend([flag, value])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prepared_path = Path(args.prepared_input)
    plan_path = Path(args.generation_plan)
    output_path = Path(args.output)

    try:
        prepared = _load_prepared(prepared_path)
        plan = _load_plan(plan_path)
        config = LiveProviderConfig.load(args.providers)
        segment = find_generation_segment(plan, args.segment_id)
        materialized = materialize_generation_segment_canary(
            prepared,
            plan,
            args.segment_id,
            provider_clip_seconds=config.dashscope.provider_clip_seconds,
            allow_experimental_multi_shot=args.allow_experimental_multi_shot,
        )
    except (OSError, ValidationError, json.JSONDecodeError, SegmentCanaryError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except ProviderConfigurationError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    materialized_path = (
        Path(args.materialized_prepared)
        if args.materialized_prepared
        else _materialized_path(output_path, args.segment_id)
    ).resolve()
    _atomic_write(materialized_path, materialized.to_canonical_json() + "\n")

    delegate_report = _delegate_report_path(output_path, args.segment_id).resolve()
    delegated = [
        "--input",
        str(materialized_path),
        "--output",
        str(output_path),
        "--providers",
        str(args.providers),
        "--shot-id",
        args.segment_id,
        "--stage",
        args.stage,
        "--max-api-calls",
        str(args.max_api_calls),
        "--max-cost-cny",
        str(args.max_cost_cny),
        "--report",
        str(delegate_report),
        "--print-report",
    ]
    _append_optional(delegated, "--approved-keyframe", args.approved_keyframe)
    _append_optional(delegated, "--approval-manifest", args.approval_manifest)
    _append_optional(delegated, "--keyframe-output", args.keyframe_output)
    _append_optional(delegated, "--ledger-file", args.ledger_file)
    _append_optional(delegated, "--work-dir", args.work_dir)
    _append_optional(delegated, "--projects-file", args.projects_file)
    _append_optional(delegated, "--index-file", args.index_file)
    if args.reset:
        delegated.append("--reset")

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        exit_code = render_canary_main(delegated)
    if exit_code != EXIT_OK:
        return exit_code

    try:
        payload = json.loads(delegate_report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"provider error: cannot read delegated report: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    payload.update(
        {
            "canary_protocol": SEGMENT_CANARY_PROTOCOL,
            "segment_id": segment.segment_id,
            "source_shot_ids": segment.parent_shot_ids,
            "generation_plan_episode_id": plan.generation_plan_episode_id,
            "generation_plan_digest": plan.content_digest,
            "provider_route_id": segment.provider_route_id,
            "requested_duration_seconds": segment.requested_duration_seconds,
            "editorial_duration_seconds": str(segment.editorial_duration_seconds),
            "editorial_frame_count": segment.editorial_frame_count,
            "used_start_frame": segment.used_start_frame,
            "used_end_frame": segment.used_end_frame,
            "timeline_fps": segment.timeline_fps,
            "editorial_shot_count": len(segment.editorial_shots),
            "experimental_multi_shot": (
                len(segment.editorial_shots) > 1
                and args.allow_experimental_multi_shot
            ),
            "materialized_prepared_episode": str(materialized_path),
            "materialized_source_digest": materialized.source_digest,
        }
    )
    report_content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    if args.report:
        _atomic_write(Path(args.report), report_content)
    if args.print_report:
        print(report_content, end="")
    else:
        print(
            f"Segment: {segment.segment_id}\n"
            f"Stage: {args.stage}\n"
            f"Route: {segment.provider_route_id}\n"
            f"Provider request: {segment.requested_duration_seconds}s\n"
            f"Editorial duration: {segment.editorial_duration_seconds}s\n"
            f"Materialized input: {materialized_path}\n"
            f"External API calls this stage: {payload.get('external_api_calls', 0)}\n"
            "Status: OK"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
