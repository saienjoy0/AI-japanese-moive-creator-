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
from ..rendering.canary_tasks import Wan27LiveTaskExecutor
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


def _segment_metadata(
    *,
    segment,
    plan: GenerationPlanEpisode,
    materialized: PreparedEpisode,
    materialized_path: Path,
    allow_experimental_multi_shot: bool,
) -> dict[str, object]:
    return {
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
            len(segment.editorial_shots) > 1 and allow_experimental_multi_shot
        ),
        "materialized_prepared_episode": str(materialized_path),
        "materialized_source_digest": materialized.source_digest,
    }


def _write_enriched_report(
    payload: dict[str, object],
    *,
    report_path: str | None,
    print_report: bool,
) -> str:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    if report_path:
        _atomic_write(Path(report_path), content)
    if print_report:
        print(content, end="")
    return content


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

    approved_shots = {segment.segment_id}
    planned_keyframe_calls = 1
    planned_keyframe_cost = config.dashscope.estimate_image_cost_cny()
    planned_render_calls = Wan27LiveTaskExecutor.estimate_api_calls(
        materialized,
        approved_keyframe_shots=approved_shots,
    )
    planned_render_cost = Wan27LiveTaskExecutor.estimate_cost_cny(
        materialized,
        config,
        approved_keyframe_shots=approved_shots,
    )
    planned_api_calls = planned_keyframe_calls + planned_render_calls
    planned_cost_cny = planned_keyframe_cost + planned_render_cost
    within_call_limit = 0 <= args.max_api_calls and planned_api_calls <= args.max_api_calls
    within_cost_limit = Decimal("0") <= args.max_cost_cny and planned_cost_cny <= args.max_cost_cny

    common_metadata = _segment_metadata(
        segment=segment,
        plan=plan,
        materialized=materialized,
        materialized_path=materialized_path,
        allow_experimental_multi_shot=args.allow_experimental_multi_shot,
    )
    budget_metadata: dict[str, object] = {
        "planned_keyframe_calls": planned_keyframe_calls,
        "planned_render_calls": planned_render_calls,
        "planned_api_calls": planned_api_calls,
        "planned_keyframe_cost_cny": str(planned_keyframe_cost),
        "planned_render_cost_cny": str(planned_render_cost),
        "planned_cost_cny": str(planned_cost_cny),
        "max_api_calls": args.max_api_calls,
        "max_cost_cny": str(args.max_cost_cny),
        "within_requested_call_limit": within_call_limit,
        "within_requested_cost_limit": within_cost_limit,
    }
    if not within_call_limit or not within_cost_limit:
        failure = {
            "valid": False,
            "stage": args.stage,
            "credentials_present": False,
            "external_api_calls": 0,
            "budget_gate": "blocked_before_provider_submission",
            "errors": [
                message
                for condition, message in (
                    (
                        not within_call_limit,
                        f"planned {planned_api_calls} API calls exceed limit {args.max_api_calls}",
                    ),
                    (
                        not within_cost_limit,
                        f"planned {planned_cost_cny} CNY exceeds limit {args.max_cost_cny} CNY",
                    ),
                )
                if condition
            ],
            **common_metadata,
            **budget_metadata,
        }
        _write_enriched_report(
            failure,
            report_path=args.report,
            print_report=args.print_report,
        )
        print(
            "provider error: segment Canary budget gate blocked all provider submissions: "
            + "; ".join(failure["errors"]),
            file=sys.stderr,
        )
        return EXIT_PROVIDER

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

    payload.update(common_metadata)
    payload.update(budget_metadata)
    _write_enriched_report(
        payload,
        report_path=args.report,
        print_report=args.print_report,
    )
    if not args.print_report:
        print(
            f"Segment: {segment.segment_id}\n"
            f"Stage: {args.stage}\n"
            f"Route: {segment.provider_route_id}\n"
            f"Provider request: {segment.requested_duration_seconds}s\n"
            f"Editorial duration: {segment.editorial_duration_seconds}s\n"
            f"Planned budget: {planned_api_calls} calls / {planned_cost_cny} CNY\n"
            f"Materialized input: {materialized_path}\n"
            f"External API calls this stage: {payload.get('external_api_calls', 0)}\n"
            "Status: OK"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
