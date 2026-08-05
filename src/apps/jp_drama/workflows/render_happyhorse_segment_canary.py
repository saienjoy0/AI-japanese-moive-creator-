"""Render one approval-gated segment through official HappyHorse 1.1 I2V.

This is an intentionally narrow canary route. It preserves every approved
master-image and first-frame lineage gate, but treats fixed voice identities as
not applicable because the official HappyHorse request has no voice_id input.
The returned MP4 must contain an audio stream; otherwise the canary fails and
the existing external-TTS design remains the fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..assets import AssetBundleError, assess_asset_readiness, load_bundle
from ..assets.models import AssetReadinessIssue, AssetReadinessReport
from ..generation.models import GenerationPlanEpisode, GenerationSegment, PromptBundle
from ..preparation.models import PreparedEpisode
from ..rendering.ffmpeg import FFmpegError, file_sha256, media_has_audio
from ..rendering.happyhorse11 import HappyHorse11I2VModel, require_local_first_frame
from ..rendering.provider_config import LiveProviderConfig, ProviderConfigurationError
from ..rendering.provider_ledger import (
    CanaryProviderLedgerStore,
    ProviderLedgerError,
)


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_PROVIDER = 6
PROTOCOL = "happyhorse-1.1-i2v-official-canary-v1"
MODEL_NAME = HappyHorse11I2VModel.MODEL_NAME


class HappyHorseCanaryError(RuntimeError):
    """The official HappyHorse canary cannot safely proceed."""


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight or render one approved GenerationSegment with the official "
            "HappyHorse 1.1 I2V asynchronous API. No external TTS or voice ID is used."
        )
    )
    parser.add_argument("--prepared-input", required=True)
    parser.add_argument("--generation-plan", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--asset-bundle", required=True)
    parser.add_argument("--providers", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--stage", choices=("preflight", "render"), default="preflight")
    parser.add_argument("--ledger-file")
    parser.add_argument("--report")
    parser.add_argument("--resolution", choices=("720P", "1080P"), default="720P")
    parser.add_argument("--max-api-calls", type=int, default=1)
    parser.add_argument("--max-cost-cny", type=_decimal, default=Decimal("10"))
    parser.add_argument(
        "--cost-reserve-cny",
        type=_decimal,
        default=Decimal("6"),
        help=(
            "Conservative per-video CNY budget reservation. This is not a fixed "
            "exchange-rate conversion of Alibaba Cloud's USD list price."
        ),
    )
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_prepared(path: str | Path) -> PreparedEpisode:
    return PreparedEpisode.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _load_plan(path: str | Path) -> GenerationPlanEpisode:
    return GenerationPlanEpisode.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def _find_segment(plan: GenerationPlanEpisode, segment_id: str) -> GenerationSegment:
    segment = next((item for item in plan.segments if item.segment_id == segment_id), None)
    if segment is None:
        raise HappyHorseCanaryError(f"unknown generation segment: {segment_id}")
    return segment


def allow_provider_native_voice(
    readiness: AssetReadinessReport,
) -> AssetReadinessReport:
    """Relax only the fixed voice-ID errors for this isolated native-audio canary."""

    voice_errors = [
        item for item in readiness.errors if item.code == "voice_profile_not_ready"
    ]
    remaining_errors = [
        item for item in readiness.errors if item.code != "voice_profile_not_ready"
    ]
    warnings = list(readiness.warnings)
    warnings.extend(
        AssetReadinessIssue(
            code="provider_native_voice_uncontrolled",
            severity="warning",
            message=(
                "HappyHorse has no voice_id field; provider-generated voice identity "
                "is not fixed across clips and must be evaluated by a human."
            ),
            character_seed_id=item.character_seed_id,
            segment_id=item.segment_id,
        )
        for item in voice_errors
    )
    return readiness.model_copy(
        update={
            "ready": not remaining_errors,
            "errors": remaining_errors,
            "warnings": warnings,
            "required_voice_character_ids": [],
        }
    )


def build_happyhorse_prompt(bundle: PromptBundle) -> str:
    parts = [
        "Japanese live-action vertical short drama. Preserve the approved first "
        "frame's faces, ages, hairstyles, costumes, body proportions, background, "
        "props, lighting, and screen direction.",
        f"Narrative: {bundle.narrative_summary}",
        f"Visual: {bundle.visual_prompt}",
        f"Motion: {bundle.motion_prompt}",
        f"Camera: {bundle.camera_prompt}",
        f"Timing: {bundle.timed_shot_prompt}",
    ]
    if bundle.dialogue_prompt:
        parts.append(
            "Spoken dialogue: Use natural Japanese and synchronize the visible "
            f"speaker's mouth to this dialogue exactly: {bundle.dialogue_prompt}"
        )
    if bundle.audio_prompt:
        parts.append(f"Audio: {bundle.audio_prompt}")
    else:
        parts.append(
            "Audio: natural Japanese dialogue and restrained location ambience; "
            "no narration and no background music unless the scene explicitly requires it."
        )
    if bundle.negative_constraints:
        parts.append("Constraints: " + "; ".join(bundle.negative_constraints))
    parts.append(
        "Do not add subtitles, captions, logos, watermarks, title cards, or on-screen text."
    )
    return "\n".join(parts)


def _approved_first_frame(bundle, segment: GenerationSegment):
    matches = [
        item
        for item in bundle.assets
        if item.role == "first_frame"
        and item.approval_status == "approved"
        and segment.segment_id in item.required_for_segment_ids
    ]
    if len(matches) != 1:
        raise HappyHorseCanaryError(
            f"HappyHorse render requires exactly one approved first frame for "
            f"{segment.segment_id}; found {len(matches)}"
        )
    frame = matches[0]
    if frame.width is None or frame.height is None:
        raise HappyHorseCanaryError("approved first-frame dimensions are missing")
    if frame.width < 300 or frame.height < 300:
        raise HappyHorseCanaryError(
            "HappyHorse first-frame width and height must each be at least 300 pixels"
        )
    ratio = frame.width / frame.height
    if not 1 / 2.5 <= ratio <= 2.5:
        raise HappyHorseCanaryError(
            "HappyHorse first-frame aspect ratio must be between 1:2.5 and 2.5:1"
        )
    if not frame.asset_path or not frame.asset_sha256:
        raise HappyHorseCanaryError("approved first-frame metadata is incomplete")
    require_local_first_frame(frame.asset_path)
    return frame


def _provider_duration(segment: GenerationSegment) -> int:
    duration = max(
        HappyHorse11I2VModel.MIN_DURATION_SECONDS,
        segment.requested_duration_seconds,
    )
    if duration > HappyHorse11I2VModel.MAX_DURATION_SECONDS:
        raise HappyHorseCanaryError(
            f"segment requests {duration}s but HappyHorse supports at most 15s"
        )
    return duration


def _seed(plan: GenerationPlanEpisode, segment: GenerationSegment, base: int) -> int:
    material = (
        f"{plan.content_digest}|{segment.segment_id}|{MODEL_NAME}|official-canary"
    ).encode("utf-8")
    offset = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
    return (base + offset) % 2_147_483_648


def _request_fingerprint(
    *,
    prompt: str,
    first_frame_sha256: str,
    resolution: str,
    duration: int,
    seed: int,
) -> str:
    payload = json.dumps(
        {
            "protocol": PROTOCOL,
            "model": MODEL_NAME,
            "prompt": prompt,
            "first_frame_sha256": first_frame_sha256,
            "resolution": resolution,
            "duration": duration,
            "seed": seed,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _emit_report(
    payload: dict[str, Any],
    *,
    path: str | None,
    print_report: bool,
) -> None:
    if path:
        _atomic_write_json(Path(path).resolve(), payload)
    if print_report:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _default_ledger(output: Path, segment_id: str) -> Path:
    return output.parent / f".{output.stem}_{segment_id}_happyhorse_ledger.json"


def _base_report(
    *,
    stage: str,
    plan: GenerationPlanEpisode,
    segment: GenerationSegment,
    bundle,
    readiness: AssetReadinessReport,
    frame,
    prompt: str,
    resolution: str,
    duration: int,
    seed: int,
    cost_reserve_cny: Decimal,
) -> dict[str, Any]:
    return {
        "valid": readiness.ready,
        "stage": stage,
        "protocol": PROTOCOL,
        "model": MODEL_NAME,
        "generation_plan_digest": plan.content_digest,
        "segment_id": segment.segment_id,
        "provider_route_id": "dashscope/happyhorse-1.1-i2v",
        "audio_strategy": "provider_native_uncontrolled",
        "voice_id_required": False,
        "external_tts_calls": 0,
        "approved_asset_bundle_digest": bundle.content_digest,
        "asset_readiness": readiness.model_dump(mode="json"),
        "first_frame": {
            "asset_id": frame.asset_id,
            "path": frame.asset_path,
            "sha256": frame.asset_sha256,
            "width": frame.width,
            "height": frame.height,
            "approval_manifest_path": frame.approval_manifest_path,
        },
        "request": {
            "resolution": resolution,
            "duration": duration,
            "seed": seed,
            "prompt": prompt,
            "request_fingerprint": _request_fingerprint(
                prompt=prompt,
                first_frame_sha256=frame.asset_sha256,
                resolution=resolution,
                duration=duration,
                seed=seed,
            ),
        },
        "cost_reserve_cny": str(cost_reserve_cny),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output).resolve()

    try:
        if args.max_api_calls < 0 or args.max_api_calls > 3:
            raise HappyHorseCanaryError("max-api-calls must be between 0 and 3")
        if args.cost_reserve_cny < 0:
            raise HappyHorseCanaryError("cost-reserve-cny must not be negative")
        prepared = _load_prepared(args.prepared_input)
        plan = _load_plan(args.generation_plan)
        segment = _find_segment(plan, args.segment_id)
        bundle = load_bundle(args.asset_bundle)
        config = LiveProviderConfig.load(args.providers)
        readiness = allow_provider_native_voice(
            assess_asset_readiness(
                bundle,
                prepared,
                plan,
                stage="render",
                segment_ids=[segment.segment_id],
            )
        )
        frame = _approved_first_frame(bundle, segment)
        duration = _provider_duration(segment)
        prompt = build_happyhorse_prompt(segment.prompt_bundle)
        seed = _seed(plan, segment, config.dashscope.seed_base)
    except (
        OSError,
        ValueError,
        ValidationError,
        AssetBundleError,
        HappyHorseCanaryError,
    ) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except ProviderConfigurationError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    report = _base_report(
        stage=args.stage,
        plan=plan,
        segment=segment,
        bundle=bundle,
        readiness=readiness,
        frame=frame,
        prompt=prompt,
        resolution=args.resolution,
        duration=duration,
        seed=seed,
        cost_reserve_cny=args.cost_reserve_cny,
    )

    if not readiness.ready:
        report.update(
            {
                "valid": False,
                "status": "blocked",
                "provider_submissions_this_run": 0,
                "errors": [item.message for item in readiness.errors],
            }
        )
        _emit_report(report, path=args.report, print_report=args.print_report)
        print(
            "provider error: approved asset gate blocked HappyHorse submission",
            file=sys.stderr,
        )
        return EXIT_PROVIDER

    if args.stage == "preflight":
        report.update(
            {
                "valid": True,
                "status": "ready_for_single_paid_submission",
                "provider_submissions_this_run": 0,
                "external_api_calls": 0,
                "next_action": (
                    "rerun with --stage render after reviewing the exact request"
                ),
            }
        )
        _emit_report(report, path=args.report, print_report=args.print_report)
        if not args.print_report:
            print(
                f"HappyHorse preflight: READY\n"
                f"Segment: {segment.segment_id}\n"
                f"Duration: {duration}s\n"
                f"Resolution: {args.resolution}\n"
                "Voice ID: not required\n"
                "External API calls: 0"
            )
        return EXIT_OK

    ledger_path = (
        Path(args.ledger_file).resolve()
        if args.ledger_file
        else _default_ledger(output, segment.segment_id).resolve()
    )
    store = CanaryProviderLedgerStore(ledger_path)
    operation_id = f"{segment.segment_id}:happyhorse-1.1-i2v"

    try:
        config.require_environment()
        ledger = store.load_or_create(
            source_digest=plan.content_digest,
            shot_id=segment.segment_id,
            max_api_calls=args.max_api_calls,
            max_cost_cny=args.max_cost_cny,
        )
        record, created = store.begin(
            ledger,
            operation_id=operation_id,
            stage="render",
            operation_type="video",
            provider="dashscope",
            model=MODEL_NAME,
            estimated_cost_cny=args.cost_reserve_cny,
        )

        if record.status == "succeeded" and output.is_file():
            actual_sha = file_sha256(output)
            if record.output_sha256 == actual_sha and media_has_audio(output):
                report.update(
                    {
                        "valid": True,
                        "status": "reused_verified_output",
                        "provider_submissions_this_run": 0,
                        "external_api_calls": 0,
                        "ledger_file": str(ledger_path),
                        "output": str(output),
                        "output_sha256": actual_sha,
                        "audio_stream_present": True,
                    }
                )
                _emit_report(report, path=args.report, print_report=args.print_report)
                return EXIT_OK

        if not created and not record.provider_task_id:
            raise HappyHorseCanaryError(
                "a prior submission may exist but no provider task ID was saved; "
                "refusing a duplicate paid request"
            )

        model = HappyHorse11I2VModel(
            {
                "params": {
                    "resolution": args.resolution,
                    "duration": duration,
                    "watermark": config.dashscope.watermark,
                }
            }
        )
        model.configure_operation(
            resume_task_id=None if created else record.provider_task_id,
            on_task_submitted=lambda task_id, request_id: store.mark_submitted(
                ledger,
                operation_id,
                provider_task_id=task_id,
                provider_request_id=request_id,
            ),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            model.generate(
                prompt,
                str(output),
                img_path=str(frame.asset_path),
                model_name=MODEL_NAME,
                resolution=args.resolution,
                duration=duration,
                watermark=config.dashscope.watermark,
                seed=seed,
            )
        finally:
            model.clear_operation()

        if not output.is_file() or output.stat().st_size == 0:
            raise HappyHorseCanaryError("HappyHorse returned no usable MP4")
        if not media_has_audio(output):
            raise HappyHorseCanaryError(
                "happyhorse_native_audio_missing: the official MP4 has no audio stream; "
                "use the approved external-TTS design instead"
            )

        digest = file_sha256(output)
        store.mark_succeeded(ledger, operation_id, output_sha256=digest)
        report.update(
            {
                "valid": True,
                "status": "succeeded_awaiting_human_review",
                "provider_submissions_this_run": 1 if created else 0,
                "external_api_calls": 1 if created else 0,
                "external_tts_calls": 0,
                "ledger_file": str(ledger_path),
                "provider_task_id": ledger.operations[operation_id].provider_task_id,
                "provider_request_id": ledger.operations[operation_id].provider_request_id,
                "output": str(output),
                "output_sha256": digest,
                "audio_stream_present": True,
                "next_action": (
                    "human-review Japanese speech, lip synchronization, identity, and motion"
                ),
            }
        )
    except (
        OSError,
        ValueError,
        FFmpegError,
        ProviderConfigurationError,
        ProviderLedgerError,
        HappyHorseCanaryError,
        RuntimeError,
    ) as exc:
        try:
            if "ledger" in locals() and operation_id in ledger.operations:
                store.mark_unknown(
                    ledger,
                    operation_id,
                    f"{type(exc).__name__}: {exc}",
                )
        except Exception:
            pass
        report.update(
            {
                "valid": False,
                "status": "failed",
                "provider_submissions_this_run": 1 if "created" in locals() and created else 0,
                "external_api_calls": 1 if "created" in locals() and created else 0,
                "external_tts_calls": 0,
                "ledger_file": str(ledger_path),
                "errors": [str(exc)],
                "fallback": (
                    "retain the existing ApprovedAssetBundle voice-ID gate and use "
                    "external TTS plus muxing"
                ),
            }
        )
        _emit_report(report, path=args.report, print_report=args.print_report)
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    _emit_report(report, path=args.report, print_report=args.print_report)
    if not args.print_report:
        print(
            f"HappyHorse Canary: GENERATED\n"
            f"Segment: {segment.segment_id}\n"
            f"Output: {output}\n"
            "Provider audio stream: present\n"
            "External TTS calls: 0\n"
            "Status: awaiting human review"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
