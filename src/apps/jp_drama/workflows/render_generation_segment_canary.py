"""Run one safe GenerationSegment through the approval-gated Wan canary."""

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

from ..assets import (
    AssetBundleError,
    assess_asset_readiness,
    load_bundle,
)
from ..generation import CandidateSelectionError, select_safe_canary_candidate
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
            "Select one safe single-shot GenerationSegment and delegate execution "
            "to the restart-safe Wan 2.7 canary. Paid stages require an approved "
            "reference-asset and voice bundle."
        )
    )
    parser.add_argument("--prepared-input", required=True, help="PreparedEpisode JSON")
    parser.add_argument("--generation-plan", required=True, help="GenerationPlanEpisode JSON")
    parser.add_argument(
        "--segment-id",
        default="auto",
        help="Generation segment ID or 'auto' for deterministic safe selection",
    )
    parser.add_argument("--output", required=True, help="Final one-segment Canary MP4")
    parser.add_argument("--providers", required=True, help="Live provider configuration JSON")
    parser.add_argument("--asset-bundle", help="ApprovedAssetBundle JSON")
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
    parser.add_argument("--max-cost-cny", type=_decimal, default=Decimal("10.0"))
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


def _derived_provider_path(output: Path, segment_id: str) -> Path:
    return output.parent / f".{output.stem}_{segment_id}_providers.json"


def _append_optional(arguments: list[str], flag: str, value: str | None) -> None:
    if value:
        arguments.extend([flag, value])


def _select_segment(
    plan: GenerationPlanEpisode,
    *,
    requested_segment_id: str,
    provider_clip_seconds: int,
):
    decision = select_safe_canary_candidate(
        plan,
        provider_clip_seconds=provider_clip_seconds,
    )
    if requested_segment_id == "auto":
        if decision.selected_segment_id is None:
            raise CandidateSelectionError(
                "no safe Wan single-shot Canary candidate is available"
            )
        selected_id = decision.selected_segment_id
    else:
        selected_id = requested_segment_id
        if selected_id not in decision.eligible_segment_ids:
            rejected = next(
                (
                    item
                    for item in decision.rejected_segments
                    if item.segment_id == selected_id
                ),
                None,
            )
            detail = (
                ", ".join(rejected.reason_codes)
                if rejected is not None
                else "unknown_segment"
            )
            raise CandidateSelectionError(
                f"requested segment is not eligible for Wan Canary: {detail}"
            )
    return find_generation_segment(plan, selected_id), decision


def _segment_metadata(
    *,
    segment,
    plan: GenerationPlanEpisode,
    materialized: PreparedEpisode,
    materialized_path: Path,
    decision,
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
        "execution_mode": "single_shot",
        "materialized_prepared_episode": str(materialized_path),
        "materialized_source_digest": materialized.source_digest,
        "candidate_selection": decision.model_dump(mode="json"),
    }


def _write_enriched_report(
    payload: dict[str, object],
    *,
    report_path: str | None,
    print_report: bool,
) -> None:
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


