"""Run a restart-safe, cost-bounded one-shot Wan 2.7 canary."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from ..persistence import (
    LumenXProjectStore,
    PersistenceConflictError,
    PersistenceError,
    PersistenceNotReadyError,
    PersistenceVerificationError,
)
from ..preparation.models import PreparedEpisode
from ..rendering import (
    LiveProviderConfig,
    LiveTaskExecutor,
    ProviderCallLimitError,
    ProviderConfigurationError,
    RenderExecutionError,
    RenderGraphRunner,
    RenderStateConflictError,
    RenderTaskFailedError,
    RenderValidationError,
    select_canary_shot,
)
from ..rendering.approval import (
    ApprovalError,
    create_approval_manifest,
    load_and_verify_approval,
    png_dimensions,
)
from ..rendering.ffmpeg import file_sha256
from ..rendering.provider_ledger import (
    CanaryProviderLedgerStore,
    ProviderLedgerError,
)


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_PERSISTENCE = 3
EXIT_RENDER = 4
EXIT_VALIDATION = 5
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
            "Run one Japanese-drama shot through a persistent-budget Wan 2.7 canary. "
            "Generate, visually approve, then render without duplicate paid submissions."
        )
    )
    parser.add_argument("--input", required=True, help="PreparedEpisode JSON")
    parser.add_argument("--output", required=True, help="Final one-shot MP4")
    parser.add_argument("--providers", required=True, help="Live provider configuration JSON")
    parser.add_argument("--shot-id", required=True, help="Exactly one source shot ID")
    parser.add_argument(
        "--stage",
        choices=("preflight", "keyframe", "approve", "render"),
        default="preflight",
        help=(
            "preflight costs nothing; keyframe submits one image; approve writes a "
            "hash-bound manifest; render resumes/submits only remaining operations"
        ),
    )
    parser.add_argument(
        "--approved-keyframe",
        help="Keyframe to approve; render verifies it against --approval-manifest",
    )
    parser.add_argument(
        "--approval-manifest",
        help="Approval manifest path (derived from --output by default)",
    )
    parser.add_argument(
        "--keyframe-output",
        help="Output path for --stage keyframe (derived from --output by default)",
    )
    parser.add_argument(
        "--ledger-file",
        help="Persistent provider ledger outside resettable render state",
    )
    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=3,
        help="Immutable cumulative provider submission ceiling for all canary stages",
    )
    parser.add_argument(
        "--max-cost-cny",
        type=_decimal,
        default=Decimal("5.0"),
        help="Immutable cumulative estimated provider cost ceiling in CNY",
    )
    parser.add_argument("--work-dir", help="Restart-safe render work directory")
    parser.add_argument("--projects-file", help="LumenX projects file")
    parser.add_argument("--index-file", help="Japanese-drama persistence index")
    parser.add_argument("--report", help="Optional JSON report path")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Discard render outputs/state only; provider ledger is intentionally preserved",
    )
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_prepared(path: Path) -> PreparedEpisode:
    return PreparedEpisode.model_validate_json(path.read_text(encoding="utf-8"))


def _default_work_dir(output: Path, shot_id: str) -> Path:
    return output.parent / f".{output.stem}_{shot_id}_canary_work"


def _default_keyframe(output: Path, shot_id: str) -> Path:
    return output.parent / f"{output.stem}_{shot_id}_keyframe.png"


def _default_approval(output: Path, shot_id: str) -> Path:
    return output.parent / f"{output.stem}_{shot_id}_keyframe.approval.json"


def _default_ledger(output: Path, shot_id: str) -> Path:
    return output.parent / f".{output.stem}_{shot_id}_provider_ledger.json"


def _keyframe_operation_id(prepared: PreparedEpisode, shot_id: str) -> str:
    node = next(
        (
            item
            for item in prepared.render_graph.nodes
            if item.shot_id == shot_id
            and item.task_type in {"generate_video", "generate_native_av", "generate_image"}
        ),
        None,
    )
    if node is None:
        raise ValueError(f"shot cannot generate a keyframe: {shot_id}")
    return f"{node.task_id}:canary-keyframe"


def _write_report(payload: dict[str, object], path: str | None, print_report: bool) -> None:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path:
        report_path = Path(path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(content, encoding="utf-8")
    if print_report:
        print(content, end="")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_api_calls < 0 or args.max_api_calls > 3:
        print("provider error: --max-api-calls must be between 0 and 3", file=sys.stderr)
        return EXIT_PROVIDER
    if args.max_cost_cny < 0 or args.max_cost_cny > Decimal("50"):
        print("provider error: --max-cost-cny must be between 0 and 50", file=sys.stderr)
        return EXIT_PROVIDER

    input_path = Path(args.input)
    output_path = Path(args.output)
    try:
        source = _load_prepared(input_path)
        config = LiveProviderConfig.load(args.providers)
        prepared = select_canary_shot(
            source,
            args.shot_id,
            target_duration_seconds=config.dashscope.provider_clip_seconds,
        )
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except ProviderConfigurationError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    if not prepared.readiness_report.generation_ready:
        print("not ready: selected canary shot is not generation-ready", file=sys.stderr)
        return EXIT_NOT_READY

    work_dir = Path(args.work_dir) if args.work_dir else _default_work_dir(output_path, args.shot_id)
    keyframe_path = (
        Path(args.keyframe_output)
        if args.keyframe_output
        else _default_keyframe(output_path, args.shot_id)
    ).resolve()
    approval_path = (
        Path(args.approval_manifest)
        if args.approval_manifest
        else _default_approval(output_path, args.shot_id)
    ).resolve()
    ledger_path = (
        Path(args.ledger_file)
        if args.ledger_file
        else _default_ledger(output_path, args.shot_id)
    ).resolve()
    ledger_store = CanaryProviderLedgerStore(ledger_path)
    try:
        ledger = ledger_store.load_or_create(
            source_digest=prepared.source_digest,
            shot_id=args.shot_id,
            max_api_calls=args.max_api_calls,
            max_cost_cny=args.max_cost_cny,
        )
    except (ProviderLedgerError, OSError, ValidationError) as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    approved_shots = {args.shot_id}
    keyframe_calls = 1
    keyframe_cost = config.dashscope.estimate_image_cost_cny()
    render_calls = LiveTaskExecutor.estimate_api_calls(
        prepared,
        approved_keyframe_shots=approved_shots,
    )
    render_cost = LiveTaskExecutor.estimate_cost_cny(
        prepared,
        config,
        approved_keyframe_shots=approved_shots,
    )
    missing_environment = config.dashscope.missing_environment()

    if args.stage == "preflight":
        payload = {
            "valid": True,
            "stage": "preflight",
            "shot_id": args.shot_id,
            "project_id": prepared.project_draft.project_id,
            "target_duration_seconds": prepared.project_draft.target_duration_seconds,
            "provider_manifest": config.provider_manifest,
            "required_environment": config.dashscope.required_environment(),
            "missing_environment": missing_environment,
            "credentials_present": not missing_environment,
            "keyframe_calls": keyframe_calls,
            "keyframe_estimated_cost_cny": str(keyframe_cost),
            "render_calls_after_approval": render_calls,
            "render_estimated_cost_cny": str(render_cost),
            "cumulative_estimated_cost_cny": str(keyframe_cost + render_cost),
            "max_api_calls": args.max_api_calls,
            "max_cost_cny": str(args.max_cost_cny),
            "ledger_file": str(ledger_path),
            "committed_api_calls": ledger.committed_api_calls,
            "committed_cost_cny": str(ledger.committed_cost_cny),
            "external_api_calls": 0,
        }
        _write_report(payload, args.report, args.print_report)
        if not args.print_report:
            print(
                f"Shot: {args.shot_id}\n"
                f"Native canary duration: {prepared.project_draft.target_duration_seconds:.3f}s\n"
                f"Keyframe: {keyframe_calls} call / {keyframe_cost} CNY\n"
                f"Render after approval: {render_calls} calls / {render_cost} CNY\n"
                f"Persistent ledger: {ledger_path}\n"
                f"Committed: {ledger.committed_api_calls}/{args.max_api_calls} calls, "
                f"{ledger.committed_cost_cny}/{args.max_cost_cny} CNY\n"
                f"Missing environment: {', '.join(missing_environment) or 'none'}\n"
                "Preflight: VALID"
            )
        return EXIT_OK

    if args.stage == "approve":
        candidate = (
            Path(args.approved_keyframe).resolve()
            if args.approved_keyframe
            else keyframe_path
        )
        if not candidate.is_file() or candidate.stat().st_size == 0:
            print(
                f"approval error: keyframe does not exist or is empty: {candidate}",
                file=sys.stderr,
            )
            return EXIT_APPROVAL
        operation_id = _keyframe_operation_id(prepared, args.shot_id)
        record = ledger.operations.get(operation_id)
        if record is None or record.status != "succeeded":
            print(
                "approval error: keyframe provider operation has not succeeded in this ledger",
                file=sys.stderr,
            )
            return EXIT_APPROVAL
        if record.output_sha256 and file_sha256(candidate) != record.output_sha256:
            print(
                "approval error: keyframe does not match the provider ledger artifact",
                file=sys.stderr,
            )
            return EXIT_APPROVAL
        try:
            manifest = create_approval_manifest(
                shot_id=args.shot_id,
                asset_path=candidate,
                generated_by=f"dashscope/{config.dashscope.image_model}",
                operation_id=operation_id,
                output_path=approval_path,
            )
        except (ApprovalError, OSError) as exc:
            print(f"approval error: {exc}", file=sys.stderr)
            return EXIT_APPROVAL
        payload = {
            "valid": True,
            "stage": "approve",
            "shot_id": args.shot_id,
            "approval_manifest": str(approval_path),
            "approved_keyframe": manifest.asset_path,
            "approved_keyframe_sha256": manifest.asset_sha256,
            "dimensions": f"{manifest.width}x{manifest.height}",
            "operation_id": manifest.operation_id,
            "external_api_calls": 0,
        }
        _write_report(payload, args.report, args.print_report)
        if not args.print_report:
            print(
                f"Shot: {args.shot_id}\n"
                f"Approved keyframe: {manifest.asset_path}\n"
                f"Approval manifest: {approval_path}\n"
                f"SHA-256: {manifest.asset_sha256}\n"
                "Status: APPROVED"
            )
        return EXIT_OK

    try:
        config.require_environment()
    except ProviderConfigurationError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    if args.stage == "keyframe":
        try:
            executor = LiveTaskExecutor(
                config,
                api_call_limit=args.max_api_calls,
                ledger_store=ledger_store,
                ledger=ledger,
            )
            generated = executor.generate_canary_keyframe(
                prepared,
                shot_id=args.shot_id,
                output=keyframe_path,
            )
            width, height = png_dimensions(generated)
        except Exception as exc:
            print(f"provider error: {exc}", file=sys.stderr)
            return EXIT_PROVIDER
        payload = {
            "valid": True,
            "stage": "keyframe",
            "shot_id": args.shot_id,
            "keyframe": str(generated),
            "keyframe_sha256": file_sha256(generated),
            "dimensions": f"{width}x{height}",
            "ledger_file": str(ledger_path),
            "cumulative_api_calls": ledger.committed_api_calls,
            "cumulative_cost_cny": str(ledger.committed_cost_cny),
            "next_action": "review this image, then run --stage approve",
        }
        _write_report(payload, args.report, args.print_report)
        if not args.print_report:
            print(
                f"Shot: {args.shot_id}\n"
                f"Keyframe: {generated}\n"
                f"SHA-256: {payload['keyframe_sha256']}\n"
                f"Cumulative calls: {ledger.committed_api_calls}/{args.max_api_calls}\n"
                f"Cumulative cost: {ledger.committed_cost_cny}/{args.max_cost_cny} CNY\n"
                "Status: AWAITING HUMAN APPROVAL"
            )
        return EXIT_OK

    try:
        manifest, approved = load_and_verify_approval(
            approval_path,
            expected_shot_id=args.shot_id,
            expected_generated_by=f"dashscope/{config.dashscope.image_model}",
        )
    except (ApprovalError, OSError) as exc:
        print(f"approval error: {exc}", file=sys.stderr)
        return EXIT_APPROVAL
    if args.approved_keyframe and Path(args.approved_keyframe).resolve() != approved:
        print(
            "approval error: --approved-keyframe does not match the approval manifest",
            file=sys.stderr,
        )
        return EXIT_APPROVAL

    projects_file = (
        Path(args.projects_file)
        if args.projects_file
        else work_dir / "lumenx" / "projects.json"
    )
    index_file = (
        Path(args.index_file)
        if args.index_file
        else work_dir / "lumenx" / "persistence_index.json"
    )
    if args.reset:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        if output_path.exists():
            output_path.unlink()

    store = LumenXProjectStore(projects_file=projects_file, index_file=index_file)
    try:
        persistence = store.save(prepared)
    except PersistenceNotReadyError as exc:
        print(f"not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY
    except (PersistenceConflictError, PersistenceVerificationError, PersistenceError) as exc:
        print(f"persistence error: {exc}", file=sys.stderr)
        return EXIT_PERSISTENCE

    try:
        executor = LiveTaskExecutor(
            config,
            api_call_limit=args.max_api_calls,
            approved_keyframes={args.shot_id: approved},
            ledger_store=ledger_store,
            ledger=ledger,
        )
        runner = RenderGraphRunner(
            prepared,
            output_file=output_path,
            work_dir=work_dir,
            executor=executor,
            persistence_status=persistence.status,
        )
        report = runner.run()
    except RenderValidationError as exc:
        print(f"validation error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    except (
        ProviderCallLimitError,
        RenderTaskFailedError,
        RenderStateConflictError,
        RenderExecutionError,
        ProviderConfigurationError,
    ) as exc:
        print(f"render error: {exc}", file=sys.stderr)
        return EXIT_RENDER
    except Exception as exc:
        print(f"unexpected render error: {exc}", file=sys.stderr)
        return EXIT_RENDER

    payload = json.loads(report.to_canonical_json())
    payload.update(
        {
            "stage": "render",
            "shot_id": args.shot_id,
            "approval_manifest": str(approval_path),
            "approved_keyframe": str(approved),
            "approved_keyframe_sha256": manifest.asset_sha256,
            "requested_api_call_limit": args.max_api_calls,
            "requested_cost_limit_cny": str(args.max_cost_cny),
            "ledger_file": str(ledger_path),
            "cumulative_api_calls": ledger.committed_api_calls,
            "cumulative_cost_cny": str(ledger.committed_cost_cny),
        }
    )
    _write_report(payload, args.report, args.print_report)
    if not args.print_report:
        print(
            f"Shot: {args.shot_id}\n"
            f"Approved keyframe: {approved}\n"
            f"Output: {output_path}\n"
            f"Duration: {report.duration_seconds:.3f}s\n"
            f"Cumulative calls: {ledger.committed_api_calls}/{args.max_api_calls}\n"
            f"Cumulative cost: {ledger.committed_cost_cny}/{args.max_cost_cny} CNY\n"
            f"Valid: {'YES' if report.valid else 'NO'}"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
