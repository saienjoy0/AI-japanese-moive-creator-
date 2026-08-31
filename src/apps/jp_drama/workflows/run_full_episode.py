"""Preflight, resume, render, trim, and compose one complete Japanese drama episode."""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from ..assets import AssetBundleError, assess_asset_readiness, load_bundle
from ..full_episode import (
    FullEpisodeComposer,
    FullEpisodeError,
    FullEpisodeValidationReport,
)
from ..generation import (
    ExecutionBudgetError,
    build_execution_budget,
    load_ledgers,
)
from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..preparation.models import PreparedEpisode
from ..rendering.ffmpeg import canonical_digest, file_sha256
from ..rendering.provider_config import LiveProviderConfig, ProviderConfigurationError
from ..rendering.segment_canary import (
    SegmentCanaryError,
    materialize_generation_segment_canary,
)
from .render_canary_episode import main as render_canary_main


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_RENDER = 4
EXIT_VALIDATION = 5
EXIT_PROVIDER = 6


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a restart-safe full Japanese-drama episode. Preflight is zero-cost; "
            "paid render requires an explicit approval digest; compose accepts existing "
            "segment MP4 files and makes no provider calls."
        )
    )
    parser.add_argument("--prepared-input", required=True, help="PreparedEpisode JSON")
    parser.add_argument("--generation-plan", required=True, help="GenerationPlanEpisode JSON")
    parser.add_argument("--output", required=True, help="Final episode MP4")
    parser.add_argument("--work-dir", required=True, help="Persistent full-episode work directory")
    parser.add_argument(
        "--stage",
        choices=("preflight", "compose", "render"),
        default="preflight",
    )
    parser.add_argument("--providers", help="Live provider configuration JSON")
    parser.add_argument("--asset-bundle", help="ApprovedAssetBundle JSON")
    parser.add_argument(
        "--segment-outputs",
        help="JSON map of segment_id to existing provider MP4; required for compose",
    )
    parser.add_argument("--hard-max-calls", type=int, default=20)
    parser.add_argument("--hard-limit-cny", type=_decimal, default=Decimal("60"))
    parser.add_argument("--retry-reserve-cny", type=_decimal, default=Decimal("0"))
    parser.add_argument("--candidate-reserve-cny", type=_decimal, default=Decimal("0"))
    parser.add_argument(
        "--execute-paid",
        action="store_true",
        help="Explicitly enable provider submission after every gate passes",
    )
    parser.add_argument(
        "--approval-digest",
        help="Exact operator approval digest printed by preflight",
    )
    parser.add_argument("--target-width", type=int, default=720)
    parser.add_argument("--target-height", type=int, default=1280)
    parser.add_argument("--report", help="Optional workflow JSON report")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_prepared(path: str | Path) -> PreparedEpisode:
    return PreparedEpisode.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _load_plan(path: str | Path) -> GenerationPlanEpisode:
    return GenerationPlanEpisode.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_report(payload: dict, path: str | None, print_report: bool) -> None:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path:
        _atomic_write(Path(path).resolve(), content)
    if print_report:
        print(content, end="")


def _ledger_path(work_dir: Path, segment_id: str) -> Path:
    return work_dir / "ledgers" / f"{segment_id}.json"


def _segment_report_path(work_dir: Path, segment_id: str) -> Path:
    return work_dir / "provider_reports" / f"{segment_id}.json"


def _segment_output_path(
    work_dir: Path,
    segment: GenerationSegment,
) -> Path:
    return work_dir / "provider_outputs" / f"{segment.order:03d}_{segment.segment_id}.mp4"


def _materialized_path(work_dir: Path, segment_id: str) -> Path:
    return work_dir / "materialized" / f"{segment_id}.json"


def _segment_work_dir(work_dir: Path, segment_id: str) -> Path:
    return work_dir / "provider_work" / segment_id


def _derived_provider_path(work_dir: Path) -> Path:
    return work_dir / "approved_providers.json"


def _existing_ledgers(work_dir: Path) -> list:
    ledger_dir = work_dir / "ledgers"
    if not ledger_dir.is_dir():
        return []
    paths = sorted(item for item in ledger_dir.glob("*.json") if item.is_file())
    return load_ledgers(paths)


def _approved_config(config: LiveProviderConfig, bundle) -> LiveProviderConfig:
    voices = {
        item.character_seed_id: item.voice_id
        for item in bundle.voice_profiles
        if item.approval_status == "approved" and item.voice_id
    }
    provider = config.dashscope.model_copy(update={"voice_by_character": voices})
    return config.model_copy(update={"dashscope": provider})


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
            f"full episode requires exactly one approved first frame for {segment_id}"
        )
    return matches[0]


