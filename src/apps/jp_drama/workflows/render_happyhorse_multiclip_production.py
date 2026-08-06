"""Preflight, render, and assemble one HappyHorse multi-clip production segment."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..production.happyhorse_multiclip import (
    HappyHorseMultiClipError,
    assemble_clips,
    atomic_write_json,
    build_preflight_report,
    file_sha256,
    load_plan,
    materialize_first_frame,
)
from ..rendering.ffmpeg import FFmpegError, media_has_audio
from ..rendering.happyhorse11 import HappyHorse11I2VModel
from ..rendering.provider_config import LiveProviderConfig, ProviderConfigurationError
from ..rendering.provider_ledger import CanaryProviderLedgerStore, ProviderLedgerError


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_PROVIDER = 6
PROTOCOL = "happyhorse-1.1-i2v-multiclip-production-v1"
MODEL_NAME = HappyHorse11I2VModel.MODEL_NAME


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a SHA-bound multi-clip HappyHorse production plan. Preflight and "
            "assembly make no provider calls; render requires an exact approval digest."
        )
    )
    parser.add_argument("--plan", required=True)
    parser.add_argument("--providers", required=True)
    parser.add_argument("--repository-root", default=".")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--stage",
        choices=("preflight", "render", "assemble", "full"),
        default="preflight",
    )
    parser.add_argument("--approval-digest")
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--ledger-file")
    parser.add_argument("--report")
    parser.add_argument("--max-api-calls", type=int, default=4)
    parser.add_argument("--max-cost-cny", type=_decimal, default=Decimal("20"))
    parser.add_argument(
        "--cost-reserve-cny-per-clip",
        type=_decimal,
        default=Decimal("4"),
    )
    parser.add_argument("--print-report", action="store_true")
    return parser


def _emit(payload: dict[str, Any], *, path: str | None, print_report: bool) -> None:
    if path:
        atomic_write_json(path, payload)
    if print_report:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _paths(output_dir: Path, source_segment_id: str) -> tuple[Path, Path, Path]:
    raw = output_dir / "raw"
    ledger = output_dir / f".{source_segment_id}_happyhorse_multiclip_ledger.json"
    final = output_dir / f"{source_segment_id}.production.mp4"
    return raw, ledger, final


def _render(
    *,
    plan,
    config: LiveProviderConfig,
    preflight: dict[str, Any],
    output_dir: Path,
    ledger_file: Path,
    max_api_calls: int,
    max_cost_cny: Decimal,
    reserve_each: Decimal,
    repository_root: Path,
) -> dict[str, Any]:
    config.require_environment()
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    store = CanaryProviderLedgerStore(ledger_file)
    ledger = store.load_or_create(
        source_digest=plan.content_digest,
        shot_id=plan.source_segment_id,
        max_api_calls=max_api_calls,
        max_cost_cny=max_cost_cny,
    )
    results: list[dict[str, Any]] = []
    submissions = 0

    for clip, request in zip(plan.clips, preflight["requests"], strict=True):
        operation_id = request["operation_id"]
        output = raw_dir / f"{clip.clip_id}.mp4"
        record, created = store.begin(
            ledger,
            operation_id=operation_id,
            stage="render",
            operation_type="video",
            provider="dashscope",
            model=MODEL_NAME,
            estimated_cost_cny=reserve_each,
        )
        if record.status == "succeeded" and output.is_file():
            digest = file_sha256(output)
            if record.output_sha256 == digest and (
                not clip.requires_audio_stream or media_has_audio(output)
            ):
                results.append(
                    {
                        "clip_id": clip.clip_id,
                        "status": "reused_verified_output",
                        "output": str(output),
                        "output_sha256": digest,
                        "provider_task_id": record.provider_task_id,
                        "audio_stream_present": media_has_audio(output),
                    }
                )
                continue
        if not created and not record.provider_task_id:
            raise HappyHorseMultiClipError(
                f"{clip.clip_id}: prior submission may exist without a saved task ID; "
                "refusing a duplicate paid request"
            )

        model = HappyHorse11I2VModel(
            {
                "params": {
                    "resolution": plan.output.resolution,
                    "duration": clip.provider_request_duration_seconds,
                    "watermark": config.dashscope.watermark,
                }
            }
        )
        model.configure_operation(
            resume_task_id=None if created else record.provider_task_id,
            on_task_submitted=lambda task_id, request_id, op=operation_id: (
                store.mark_submitted(
                    ledger,
                    op,
                    provider_task_id=task_id,
                    provider_request_id=request_id,
                )
            ),
        )
        try:
            source_frame = (repository_root / clip.first_frame_path).resolve()
            frame = materialize_first_frame(
                source_frame,
                raw_dir / "first_frames",
                expected_sha256=clip.first_frame_sha256,
            )
            model.generate(
                request["prompt"],
                str(output),
                img_path=str(frame),
                model_name=MODEL_NAME,
                resolution=plan.output.resolution,
                duration=clip.provider_request_duration_seconds,
                watermark=config.dashscope.watermark,
                seed=request["seed"],
            )
            if not output.is_file() or output.stat().st_size == 0:
                raise HappyHorseMultiClipError(
                    f"{clip.clip_id}: HappyHorse returned no usable MP4"
                )
            audio_present = media_has_audio(output)
            if clip.requires_audio_stream and not audio_present:
                raise HappyHorseMultiClipError(
                    f"{clip.clip_id}: happyhorse_native_audio_missing"
                )
            digest = file_sha256(output)
            store.mark_succeeded(ledger, operation_id, output_sha256=digest)
        except Exception as exc:
            try:
                store.mark_unknown(
                    ledger,
                    operation_id,
                    f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
            raise
        finally:
            model.clear_operation()
        submissions += 1 if created else 0
        results.append(
            {
                "clip_id": clip.clip_id,
                "status": "succeeded",
                "output": str(output),
                "output_sha256": digest,
                "provider_task_id": ledger.operations[operation_id].provider_task_id,
                "provider_request_id": ledger.operations[operation_id].provider_request_id,
                "audio_stream_present": audio_present,
            }
        )
    return {
        "valid": True,
        "status": "clips_ready_for_assembly",
        "clips": results,
        "provider_submissions_this_run": submissions,
        "external_api_calls": submissions,
        "ledger_file": str(ledger_file),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository_root = Path(args.repository_root).resolve()
    output_dir = Path(args.output_dir).resolve()

    try:
        plan = load_plan(args.plan)
        config = LiveProviderConfig.load(args.providers)
        preflight = build_preflight_report(
            plan,
            repository_root=args.repository_root,
            seed_base=config.dashscope.seed_base,
            resolution=plan.output.resolution,
            max_api_calls=args.max_api_calls,
            max_cost_cny=str(args.max_cost_cny),
            cost_reserve_cny_per_clip=str(args.cost_reserve_cny_per_clip),
            missing_environment=config.dashscope.missing_environment(),
        )
    except (
        OSError,
        ValueError,
        HappyHorseMultiClipError,
        ProviderConfigurationError,
    ) as exc:
        payload = {
            "valid": False,
            "stage": args.stage,
            "protocol": PROTOCOL,
            "external_api_calls": 0,
            "errors": [str(exc)],
        }
        _emit(payload, path=args.report, print_report=args.print_report)
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if args.stage == "preflight":
        _emit(preflight, path=args.report, print_report=args.print_report)
        return EXIT_OK if preflight["valid"] else EXIT_PROVIDER

    if not preflight["valid"]:
        _emit(preflight, path=args.report, print_report=args.print_report)
        print("provider error: preflight limits are invalid", file=sys.stderr)
        return EXIT_PROVIDER

    raw_dir, default_ledger, final_output = _paths(
        output_dir,
        plan.source_segment_id,
    )
    ledger_file = (
        Path(args.ledger_file).resolve() if args.ledger_file else default_ledger
    )

    render_payload: dict[str, Any] | None = None
    try:
        if args.stage in {"render", "full"}:
            if not args.execute_paid:
                raise HappyHorseMultiClipError(
                    f"--stage {args.stage} requires --execute-paid"
                )
            if args.approval_digest != preflight["approval_digest"]:
                raise HappyHorseMultiClipError(
                    "approval digest does not match the current four-clip preflight"
                )
            render_payload = _render(
                plan=plan,
                config=config,
                preflight=preflight,
                output_dir=output_dir,
                ledger_file=ledger_file,
                max_api_calls=args.max_api_calls,
                max_cost_cny=args.max_cost_cny,
                reserve_each=args.cost_reserve_cny_per_clip,
                repository_root=repository_root,
            )
            if args.stage == "render":
                payload = {**preflight, **render_payload, "stage": "render"}
                _emit(payload, path=args.report, print_report=args.print_report)
                return EXIT_OK

        if args.stage in {"assemble", "full"}:
            raw_outputs = [raw_dir / f"{clip.clip_id}.mp4" for clip in plan.clips]
            for clip, raw in zip(plan.clips, raw_outputs, strict=True):
                if not raw.is_file() or raw.stat().st_size == 0:
                    raise HappyHorseMultiClipError(
                        f"cannot assemble before rendering {clip.clip_id}"
                    )
                if clip.requires_audio_stream and not media_has_audio(raw):
                    raise HappyHorseMultiClipError(
                        f"cannot assemble {clip.clip_id}: required audio stream is missing"
                    )
            final = assemble_clips(
                plan,
                raw_outputs=raw_outputs,
                output_path=final_output,
            )
            if not media_has_audio(final):
                raise HappyHorseMultiClipError(
                    "assembled production MP4 has no audio stream"
                )
            payload = {
                **preflight,
                "valid": True,
                "stage": "full" if args.stage == "full" else "assemble",
                "status": "production_segment_ready_for_human_review",
                "render": render_payload,
                "final_output": str(final),
                "final_output_sha256": file_sha256(final),
                "final_duration_seconds": plan.output.duration_seconds,
                "audio_stream_present": True,
                "external_api_calls": (
                    render_payload["external_api_calls"] if render_payload else 0
                ),
                "next_action": (
                    "human-review identity, motion, Japanese internal monologue, "
                    "audio continuity, and cut timing"
                ),
            }
            _emit(payload, path=args.report, print_report=args.print_report)
            return EXIT_OK
    except (
        OSError,
        ValueError,
        RuntimeError,
        FFmpegError,
        ProviderConfigurationError,
        ProviderLedgerError,
        HappyHorseMultiClipError,
    ) as exc:
        payload = {
            **preflight,
            "valid": False,
            "stage": args.stage,
            "status": "failed",
            "external_api_calls": (
                render_payload["external_api_calls"] if render_payload else 0
            ),
            "ledger_file": str(ledger_file),
            "errors": [str(exc)],
        }
        _emit(payload, path=args.report, print_report=args.print_report)
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
