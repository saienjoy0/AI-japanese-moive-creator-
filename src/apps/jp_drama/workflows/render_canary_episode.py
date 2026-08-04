"""Run a cost-bounded one-shot Wan 2.7 canary in approval-gated stages."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
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
from ..rendering.ffmpeg import file_sha256


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_PERSISTENCE = 3
EXIT_RENDER = 4
EXIT_VALIDATION = 5
EXIT_PROVIDER = 6
EXIT_APPROVAL = 7


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one Japanese-drama shot through a hard-limited Wan 2.7 canary. "
            "Generate a keyframe first, approve it, then render video and TTS."
        )
    )
    parser.add_argument("--input", required=True, help="PreparedEpisode JSON")
    parser.add_argument("--output", required=True, help="Final one-shot MP4")
    parser.add_argument("--providers", required=True, help="Live provider configuration JSON")
    parser.add_argument("--shot-id", required=True, help="Exactly one source shot ID")
    parser.add_argument(
        "--stage",
        choices=("preflight", "keyframe", "render"),
        default="preflight",
        help="preflight costs nothing; keyframe makes one call; render uses an approved keyframe",
    )
    parser.add_argument(
        "--approved-keyframe",
        help="Human-approved keyframe required by --stage render",
    )
    parser.add_argument(
        "--keyframe-output",
        help="Output path for --stage keyframe (derived from --output by default)",
    )
    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=3,
        help="Hard ceiling for provider submissions; canary mode rejects values above 3",
    )
    parser.add_argument("--work-dir", help="Restart-safe render work directory")
    parser.add_argument("--projects-file", help="LumenX projects file")
    parser.add_argument("--index-file", help="Japanese-drama persistence index")
    parser.add_argument("--report", help="Optional JSON report path")
    parser.add_argument("--reset", action="store_true", help="Discard prior canary render state")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_prepared(path: Path) -> PreparedEpisode:
    return PreparedEpisode.model_validate_json(path.read_text(encoding="utf-8"))


def _default_work_dir(output: Path, shot_id: str) -> Path:
    return output.parent / f".{output.stem}_{shot_id}_canary_work"


def _default_keyframe(output: Path, shot_id: str) -> Path:
    return output.parent / f"{output.stem}_{shot_id}_keyframe.png"


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

    input_path = Path(args.input)
    output_path = Path(args.output)
    try:
        source = _load_prepared(input_path)
        prepared = select_canary_shot(source, args.shot_id)
        config = LiveProviderConfig.load(args.providers)
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except ProviderConfigurationError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    if not prepared.readiness_report.generation_ready:
        print("not ready: selected canary shot is not generation-ready", file=sys.stderr)
        return EXIT_NOT_READY

    approved = Path(args.approved_keyframe).resolve() if args.approved_keyframe else None
    approved_shots = {args.shot_id} if approved else set()
    estimated_render_calls = LiveTaskExecutor.estimate_api_calls(
        prepared,
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
            "keyframe_calls": 1,
            "render_calls_without_approved_keyframe": LiveTaskExecutor.estimate_api_calls(prepared),
            "render_calls_with_approved_keyframe": LiveTaskExecutor.estimate_api_calls(
                prepared,
                approved_keyframe_shots={args.shot_id},
            ),
            "max_api_calls": args.max_api_calls,
            "external_api_calls": 0,
        }
        _write_report(payload, args.report, args.print_report)
        if not args.print_report:
            print(
                f"Shot: {args.shot_id}\n"
                f"Keyframe calls: 1\n"
                f"Render calls after approval: {payload['render_calls_with_approved_keyframe']}\n"
                f"Missing environment: {', '.join(missing_environment) or 'none'}\n"
                "Preflight: VALID"
            )
        return EXIT_OK

    try:
        config.require_environment()
    except ProviderConfigurationError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    if args.stage == "keyframe":
        if args.max_api_calls < 1:
            print("provider error: keyframe stage requires --max-api-calls >= 1", file=sys.stderr)
            return EXIT_PROVIDER
        keyframe_output = (
            Path(args.keyframe_output)
            if args.keyframe_output
            else _default_keyframe(output_path, args.shot_id)
        )
        try:
            executor = LiveTaskExecutor(config, api_call_limit=min(args.max_api_calls, 1))
            generated = executor.generate_canary_keyframe(
                prepared,
                shot_id=args.shot_id,
                output=keyframe_output,
            )
        except Exception as exc:
            print(f"provider error: {exc}", file=sys.stderr)
            return EXIT_PROVIDER
        payload = {
            "valid": True,
            "stage": "keyframe",
            "shot_id": args.shot_id,
            "keyframe": str(generated),
            "keyframe_sha256": file_sha256(generated),
            "external_api_calls": executor.external_api_calls,
            "next_action": "review this image before running --stage render",
        }
        _write_report(payload, args.report, args.print_report)
        if not args.print_report:
            print(
                f"Shot: {args.shot_id}\n"
                f"Keyframe: {generated}\n"
                f"SHA-256: {payload['keyframe_sha256']}\n"
                f"External API calls: {executor.external_api_calls}\n"
                "Status: AWAITING HUMAN APPROVAL"
            )
        return EXIT_OK

    if approved is None or not approved.is_file() or approved.stat().st_size == 0:
        print(
            "approval error: --stage render requires a non-empty --approved-keyframe",
            file=sys.stderr,
        )
        return EXIT_APPROVAL
    if estimated_render_calls > args.max_api_calls:
        print(
            f"provider error: selected render needs {estimated_render_calls} calls, "
            f"above limit {args.max_api_calls}",
            file=sys.stderr,
        )
        return EXIT_PROVIDER

    work_dir = Path(args.work_dir) if args.work_dir else _default_work_dir(output_path, args.shot_id)
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
            api_call_limit=estimated_render_calls,
            approved_keyframes={args.shot_id: approved},
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
            "approved_keyframe": str(approved),
            "approved_keyframe_sha256": file_sha256(approved),
            "requested_api_call_limit": args.max_api_calls,
            "enforced_render_call_limit": estimated_render_calls,
        }
    )
    _write_report(payload, args.report, args.print_report)
    if not args.print_report:
        print(
            f"Shot: {args.shot_id}\n"
            f"Approved keyframe: {approved}\n"
            f"Output: {output_path}\n"
            f"Duration: {report.duration_seconds:.3f}s\n"
            f"External API calls: {report.external_api_calls}/{estimated_render_calls}\n"
            f"Valid: {'YES' if report.valid else 'NO'}"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
