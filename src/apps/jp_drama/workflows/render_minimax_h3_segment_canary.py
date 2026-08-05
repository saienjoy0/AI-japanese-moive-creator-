"""Run one approved GenerationSegment through MiniMax H3 without duplicate POSTs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode
from ..rendering.minimax_h3_adapter import MiniMaxH3Adapter
from ..rendering.minimax_h3_approval import (
    MiniMaxH3ApprovalError,
    create_h3_approval_manifest,
    verify_h3_approval_manifest,
)
from ..rendering.minimax_h3_canary import (
    H3CanaryAssetManifest,
    MiniMaxH3CanaryError,
    find_h3_segment,
    prepare_h3_canary,
    write_prepared_h3_canary,
)
from ..rendering.minimax_h3_client import MiniMaxH3Client
from ..rendering.minimax_h3_config import (
    MiniMaxH3ConfigurationError,
    MiniMaxH3ProviderConfig,
)
from ..rendering.minimax_h3_executor import (
    MiniMaxH3ExecutionError,
    MiniMaxH3Executor,
    strict_h3_video_validator,
)
from ..rendering.provider_execution_ledger import (
    H3ExecutionLedgerError,
    H3ExecutionLedgerStore,
)


EXIT_OK = 0
EXIT_INPUT = 1
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
            "Preflight, approve, render, resume, or reconcile exactly one MiniMax H3 "
            "GenerationSegment with a persistent one-POST ledger."
        )
    )
    parser.add_argument("--prepared-input", required=True)
    parser.add_argument("--generation-plan", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--assets", required=True, help="Public HTTPS reference asset manifest")
    parser.add_argument("--config", required=True, help="MiniMax H3 provider config JSON")
    parser.add_argument("--output", required=True, help="Final one-segment MP4")
    parser.add_argument(
        "--stage",
        choices=("preflight", "approve", "render", "resume", "reconcile"),
        default="preflight",
    )
    parser.add_argument("--max-cost-usd", type=_decimal, default=Decimal("1.00"))
    parser.add_argument("--approval-manifest")
    parser.add_argument("--ledger-file")
    parser.add_argument("--prepared-request-output")
    parser.add_argument("--report")
    parser.add_argument("--recovered-task-id")
    parser.add_argument("--recovery-evidence")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _atomic_write(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_report(payload: dict[str, object], path: str | None, print_report: bool) -> None:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path:
        _atomic_write(Path(path), content)
    if print_report:
        print(content, end="")


def _default_approval(output: Path, segment_id: str) -> Path:
    return output.parent / f".{output.stem}_{segment_id}_h3.approval.json"


def _default_ledger(output: Path, segment_id: str) -> Path:
    return output.parent / f".{output.stem}_{segment_id}_h3.ledger.json"


def _default_prepared(output: Path, segment_id: str) -> Path:
    return output.parent / f".{output.stem}_{segment_id}_h3.prepared.json"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_cost_usd < 0 or args.max_cost_usd > Decimal("5.00"):
        print("provider error: --max-cost-usd must be between 0 and 5.00", file=sys.stderr)
        return EXIT_PROVIDER

    output = Path(args.output).resolve()
    approval_path = (
        Path(args.approval_manifest).resolve()
        if args.approval_manifest
        else _default_approval(output, args.segment_id).resolve()
    )
    ledger_path = (
        Path(args.ledger_file).resolve()
        if args.ledger_file
        else _default_ledger(output, args.segment_id).resolve()
    )
    prepared_request_path = (
        Path(args.prepared_request_output).resolve()
        if args.prepared_request_output
        else _default_prepared(output, args.segment_id).resolve()
    )

    try:
        prepared_episode = PreparedEpisode.model_validate_json(
            Path(args.prepared_input).read_text(encoding="utf-8")
        )
        plan = GenerationPlanEpisode.model_validate_json(
            Path(args.generation_plan).read_text(encoding="utf-8")
        )
        config = MiniMaxH3ProviderConfig.load(args.config)
        segment = find_h3_segment(plan, args.segment_id)
        adapter = MiniMaxH3Adapter(config, route_id=segment.provider_route_id)
        assets = H3CanaryAssetManifest.load(args.assets)
        canary = prepare_h3_canary(
            prepared_episode,
            plan,
            segment_id=args.segment_id,
            asset_manifest=assets,
            adapter=adapter,
        )
        write_prepared_h3_canary(canary, prepared_request_path)
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        MiniMaxH3CanaryError,
        MiniMaxH3ConfigurationError,
    ) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    hashes = sorted(
        item.sha256 for item in canary.reference_bundle.assets if item.sha256 is not None
    )
    common: dict[str, object] = {
        "valid": True,
        "stage": args.stage,
        "segment_id": args.segment_id,
        "provider_route_id": segment.provider_route_id,
        "request_fingerprint": canary.prepared_request.request_fingerprint,
        "model": canary.h3_request.model,
        "resolution": canary.h3_request.resolution,
        "duration": canary.h3_request.duration,
        "authoritative_cost_usd": str(canary.authoritative_cost_usd),
        "max_cost_usd": str(args.max_cost_usd),
        "within_requested_cost_limit": canary.authoritative_cost_usd <= args.max_cost_usd,
        "price_snapshot_id": canary.price_snapshot_id,
        "reference_asset_hashes": hashes,
        "prepared_request": str(prepared_request_path),
        "approval_manifest": str(approval_path),
        "ledger_file": str(ledger_path),
        "credentials_present": bool(os.getenv(config.api_key_env, "").strip()),
        "external_api_calls": 0,
    }
    if canary.authoritative_cost_usd > args.max_cost_usd:
        common.update(
            valid=False,
            budget_gate="blocked_before_provider_submission",
            errors=[
                f"authoritative H3 cost {canary.authoritative_cost_usd} USD exceeds "
                f"limit {args.max_cost_usd} USD"
            ],
        )
        _write_report(common, args.report, args.print_report)
        print("provider error: H3 cost gate blocked provider submission", file=sys.stderr)
        return EXIT_PROVIDER

    if args.stage == "preflight":
        _write_report(common, args.report, args.print_report)
        if not args.print_report:
            print(
                f"Segment: {args.segment_id}\n"
                f"Route: {segment.provider_route_id}\n"
                f"Cost: {canary.authoritative_cost_usd}/{args.max_cost_usd} USD\n"
                f"Credentials present: {common['credentials_present']}\n"
                "External API calls: 0\nPreflight: VALID"
            )
        return EXIT_OK

    if args.stage == "approve":
        try:
            create_h3_approval_manifest(
                segment_id=args.segment_id,
                request_fingerprint=canary.prepared_request.request_fingerprint,
                reference_asset_hashes=hashes,
                model=canary.h3_request.model,
                resolution=canary.h3_request.resolution,
                duration=canary.h3_request.duration,
                authoritative_cost_usd=canary.authoritative_cost_usd,
                max_cost_usd=args.max_cost_usd,
                price_snapshot_id=canary.price_snapshot_id,
                output_path=approval_path,
            )
        except (MiniMaxH3ApprovalError, OSError, ValidationError) as exc:
            print(f"approval error: {exc}", file=sys.stderr)
            return EXIT_APPROVAL
        common["approved"] = True
        _write_report(common, args.report, args.print_report)
        return EXIT_OK

    if args.stage == "reconcile":
        if not args.recovered_task_id or not args.recovery_evidence:
            print(
                "provider error: reconcile requires --recovered-task-id and --recovery-evidence",
                file=sys.stderr,
            )
            return EXIT_PROVIDER
        store = H3ExecutionLedgerStore(ledger_path)
        try:
            record = store.load()
            if record is None:
                raise H3ExecutionLedgerError("cannot reconcile because the H3 ledger does not exist")
            if record.request_fingerprint != canary.prepared_request.request_fingerprint:
                raise H3ExecutionLedgerError("ledger request fingerprint does not match current request")
            if record.authoritative_cost_usd != canary.authoritative_cost_usd:
                raise H3ExecutionLedgerError("ledger authoritative cost does not match current request")
            if record.max_cost_usd != args.max_cost_usd:
                raise H3ExecutionLedgerError("ledger max_cost_usd does not match current request")
            store.reconcile_task_id(
                record,
                task_id=args.recovered_task_id,
                recovery_evidence=args.recovery_evidence,
            )
        except (H3ExecutionLedgerError, OSError, ValidationError) as exc:
            print(f"provider error: {exc}", file=sys.stderr)
            return EXIT_PROVIDER
        common.update(reconciled=True, task_id=args.recovered_task_id)
        _write_report(common, args.report, args.print_report)
        return EXIT_OK

    try:
        verify_h3_approval_manifest(
            approval_path,
            segment_id=args.segment_id,
            request_fingerprint=canary.prepared_request.request_fingerprint,
            reference_asset_hashes=hashes,
            model=canary.h3_request.model,
            resolution=canary.h3_request.resolution,
            duration=canary.h3_request.duration,
            authoritative_cost_usd=canary.authoritative_cost_usd,
            max_cost_usd=args.max_cost_usd,
            price_snapshot_id=canary.price_snapshot_id,
        )
    except MiniMaxH3ApprovalError as exc:
        print(f"approval error: {exc}", file=sys.stderr)
        return EXIT_APPROVAL

    client = MiniMaxH3Client(config)
    executor = MiniMaxH3Executor(
        config,
        client,
        H3ExecutionLedgerStore(ledger_path),
        video_validator=strict_h3_video_validator,
    )
    try:
        final_path = executor.execute(
            canary.prepared_request,
            segment_id=args.segment_id,
            max_cost_usd=args.max_cost_usd,
            output_path=output,
            approval_verified=True,
            resume_only=args.stage == "resume",
        )
    except (MiniMaxH3ExecutionError, H3ExecutionLedgerError, OSError, ValidationError) as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    record = H3ExecutionLedgerStore(ledger_path).load()
    common.update(
        output=str(final_path),
        task_id=record.task_id if record else None,
        status=record.status if record else "unknown",
        external_api_calls=record.external_api_calls if record else 0,
        submission_attempts=record.submission_attempts if record else 0,
    )
    _write_report(common, args.report, args.print_report)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
