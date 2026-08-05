"""Generate and approve one Wan first frame from exact approved master images."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from ..assets import (
    WanMasterReferenceError,
    load_bundle,
    load_wan_master_reference_manifest,
    verify_wan_master_reference_manifest,
)
from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode
from ..rendering.approval import ApprovalError, create_approval_manifest, png_dimensions
from ..rendering.ffmpeg import file_sha256
from ..rendering.provider_config import LiveProviderConfig, ProviderConfigurationError
from ..rendering.provider_ledger import CanaryProviderLedgerStore, ProviderLedgerError
from ..rendering.segment_canary import (
    SegmentCanaryError,
    materialize_generation_segment_canary,
)
from ..rendering.wan_master_tasks import WanMasterReferenceLiveTaskExecutor


PROTOCOL_ID = "wan-approved-master-keyframe-v1"
EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_PROVIDER = 6
EXIT_APPROVAL = 7


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight, generate, or approve one Wan first frame using an exact "
            "WanMasterReferenceManifest. Only --stage keyframe can call a provider."
        )
    )
    parser.add_argument("--prepared-input", required=True)
    parser.add_argument("--generation-plan", required=True)
    parser.add_argument("--asset-bundle", required=True)
    parser.add_argument("--master-reference-manifest", required=True)
    parser.add_argument("--providers", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument(
        "--stage",
        choices=("preflight", "keyframe", "approve"),
        default="preflight",
    )
    parser.add_argument("--keyframe-output", required=True)
    parser.add_argument("--approval-manifest")
    parser.add_argument("--ledger-file")
    parser.add_argument("--approval-digest")
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--max-api-calls", type=int, default=1)
    parser.add_argument("--max-cost-cny", type=_decimal, default=Decimal("5.0"))
    parser.add_argument("--report")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _atomic_report(payload: dict[str, object], path: str | None, print_report: bool) -> None:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path:
        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
    if print_report:
        print(content, end="")


def _approval_path(keyframe: Path, explicit: str | None) -> Path:
    return (
        Path(explicit).resolve()
        if explicit
        else keyframe.with_suffix(".approval.json")
    )


def _ledger_path(keyframe: Path, segment_id: str, explicit: str | None) -> Path:
    return (
        Path(explicit).resolve()
        if explicit
        else keyframe.parent / f".{keyframe.stem}_{segment_id}_provider_ledger.json"
    )


def _find_keyframe_task_id(prepared: PreparedEpisode, segment_id: str) -> str:
    node = next(
        (
            item
            for item in prepared.render_graph.nodes
            if item.shot_id == segment_id
            and item.task_type
            in {"generate_image", "generate_video", "generate_native_av"}
        ),
        None,
    )
    if node is None:
        raise ValueError(f"segment cannot generate a first frame: {segment_id}")
    return node.task_id


def _preflight_payload(
    *,
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    manifest,
    config: LiveProviderConfig,
    segment_id: str,
    keyframe_output: Path,
    ledger_file: Path,
    max_api_calls: int,
    max_cost_cny: Decimal,
) -> dict[str, object]:
    task_id = _find_keyframe_task_id(prepared, segment_id)
    operation_id = WanMasterReferenceLiveTaskExecutor.keyframe_operation_id(
        task_id,
        manifest,
    )
    estimated_cost = config.dashscope.estimate_image_cost_cny()
    stable = {
        "protocol": PROTOCOL_ID,
        "segment_id": segment_id,
        "generation_plan_digest": plan.content_digest,
        "prepared_source_digest": prepared.source_digest,
        "master_reference_manifest_digest": manifest.content_digest,
        "master_asset_set_digest": manifest.master_asset_set_digest,
        "reference_asset_ids": manifest.asset_ids,
        "reference_asset_hashes": manifest.asset_hashes,
        "provider_execution_profile": config.execution_profile,
        "provider": config.dashscope.provider,
        "model": config.dashscope.image_model,
        "operation_id": operation_id,
        "keyframe_output": str(keyframe_output),
        "ledger_file": str(ledger_file),
        "estimated_cost_cny": str(estimated_cost),
        "max_api_calls": max_api_calls,
        "max_cost_cny": str(max_cost_cny),
    }
    canonical = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **stable,
        "approval_digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        "missing_environment": config.dashscope.missing_environment(),
        "credentials_present": not config.dashscope.missing_environment(),
        "external_api_calls": 0,
        "valid": estimated_cost <= max_cost_cny and max_api_calls == 1,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_api_calls != 1:
        print("provider error: keyframe workflow requires --max-api-calls 1", file=sys.stderr)
        return EXIT_PROVIDER
    if args.max_cost_cny < 0 or args.max_cost_cny > Decimal("50"):
        print("provider error: --max-cost-cny must be between 0 and 50", file=sys.stderr)
        return EXIT_PROVIDER

    keyframe = Path(args.keyframe_output).resolve()
    approval_path = _approval_path(keyframe, args.approval_manifest)
    ledger_path = _ledger_path(keyframe, args.segment_id, args.ledger_file).resolve()
    try:
        source = PreparedEpisode.model_validate_json(
            Path(args.prepared_input).read_text(encoding="utf-8")
        )
        plan = GenerationPlanEpisode.model_validate_json(
            Path(args.generation_plan).read_text(encoding="utf-8")
        )
        bundle = load_bundle(args.asset_bundle)
        manifest = load_wan_master_reference_manifest(
            args.master_reference_manifest
        )
        verify_wan_master_reference_manifest(
            manifest,
            source,
            plan,
            bundle,
            segment_id=args.segment_id,
        )
        config = LiveProviderConfig.load(args.providers)
        materialized = materialize_generation_segment_canary(
            source,
            plan,
            args.segment_id,
            provider_clip_seconds=config.dashscope.provider_clip_seconds,
        )
        preflight = _preflight_payload(
            prepared=materialized,
            plan=plan,
            manifest=manifest,
            config=config,
            segment_id=args.segment_id,
            keyframe_output=keyframe,
            ledger_file=ledger_path,
            max_api_calls=args.max_api_calls,
            max_cost_cny=args.max_cost_cny,
        )
    except (
        OSError,
        ValidationError,
        ValueError,
        WanMasterReferenceError,
        SegmentCanaryError,
        ProviderConfigurationError,
    ) as exc:
        payload = {
            "valid": False,
            "protocol": PROTOCOL_ID,
            "stage": args.stage,
            "segment_id": args.segment_id,
            "external_api_calls": 0,
            "errors": [str(exc)],
        }
        _atomic_report(payload, args.report, args.print_report)
        print(f"not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY

    if args.stage == "preflight":
        payload = {
            **preflight,
            "stage": "preflight",
            "next_action": (
                "review this report and rerun --stage keyframe --execute-paid "
                "with the exact approval digest"
            ),
        }
        _atomic_report(payload, args.report, args.print_report)
        if not preflight["valid"]:
            print("not ready: keyframe preflight exceeds limits", file=sys.stderr)
            return EXIT_NOT_READY
        if not args.print_report:
            print(
                f"Wan master keyframe preflight: VALID\n"
                f"Segment: {args.segment_id}\n"
                f"References: {len(manifest.references)}\n"
                f"Estimated cost: {preflight['estimated_cost_cny']} CNY\n"
                f"Approval digest: {preflight['approval_digest']}\n"
                "External API calls: 0"
            )
        return EXIT_OK

    if args.stage == "approve":
        if args.execute_paid:
            print("approval error: --stage approve must not use --execute-paid", file=sys.stderr)
            return EXIT_APPROVAL
        try:
            store = CanaryProviderLedgerStore(ledger_path)
            ledger = store.load_or_create(
                source_digest=WanMasterReferenceLiveTaskExecutor.ledger_source_digest(
                    materialized,
                    manifest,
                ),
                shot_id=args.segment_id,
                max_api_calls=1,
                max_cost_cny=args.max_cost_cny,
            )
            operation_id = str(preflight["operation_id"])
            record = ledger.operations.get(operation_id)
            if record is None or record.status != "succeeded":
                raise ApprovalError("keyframe provider operation has not succeeded")
            if not keyframe.is_file() or keyframe.stat().st_size == 0:
                raise ApprovalError(f"keyframe does not exist: {keyframe}")
            if record.output_sha256 and file_sha256(keyframe) != record.output_sha256:
                raise ApprovalError("keyframe differs from provider ledger output")
            approved = create_approval_manifest(
                shot_id=args.segment_id,
                asset_path=keyframe,
                generated_by=f"dashscope/{config.dashscope.image_model}",
                operation_id=operation_id,
                output_path=approval_path,
                master_reference_manifest_digest=manifest.content_digest,
                master_reference_asset_ids=manifest.asset_ids,
                master_reference_asset_hashes=manifest.asset_hashes,
            )
        except (ProviderLedgerError, ApprovalError, OSError) as exc:
            payload = {
                **preflight,
                "valid": False,
                "stage": "approve",
                "external_api_calls": 0,
                "errors": [str(exc)],
            }
            _atomic_report(payload, args.report, args.print_report)
            print(f"approval error: {exc}", file=sys.stderr)
            return EXIT_APPROVAL
        payload = {
            **preflight,
            "valid": True,
            "stage": "approve",
            "approval_manifest": str(approval_path),
            "approved_keyframe": approved.asset_path,
            "approved_keyframe_sha256": approved.asset_sha256,
            "dimensions": f"{approved.width}x{approved.height}",
            "external_api_calls": 0,
            "next_action": "register this approval into the episode AssetBundle",
        }
        _atomic_report(payload, args.report, args.print_report)
        return EXIT_OK

    if not args.execute_paid:
        print("provider error: --stage keyframe requires --execute-paid", file=sys.stderr)
        return EXIT_PROVIDER
    if args.approval_digest != preflight["approval_digest"]:
        print("provider error: approval digest does not match current preflight", file=sys.stderr)
        return EXIT_PROVIDER
    if not preflight["valid"]:
        print("provider error: keyframe preflight exceeds limits", file=sys.stderr)
        return EXIT_PROVIDER
    try:
        config.require_environment()
        store = CanaryProviderLedgerStore(ledger_path)
        ledger = store.load_or_create(
            source_digest=WanMasterReferenceLiveTaskExecutor.ledger_source_digest(
                materialized,
                manifest,
            ),
            shot_id=args.segment_id,
            max_api_calls=1,
            max_cost_cny=args.max_cost_cny,
        )
        executor = WanMasterReferenceLiveTaskExecutor(
            config,
            master_references={args.segment_id: manifest},
            api_call_limit=1,
            ledger_store=store,
            ledger=ledger,
        )
        generated = executor.generate_canary_keyframe(
            materialized,
            shot_id=args.segment_id,
            output=keyframe,
        )
        width, height = png_dimensions(generated)
    except Exception as exc:
        payload = {
            **preflight,
            "valid": False,
            "stage": "keyframe",
            "errors": [str(exc)],
        }
        _atomic_report(payload, args.report, args.print_report)
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER
    payload = {
        **preflight,
        "valid": True,
        "stage": "keyframe",
        "keyframe": str(generated),
        "keyframe_sha256": file_sha256(generated),
        "dimensions": f"{width}x{height}",
        "cumulative_api_calls": ledger.committed_api_calls,
        "cumulative_cost_cny": str(ledger.committed_cost_cny),
        "next_action": "review the image, then run --stage approve",
    }
    _atomic_report(payload, args.report, args.print_report)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