def _authorization_digest(
    *,
    plan: GenerationPlanEpisode,
    bundle,
    budget,
    output: Path,
) -> str:
    """Digest stable across planned-to-committed ledger transitions."""
    return canonical_digest(
        [
            "full-episode-paid-authorization-v1",
            plan.content_digest,
            bundle.content_digest,
            budget.provider_route_id,
            budget.price_snapshot_id,
            json.dumps(budget.selected_segment_ids, separators=(",", ":")),
            str(budget.hard_maximum_calls),
            str(budget.hard_limit_cny),
            str(budget.retry_reserve_cny),
            str(budget.candidate_reserve_cny),
            str(budget.total_exposure_api_calls),
            str(budget.total_exposure_cny),
            str(output.resolve()),
        ]
    )


def _build_budget(plan, config, bundle, work_dir: Path, args):
    return build_execution_budget(
        plan,
        config,
        asset_bundle=bundle,
        ledgers=_existing_ledgers(work_dir),
        hard_maximum_calls=args.hard_max_calls,
        hard_limit_cny=args.hard_limit_cny,
        retry_reserve_cny=args.retry_reserve_cny,
        candidate_reserve_cny=args.candidate_reserve_cny,
    )


def _parse_segment_outputs(path: str | None) -> dict[str, Path]:
    if not path:
        raise ValueError("--segment-outputs is required for compose")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("segments"), dict):
        payload = payload["segments"]
    if not isinstance(payload, dict) or not payload:
        raise ValueError("segment output JSON must be a non-empty object")
    return {str(key): Path(str(value)).resolve() for key, value in payload.items()}


def _report_is_reusable(path: Path, output: Path, segment_id: str) -> bool:
    if not path.is_file() or not output.is_file() or output.stat().st_size == 0:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("valid") is True
        and payload.get("stage") == "render"
        and payload.get("shot_id") == segment_id
    )


def _compose(
    *,
    plan,
    output: Path,
    work_dir: Path,
    segment_outputs: dict[str, Path],
    args,
    asset_bundle_digest: str | None,
    authorization_digest: str | None,
    external_api_calls: int,
) -> FullEpisodeValidationReport:
    composer = FullEpisodeComposer(
        plan,
        output_file=output,
        work_dir=work_dir,
        target_width=args.target_width,
        target_height=args.target_height,
        asset_bundle_digest=asset_bundle_digest,
        execution_budget_digest=authorization_digest,
    )
    return composer.compose(
        segment_outputs,
        reset=args.reset,
        external_api_calls=external_api_calls,
    )