def _load_delegate_report(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _missing_bundle_readiness(stage: str, plan_digest: str, segment_id: str) -> dict:
    mandatory = stage != "preflight"
    issue = {
        "code": "approved_asset_bundle_missing",
        "severity": "error" if mandatory else "warning",
        "message": (
            "paid provider stages require --asset-bundle"
            if mandatory
            else "no asset bundle supplied; preflight cannot verify production assets"
        ),
        "segment_id": segment_id,
    }
    return {
        "stage": stage,
        "generation_plan_digest": plan_digest,
        "bundle_digest": None,
        "selected_segment_ids": [segment_id],
        "required_asset_ids": [],
        "required_voice_character_ids": [],
        "ready": not mandatory,
        "errors": [issue] if mandatory else [],
        "warnings": [] if mandatory else [issue],
    }


def _asset_gate(
    *,
    bundle_path: str | None,
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    segment_id: str,
    stage: str,
):
    if not bundle_path:
        return None, _missing_bundle_readiness(stage, plan.content_digest, segment_id)
    bundle = load_bundle(bundle_path)
    readiness = assess_asset_readiness(
        bundle,
        prepared,
        plan,
        stage=stage,
        segment_ids=[segment_id],
    )
    return bundle, readiness.model_dump(mode="json")


def _approved_first_frame(bundle, segment_id: str):
    matches = [
        item
        for item in bundle.assets
        if item.role == "first_frame"
        and item.approval_status == "approved"
        and segment_id in item.required_for_segment_ids
    ]
    if len(matches) != 1:
        raise AssetBundleError(
            f"render requires exactly one approved first frame for {segment_id}"
        )
    return matches[0]


def _config_with_approved_voices(config: LiveProviderConfig, bundle) -> LiveProviderConfig:
    approved = {
        item.character_seed_id: item.voice_id
        for item in bundle.voice_profiles
        if item.approval_status == "approved" and item.voice_id
    }
    provider = config.dashscope.model_copy(
        update={"voice_by_character": approved}
    )
    return config.model_copy(update={"dashscope": provider})


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prepared_path = Path(args.prepared_input)
    plan_path = Path(args.generation_plan)
    output_path = Path(args.output)

    try:
        prepared = _load_prepared(prepared_path)
        plan = _load_plan(plan_path)
        config = LiveProviderConfig.load(args.providers)
        segment, decision = _select_segment(
            plan,
            requested_segment_id=args.segment_id,
            provider_clip_seconds=config.dashscope.provider_clip_seconds,
        )
        materialized = materialize_generation_segment_canary(
            prepared,
            plan,
            segment.segment_id,
            provider_clip_seconds=config.dashscope.provider_clip_seconds,
        )
        bundle, asset_readiness = _asset_gate(
            bundle_path=args.asset_bundle,
            prepared=prepared,
            plan=plan,
            segment_id=segment.segment_id,
            stage=args.stage,
        )
    except (
        OSError,
        ValidationError,
        json.JSONDecodeError,
        SegmentCanaryError,
        CandidateSelectionError,
        AssetBundleError,
    ) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except ProviderConfigurationError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    materialized_path = (
        Path(args.materialized_prepared)
        if args.materialized_prepared
        else _materialized_path(output_path, segment.segment_id)
    ).resolve()
    _atomic_write(materialized_path, materialized.to_canonical_json() + "\n")

    common_metadata = _segment_metadata(
        segment=segment,
        plan=plan,
        materialized=materialized,
        materialized_path=materialized_path,
        decision=decision,
    )
    asset_metadata = {
        "asset_bundle_present": bundle is not None,
        "asset_readiness": asset_readiness,
    }
    missing_environment = config.dashscope.missing_environment()
    credentials_present = bool(os.getenv(config.dashscope.api_key_env))

    if not asset_readiness["ready"]:
        failure = {
            "valid": False,
            "status": "blocked",
            "stage": args.stage,
            "credentials_present": credentials_present,
            "missing_environment": missing_environment,
            "external_api_calls": 0,
            "committed_api_calls": 0,
            "committed_cost_cny": "0",
            "asset_gate": "blocked_before_provider_submission",
            "errors": [item["message"] for item in asset_readiness["errors"]],
            **common_metadata,
            **asset_metadata,
        }
        _write_enriched_report(
            failure,
            report_path=args.report,
            print_report=args.print_report,
        )
        print(
            "provider error: approved asset gate blocked all provider submissions",
            file=sys.stderr,
        )
        return EXIT_PROVIDER

    delegated_provider_path = Path(args.providers).resolve()
    approved_keyframe = args.approved_keyframe
    approval_manifest = args.approval_manifest
    if args.stage == "render":
        if bundle is None:
            raise AssertionError("render readiness cannot pass without an asset bundle")
        first_frame = _approved_first_frame(bundle, segment.segment_id)
        if approved_keyframe and Path(approved_keyframe).resolve() != Path(
            first_frame.asset_path
        ).resolve():
            print(
                "provider error: explicit approved keyframe differs from asset bundle",
                file=sys.stderr,
            )
            return EXIT_PROVIDER
        if approval_manifest and Path(approval_manifest).resolve() != Path(
            first_frame.approval_manifest_path
        ).resolve():
            print(
                "provider error: explicit approval manifest differs from asset bundle",
                file=sys.stderr,
            )
            return EXIT_PROVIDER
        approved_keyframe = first_frame.asset_path
        approval_manifest = first_frame.approval_manifest_path
        config = _config_with_approved_voices(config, bundle)
        delegated_provider_path = _derived_provider_path(
            output_path,
            segment.segment_id,
        ).resolve()
        _atomic_write(delegated_provider_path, config.to_canonical_json())

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

    budget_metadata: dict[str, object] = {
        "execution_budget": {
            "currency": "CNY",
            "price_snapshot_id": f"dashscope-{config.dashscope.price_snapshot_date}",
            "planned_keyframe_calls": planned_keyframe_calls,
            "planned_render_calls": planned_render_calls,
            "planned_api_calls": planned_api_calls,
            "planned_keyframe_cost": str(planned_keyframe_cost),
            "planned_render_cost": str(planned_render_cost),
            "planned_total": str(planned_cost_cny),
            "hard_call_limit": args.max_api_calls,
            "hard_cost_limit": str(args.max_cost_cny),
            "within_call_limit": within_call_limit,
            "within_cost_limit": within_cost_limit,
            "unknown_components": [],
        },
        "planned_keyframe_calls": planned_keyframe_calls,
        "planned_render_calls": planned_render_calls,
        "planned_api_calls": planned_api_calls,
        "planned_cost_cny": str(planned_cost_cny),
        "max_api_calls": args.max_api_calls,
        "max_cost_cny": str(args.max_cost_cny),
        "within_requested_call_limit": within_call_limit,
        "within_requested_cost_limit": within_cost_limit,
    }

    if not within_call_limit or not within_cost_limit:
        failure = {
            "valid": False,
            "status": "blocked",
            "stage": args.stage,
            "credentials_present": credentials_present,
            "missing_environment": missing_environment,
            "external_api_calls": 0,
            "committed_api_calls": 0,
            "committed_cost_cny": "0",
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
            **asset_metadata,
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

    delegate_report = _delegate_report_path(output_path, segment.segment_id).resolve()
    delegated = [
        "--input",
        str(materialized_path),
        "--output",
        str(output_path),
        "--providers",
        str(delegated_provider_path),
        "--shot-id",
        segment.segment_id,
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
    _append_optional(delegated, "--approved-keyframe", approved_keyframe)
    _append_optional(delegated, "--approval-manifest", approval_manifest)
    _append_optional(delegated, "--keyframe-output", args.keyframe_output)
    _append_optional(delegated, "--ledger-file", args.ledger_file)
    _append_optional(delegated, "--work-dir", args.work_dir)
    _append_optional(delegated, "--projects-file", args.projects_file)
    _append_optional(delegated, "--index-file", args.index_file)
    if args.reset:
        delegated.append("--reset")

    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(
        captured_stderr
    ):
        exit_code = render_canary_main(delegated)

    payload = _load_delegate_report(delegate_report) or {
        "valid": False,
        "stage": args.stage,
        "external_api_calls": 0,
    }
    payload.update(common_metadata)
    payload.update(asset_metadata)
    payload.update(budget_metadata)
    payload["credentials_present"] = credentials_present
    payload["missing_environment"] = missing_environment
    payload["delegate_exit_code"] = exit_code
    if captured_stdout.getvalue().strip():
        payload["delegate_stdout"] = captured_stdout.getvalue().strip()
    if captured_stderr.getvalue().strip():
        payload["delegate_stderr"] = captured_stderr.getvalue().strip()

    if exit_code != EXIT_OK:
        payload["valid"] = False
        payload["status"] = "failed"
        errors = list(payload.get("errors", []))
        detail = captured_stderr.getvalue().strip() or (
            f"delegated Wan Canary exited with code {exit_code}"
        )
        errors.append(detail)
        payload["errors"] = errors
    else:
        payload["status"] = (
            "succeeded"
            if args.stage in {"preflight", "approve", "render"}
            else "awaiting_operator"
        )

    _write_enriched_report(
        payload,
        report_path=args.report,
        print_report=args.print_report,
    )
    if exit_code != EXIT_OK:
        print(
            f"provider error: delegated Wan Canary failed with exit code {exit_code}",
            file=sys.stderr,
        )
        return exit_code

    if not args.print_report:
        print(
            f"Segment: {segment.segment_id}\n"
            f"Stage: {args.stage}\n"
            f"Route: {segment.provider_route_id}\n"
            f"Provider request: {segment.requested_duration_seconds}s\n"
            f"Editorial duration: {segment.editorial_duration_seconds}s\n"
            f"Assets ready: {asset_readiness['ready']}\n"
            f"Planned budget: {planned_api_calls} calls / {planned_cost_cny} CNY\n"
            f"External API calls this stage: {payload.get('external_api_calls', 0)}\n"
            "Status: OK"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