def _preflight_payload(
    *,
    plan,
    bundle,
    asset_readiness,
    budget,
    approval_digest: str,
    output: Path,
    work_dir: Path,
    config,
) -> dict:
    return {
        "valid": asset_readiness.ready and budget.payment_approved,
        "stage": "preflight",
        "generation_plan_digest": plan.content_digest,
        "asset_bundle_digest": bundle.content_digest,
        "asset_readiness": asset_readiness.model_dump(mode="json"),
        "execution_budget": budget.model_dump(mode="json"),
        "approval_digest": approval_digest,
        "output_file": str(output),
        "work_dir": str(work_dir),
        "provider_manifest": config.provider_manifest,
        "missing_environment": config.dashscope.missing_environment(),
        "external_api_calls": 0,
        "next_action": (
            "run render with --execute-paid and this exact --approval-digest"
            if asset_readiness.ready and budget.payment_approved
            else "resolve asset or budget blockers and rerun preflight"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output).resolve()
    work_dir = Path(args.work_dir).resolve()

    try:
        prepared = _load_prepared(args.prepared_input)
        plan = _load_plan(args.generation_plan)
        if plan.source_prepared_episode_digest != _prepared_digest(prepared):
            raise ValueError("GenerationPlan does not belong to PreparedEpisode")
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if args.stage == "compose":
        try:
            segment_outputs = _parse_segment_outputs(args.segment_outputs)
            report = _compose(
                plan=plan,
                output=output,
                work_dir=work_dir,
                segment_outputs=segment_outputs,
                args=args,
                asset_bundle_digest=None,
                authorization_digest=None,
                external_api_calls=0,
            )
        except (OSError, ValidationError, ValueError, FullEpisodeError) as exc:
            print(f"validation error: {exc}", file=sys.stderr)
            return EXIT_VALIDATION
        payload = {
            "valid": report.valid,
            "stage": "compose",
            "generation_plan_digest": plan.content_digest,
            "output_file": str(output),
            "validation_report": report.model_dump(mode="json"),
            "external_api_calls": 0,
        }
        _write_report(payload, args.report, args.print_report)
        if not args.print_report:
            print(
                f"Output: {output}\n"
                f"Segments: {len(report.segment_validations)}\n"
                f"Frames: {report.actual_frame_count}/{report.expected_frame_count}\n"
                f"Duration: {report.duration_seconds:.3f}s\n"
                "Status: VALID"
            )
        return EXIT_OK

    if not args.providers or not args.asset_bundle:
        print(
            "input error: --providers and --asset-bundle are required for preflight/render",
            file=sys.stderr,
        )
        return EXIT_INPUT

    try:
        config = LiveProviderConfig.load(args.providers)
        bundle = load_bundle(args.asset_bundle)
        asset_readiness = assess_asset_readiness(
            bundle,
            prepared,
            plan,
            stage="full_episode",
        )
        config = _approved_config(config, bundle)
        budget = _build_budget(plan, config, bundle, work_dir, args)
        approval_digest = _authorization_digest(
            plan=plan,
            bundle=bundle,
            budget=budget,
            output=output,
        )
    except (
        OSError,
        ValidationError,
        json.JSONDecodeError,
        AssetBundleError,
        ExecutionBudgetError,
        ProviderConfigurationError,
    ) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    preflight = _preflight_payload(
        plan=plan,
        bundle=bundle,
        asset_readiness=asset_readiness,
        budget=budget,
        approval_digest=approval_digest,
        output=output,
        work_dir=work_dir,
        config=config,
    )
    if args.stage == "preflight":
        _write_report(preflight, args.report, args.print_report)
        if not args.print_report:
            print(
                f"Segments: {len(plan.segments)}\n"
                f"Asset ready: {asset_readiness.ready}\n"
                f"Budget: {budget.total_exposure_api_calls} calls / "
                f"{budget.total_exposure_cny} CNY\n"
                f"Approval digest: {approval_digest}\n"
                "External API calls: 0"
            )
        return EXIT_OK if preflight["valid"] else EXIT_NOT_READY

    if not asset_readiness.ready:
        _write_report(
            {**preflight, "valid": False, "stage": "render", "status": "blocked"},
            args.report,
            args.print_report,
        )
        print("not ready: approved asset bundle is incomplete", file=sys.stderr)
        return EXIT_NOT_READY
    if not budget.payment_approved:
        _write_report(
            {**preflight, "valid": False, "stage": "render", "status": "blocked"},
            args.report,
            args.print_report,
        )
        print("provider error: execution budget is not approved", file=sys.stderr)
        return EXIT_PROVIDER
    if not args.execute_paid or args.approval_digest != approval_digest:
        blocked = {
            **preflight,
            "valid": False,
            "stage": "render",
            "status": "blocked",
            "paid_execution_gate": "operator_approval_required",
            "external_api_calls": 0,
        }
        _write_report(blocked, args.report, args.print_report)
        print(
            "provider error: render requires --execute-paid and the exact preflight "
            "--approval-digest; no provider request was submitted",
            file=sys.stderr,
        )
        return EXIT_PROVIDER

    try:
        config.require_environment()
    except ProviderConfigurationError as exc:
        blocked = {
            **preflight,
            "valid": False,
            "stage": "render",
            "status": "blocked",
            "provider_environment_gate": "blocked_before_provider_submission",
            "errors": [str(exc)],
            "external_api_calls": 0,
        }
        _write_report(blocked, args.report, args.print_report)
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    work_dir.mkdir(parents=True, exist_ok=True)
    derived_provider = _derived_provider_path(work_dir)
    _atomic_write(derived_provider, config.to_canonical_json())
    composer = FullEpisodeComposer(
        plan,
        output_file=output,
        work_dir=work_dir,
        target_width=args.target_width,
        target_height=args.target_height,
        asset_bundle_digest=bundle.content_digest,
        execution_budget_digest=approval_digest,
    )
    if args.reset:
        composer._reset()
    state = composer._load_or_create_state()
    state.status = "rendering"
    composer._write_state(state)

    outputs: dict[str, Path] = {}
    provider_calls = 0
    try:
        for segment in plan.segments:
            current_budget = _build_budget(plan, config, bundle, work_dir, args)
            current_digest = _authorization_digest(
                plan=plan,
                bundle=bundle,
                budget=current_budget,
                output=output,
            )
            if current_digest != approval_digest or not current_budget.payment_approved:
                raise ExecutionBudgetError(
                    "execution exposure changed after approval; stopping before the next submission"
                )

            raw_output = _segment_output_path(work_dir, segment)
            segment_report = _segment_report_path(work_dir, segment.segment_id)
            ledger = _ledger_path(work_dir, segment.segment_id)
            item = state.segments[segment.segment_id]
            item.ledger_file = str(ledger)
            item.segment_report_file = str(segment_report)

            if _report_is_reusable(segment_report, raw_output, segment.segment_id):
                item.status = "rendered"
                item.source_output_file = str(raw_output)
                item.source_output_sha256 = file_sha256(raw_output)
                item.last_error = None
                outputs[segment.segment_id] = raw_output
                composer._write_state(state)
                continue

            segment_ledgers = load_ledgers([ledger]) if ledger.is_file() else []
            segment_budget = build_execution_budget(
                plan,
                config,
                asset_bundle=bundle,
                ledgers=segment_ledgers,
                segment_ids=[segment.segment_id],
                hard_maximum_calls=3,
                hard_limit_cny=Decimal("50"),
            )
            if not segment_budget.payment_approved:
                raise ExecutionBudgetError(
                    f"segment budget is not approved: {segment.segment_id}"
                )

            first_frame = _approved_first_frame(bundle, segment.segment_id)
            materialized = materialize_generation_segment_canary(
                prepared,
                plan,
                segment.segment_id,
                provider_clip_seconds=config.dashscope.provider_clip_seconds,
            )
            materialized_path = _materialized_path(work_dir, segment.segment_id)
            _atomic_write(materialized_path, materialized.to_canonical_json() + "\n")
            raw_output.parent.mkdir(parents=True, exist_ok=True)
            segment_report.parent.mkdir(parents=True, exist_ok=True)
            ledger.parent.mkdir(parents=True, exist_ok=True)

            item.status = "rendering"
            item.attempts += 1
            item.last_error = None
            composer._write_state(state)
            exit_code = render_canary_main(
                [
                    "--input",
                    str(materialized_path),
                    "--output",
                    str(raw_output),
                    "--providers",
                    str(derived_provider),
                    "--shot-id",
                    segment.segment_id,
                    "--stage",
                    "render",
                    "--approved-keyframe",
                    str(first_frame.asset_path),
                    "--approval-manifest",
                    str(first_frame.approval_manifest_path),
                    "--ledger-file",
                    str(ledger),
                    "--max-api-calls",
                    str(segment_budget.total_exposure_api_calls),
                    "--max-cost-cny",
                    str(segment_budget.total_exposure_cny),
                    "--work-dir",
                    str(_segment_work_dir(work_dir, segment.segment_id)),
                    "--report",
                    str(segment_report),
                ]
            )
            if exit_code != EXIT_OK:
                item.status = "failed"
                item.last_error = f"provider segment workflow exited with code {exit_code}"
                composer._write_state(state)
                raise ProviderConfigurationError(item.last_error)
            if not _report_is_reusable(segment_report, raw_output, segment.segment_id):
                raise ProviderConfigurationError(
                    f"provider segment succeeded without a reusable report: {segment.segment_id}"
                )
            item.status = "rendered"
            item.source_output_file = str(raw_output)
            item.source_output_sha256 = file_sha256(raw_output)
            item.last_error = None
            outputs[segment.segment_id] = raw_output
            composer._write_state(state)

        provider_calls = sum(
            ledger.committed_api_calls for ledger in _existing_ledgers(work_dir)
        )
        report = composer.compose(
            outputs,
            external_api_calls=provider_calls,
        )
    except (
        OSError,
        ValidationError,
        AssetBundleError,
        ExecutionBudgetError,
        SegmentCanaryError,
        ProviderConfigurationError,
        FullEpisodeError,
    ) as exc:
        state.status = "failed"
        composer._write_state(state)
        failure = {
            **preflight,
            "valid": False,
            "stage": "render",
            "status": "failed",
            "error": str(exc),
            "external_api_calls": provider_calls,
            "state_file": str(composer.state_file),
        }
        _write_report(failure, args.report, args.print_report)
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_RENDER

    payload = {
        "valid": report.valid,
        "stage": "render",
        "status": "succeeded",
        "generation_plan_digest": plan.content_digest,
        "asset_bundle_digest": bundle.content_digest,
        "approval_digest": approval_digest,
        "output_file": str(output),
        "state_file": str(composer.state_file),
        "validation_report_file": str(composer.report_file),
        "validation_report": report.model_dump(mode="json"),
        "external_api_calls": provider_calls,
    }
    _write_report(payload, args.report, args.print_report)
    if not args.print_report:
        print(
            f"Output: {output}\n"
            f"Segments: {len(report.segment_validations)}\n"
            f"Frames: {report.actual_frame_count}/{report.expected_frame_count}\n"
            f"External API calls: {provider_calls}\n"
            "Status: SUCCEEDED"
        )
    return EXIT_OK


def _prepared_digest(prepared: PreparedEpisode) -> str:
    import hashlib

    canonical = prepared.to_canonical_json(indent=None).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


if __name__ == "__main__":
    raise SystemExit(main())
