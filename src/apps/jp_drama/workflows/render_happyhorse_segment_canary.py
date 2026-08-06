"""Render one approval-gated segment through official HappyHorse 1.1.

The existing first-frame I2V Canary remains the default. The references mode
reuses the approved Wan master-reference manifest to test HappyHorse R2V
without generating extra keyframes. Both modes use the same persistent ledger,
save provider task IDs before polling, and resume without another paid POST.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from ..assets import AssetBundleError, assess_asset_readiness, load_bundle
from ..assets.models import AssetReadinessIssue, AssetReadinessReport
from ..assets.wan_references import (
    WanMasterReferenceError,
    WanMasterReferenceManifest,
    build_wan_master_reference_manifest,
)
from ..generation.models import GenerationPlanEpisode, GenerationSegment, PromptBundle
from ..preparation.models import PreparedEpisode
from ..rendering.ffmpeg import (
    FFmpegError,
    black_duration,
    ffprobe_json,
    file_sha256,
    media_has_audio,
)
from ..rendering.happyhorse11 import (
    HappyHorse11I2VModel,
    HappyHorse11R2VModel,
    require_local_first_frame,
)
from ..rendering.provider_config import LiveProviderConfig, ProviderConfigurationError
from ..rendering.provider_ledger import (
    CanaryProviderLedgerStore,
    ProviderLedgerError,
)


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_PROVIDER = 6
InputMode = Literal["first_frame", "references"]
PROTOCOL_I2V = "happyhorse-1.1-i2v-official-canary-v1"
PROTOCOL_R2V = "happyhorse-1.1-r2v-official-canary-v1"


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
            "HappyHorse 1.1 asynchronous API. The default is the existing first-frame "
            "I2V route; references mode uses approved master images for R2V."
        )
    )
    parser.add_argument("--prepared-input", required=True)
    parser.add_argument("--generation-plan", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--asset-bundle", required=True)
    parser.add_argument("--providers", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--input-mode",
        choices=("first_frame", "references"),
        default="first_frame",
    )
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
            "exchange-rate conversion of Alibaba Cloud's list price."
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
    matches = [item for item in plan.segments if item.segment_id == segment_id]
    if len(matches) != 1:
        raise HappyHorseCanaryError(
            f"unknown or duplicate generation segment: {segment_id}"
        )
    return matches[0]


def allow_provider_native_voice(
    readiness: AssetReadinessReport,
) -> AssetReadinessReport:
    """Relax only fixed voice-ID errors for an isolated native-audio Canary."""

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


def add_native_voice_warning(
    readiness: AssetReadinessReport,
    segment: GenerationSegment,
) -> AssetReadinessReport:
    """Record native voice uncertainty when keyframe readiness has no voice gate."""

    if not segment.dialogue_slices:
        return readiness
    if any(item.code == "provider_native_voice_uncontrolled" for item in readiness.warnings):
        return readiness
    speakers = sorted({item.speaker_character_id for item in segment.dialogue_slices})
    warnings = list(readiness.warnings)
    warnings.extend(
        AssetReadinessIssue(
            code="provider_native_voice_uncontrolled",
            severity="warning",
            message=(
                "HappyHorse R2V native voice identity is not fixed and must be "
                "evaluated by a human before route adoption."
            ),
            character_seed_id=speaker,
            segment_id=segment.segment_id,
        )
        for speaker in speakers
    )
    return readiness.model_copy(
        update={
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


def build_happyhorse_reference_prompt(
    bundle: PromptBundle,
    manifest: WanMasterReferenceManifest,
) -> str:
    """Bind every ordered reference to one explicit [Image N] prompt mention."""

    role_descriptions = {
        "character_master": "character identity, age, face, hair, body and costume",
        "location_master": "location architecture, layout, period, lighting and furniture",
        "prop_master": "prop identity, material, color, scale, state and count",
    }
    lines = [
        "Japanese live-action vertical short drama, 9:16.",
        "Use the ordered approved reference images exactly as follows:",
    ]
    for number, reference in enumerate(manifest.references, start=1):
        lines.append(
            f"[Image {number}] is the approved {role_descriptions[reference.role]} "
            f"reference for {reference.subject_id}."
        )
    lines.append(build_happyhorse_prompt(bundle))
    prompt = "\n".join(lines)
    expected = {f"[Image {number}]" for number in range(1, len(manifest.references) + 1)}
    actual = {token for token in expected if token in prompt}
    if actual != expected:
        raise HappyHorseCanaryError(
            "R2V prompt image bindings do not match the approved reference order"
        )
    return prompt


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


def _assert_e01_g01_reference_scope(
    segment: GenerationSegment,
    manifest: WanMasterReferenceManifest,
) -> None:
    if segment.segment_id != "E01-G01":
        return
    subjects = {item.subject_id for item in manifest.references}
    missing = {"C01", "S01", "P03", "P04", "S05"} - subjects
    if missing:
        raise HappyHorseCanaryError(
            "E01-G01 approved reference scope is incomplete: "
            + ", ".join(sorted(missing))
        )


def _provider_duration(segment: GenerationSegment) -> int:
    duration = max(3, segment.requested_duration_seconds)
    if duration > 15:
        raise HappyHorseCanaryError(
            f"segment requests {duration}s but HappyHorse supports at most 15s"
        )
    return duration


def _seed(
    plan: GenerationPlanEpisode,
    segment: GenerationSegment,
    base: int,
    model_name: str,
) -> int:
    material = (
        f"{plan.content_digest}|{segment.segment_id}|{model_name}|official-canary"
    ).encode("utf-8")
    offset = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
    return (base + offset) % 2_147_483_648


def _request_fingerprint(
    *,
    protocol: str,
    model_name: str,
    plan_digest: str,
    bundle_digest: str,
    segment_id: str,
    prompt: str,
    ordered_asset_ids: list[str],
    ordered_asset_hashes: list[str],
    resolution: str,
    ratio: str | None,
    duration: int,
    seed: int,
) -> str:
    payload = json.dumps(
        {
            "protocol": protocol,
            "model": model_name,
            "generation_plan_digest": plan_digest,
            "asset_bundle_digest": bundle_digest,
            "segment_id": segment_id,
            "prompt": prompt,
            "ordered_asset_ids": ordered_asset_ids,
            "ordered_asset_hashes": ordered_asset_hashes,
            "resolution": resolution,
            "ratio": ratio,
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


def _default_ledger(output: Path, segment_id: str, model_name: str) -> Path:
    safe_model = model_name.replace(".", "-")
    return output.parent / f".{output.stem}_{segment_id}_{safe_model}_ledger.json"


def _base_report(
    *,
    stage: str,
    input_mode: InputMode,
    protocol: str,
    model_name: str,
    plan: GenerationPlanEpisode,
    segment: GenerationSegment,
    bundle,
    readiness: AssetReadinessReport,
    frame,
    manifest: WanMasterReferenceManifest | None,
    prompt: str,
    resolution: str,
    duration: int,
    seed: int,
    request_fingerprint: str,
    cost_reserve_cny: Decimal,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "valid": readiness.ready,
        "stage": stage,
        "protocol": protocol,
        "model": model_name,
        "input_mode": input_mode,
        "generation_plan_digest": plan.content_digest,
        "segment_id": segment.segment_id,
        "provider_route_id": f"dashscope/{model_name}",
        "source_provider_route_id": segment.provider_route_id,
        "audio_strategy": "provider_native_uncontrolled",
        "voice_id_required": False,
        "external_tts_calls": 0,
        "approved_asset_bundle_digest": bundle.content_digest,
        "asset_readiness": readiness.model_dump(mode="json"),
        "request": {
            "resolution": resolution,
            "ratio": "9:16" if input_mode == "references" else None,
            "duration": duration,
            "seed": seed,
            "prompt": prompt,
            "request_fingerprint": request_fingerprint,
        },
        "cost_reserve_cny": str(cost_reserve_cny),
    }
    if frame is not None:
        payload["first_frame"] = {
            "asset_id": frame.asset_id,
            "path": frame.asset_path,
            "sha256": frame.asset_sha256,
            "width": frame.width,
            "height": frame.height,
            "approval_manifest_path": frame.approval_manifest_path,
        }
    if manifest is not None:
        payload["reference_manifest_digest"] = manifest.content_digest
        payload["references"] = [
            {
                "order": item.order,
                "asset_id": item.asset_id,
                "subject_id": item.subject_id,
                "role": item.role,
                "sha256": item.asset_sha256,
                "width": item.width,
                "height": item.height,
            }
            for item in manifest.references
        ]
    return payload


def _validate_output(
    output: Path,
    *,
    required_duration: int,
) -> dict[str, Any]:
    if not output.is_file() or output.stat().st_size == 0:
        raise HappyHorseCanaryError("HappyHorse returned no usable MP4")
    if not media_has_audio(output):
        raise HappyHorseCanaryError(
            "happyhorse_native_audio_missing: the official MP4 has no audio stream; "
            "use the approved external-TTS design instead"
        )

    probe = ffprobe_json(output)
    streams = probe.get("streams", [])
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise HappyHorseCanaryError(
            f"expected exactly one video stream, found {len(videos)}"
        )
    video = videos[0]
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise HappyHorseCanaryError("HappyHorse output dimensions are invalid")
    if abs(width / height - 9 / 16) > 0.025:
        raise HappyHorseCanaryError("HappyHorse output is not vertical 9:16")

    duration = float(probe.get("format", {}).get("duration") or 0.0)
    if duration + 0.08 < required_duration:
        raise HappyHorseCanaryError(
            f"HappyHorse output {duration:.3f}s is shorter than {required_duration}s"
        )
    black = max(0.0, black_duration(output))
    if black > 0.25:
        raise HappyHorseCanaryError(
            f"black-frame duration {black:.3f}s exceeds 0.25s"
        )
    fps_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        fps = float(Fraction(str(fps_text)))
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    return {
        "output": str(output),
        "output_sha256": file_sha256(output),
        "width": width,
        "height": height,
        "fps": fps,
        "duration_seconds": duration,
        "video_streams": len(videos),
        "audio_streams": len(audios),
        "audio_stream_present": bool(audios),
        "black_duration_seconds": black,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output).resolve()
    input_mode: InputMode = args.input_mode

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
        duration = _provider_duration(segment)

        frame = None
        manifest = None
        if input_mode == "first_frame":
            model_name = HappyHorse11I2VModel.MODEL_NAME
            protocol = PROTOCOL_I2V
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
            prompt = build_happyhorse_prompt(segment.prompt_bundle)
            ordered_asset_ids = [frame.asset_id]
            ordered_asset_hashes = [frame.asset_sha256]
            ratio = None
        else:
            model_name = HappyHorse11R2VModel.MODEL_NAME
            protocol = PROTOCOL_R2V
            manifest = build_wan_master_reference_manifest(
                prepared,
                plan,
                bundle,
                segment_id=segment.segment_id,
            )
            _assert_e01_g01_reference_scope(segment, manifest)
            readiness = add_native_voice_warning(
                assess_asset_readiness(
                    bundle,
                    prepared,
                    plan,
                    stage="keyframe",
                    segment_ids=[segment.segment_id],
                ),
                segment,
            )
            prompt = build_happyhorse_reference_prompt(
                segment.prompt_bundle,
                manifest,
            )
            ordered_asset_ids = manifest.asset_ids
            ordered_asset_hashes = manifest.asset_hashes
            ratio = "9:16"

        seed = _seed(
            plan,
            segment,
            config.dashscope.seed_base,
            model_name,
        )
        request_fingerprint = _request_fingerprint(
            protocol=protocol,
            model_name=model_name,
            plan_digest=plan.content_digest,
            bundle_digest=bundle.content_digest,
            segment_id=segment.segment_id,
            prompt=prompt,
            ordered_asset_ids=ordered_asset_ids,
            ordered_asset_hashes=ordered_asset_hashes,
            resolution=args.resolution,
            ratio=ratio,
            duration=duration,
            seed=seed,
        )
    except (
        OSError,
        ValueError,
        ValidationError,
        AssetBundleError,
        HappyHorseCanaryError,
        WanMasterReferenceError,
    ) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except ProviderConfigurationError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    report = _base_report(
        stage=args.stage,
        input_mode=input_mode,
        protocol=protocol,
        model_name=model_name,
        plan=plan,
        segment=segment,
        bundle=bundle,
        readiness=readiness,
        frame=frame,
        manifest=manifest,
        prompt=prompt,
        resolution=args.resolution,
        duration=duration,
        seed=seed,
        request_fingerprint=request_fingerprint,
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
                    "review the exact request and rerun with --stage render only "
                    "after explicit paid-generation authorization"
                ),
            }
        )
        _emit_report(report, path=args.report, print_report=args.print_report)
        if not args.print_report:
            print(
                "HappyHorse preflight: READY\n"
                f"Segment: {segment.segment_id}\n"
                f"Input mode: {input_mode}\n"
                f"Model: {model_name}\n"
                f"Duration: {duration}s\n"
                f"Resolution: {args.resolution}\n"
                f"References: {len(manifest.references) if manifest else 1}\n"
                "Voice ID: not required\n"
                "External API calls: 0"
            )
        return EXIT_OK

    ledger_path = (
        Path(args.ledger_file).resolve()
        if args.ledger_file
        else _default_ledger(output, segment.segment_id, model_name).resolve()
    )
    store = CanaryProviderLedgerStore(ledger_path)
    operation_id = f"{segment.segment_id}:{model_name}"

    try:
        config.require_environment()
        ledger = store.load_or_create(
            source_digest=request_fingerprint,
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
            model=model_name,
            estimated_cost_cny=args.cost_reserve_cny,
        )

        if record.status == "succeeded" and output.is_file():
            facts = _validate_output(output, required_duration=duration)
            if record.output_sha256 == facts["output_sha256"]:
                report.update(
                    {
                        "valid": True,
                        "status": "reused_verified_output",
                        "provider_submissions_this_run": 0,
                        "external_api_calls": 0,
                        "ledger_file": str(ledger_path),
                        **facts,
                    }
                )
                _emit_report(
                    report,
                    path=args.report,
                    print_report=args.print_report,
                )
                return EXIT_OK

        if not created and not record.provider_task_id:
            raise HappyHorseCanaryError(
                "a prior submission may exist but no provider task ID was saved; "
                "refusing a duplicate paid request"
            )

        model_config = {
            "params": {
                "resolution": args.resolution,
                "duration": duration,
                "watermark": config.dashscope.watermark,
            }
        }
        if input_mode == "references":
            model_config["params"]["ratio"] = "9:16"
            model = HappyHorse11R2VModel(model_config)
        else:
            model = HappyHorse11I2VModel(model_config)

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
            if input_mode == "references":
                model.generate(
                    prompt,
                    str(output),
                    model_name=model_name,
                    reference_image_paths=manifest.asset_paths,
                    resolution=args.resolution,
                    ratio="9:16",
                    duration=duration,
                    watermark=config.dashscope.watermark,
                    seed=seed,
                )
            else:
                model.generate(
                    prompt,
                    str(output),
                    img_path=str(frame.asset_path),
                    model_name=model_name,
                    resolution=args.resolution,
                    duration=duration,
                    watermark=config.dashscope.watermark,
                    seed=seed,
                )
        finally:
            model.clear_operation()

        facts = _validate_output(output, required_duration=duration)
        store.mark_succeeded(
            ledger,
            operation_id,
            output_sha256=facts["output_sha256"],
        )
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
                **facts,
                "next_action": (
                    "human-review Japanese speech, lip synchronization, identity, "
                    "reference adherence, shot order, and motion"
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
                "provider_submissions_this_run": (
                    1 if "created" in locals() and created else 0
                ),
                "external_api_calls": (
                    1 if "created" in locals() and created else 0
                ),
                "external_tts_calls": 0,
                "ledger_file": str(ledger_path),
                "errors": [str(exc)],
                "fallback": (
                    "retain the existing approved I2V, H3, or external-TTS paths; "
                    "do not expand HappyHorse R2V to more segments"
                ),
            }
        )
        _emit_report(report, path=args.report, print_report=args.print_report)
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    _emit_report(report, path=args.report, print_report=args.print_report)
    if not args.print_report:
        print(
            "HappyHorse Canary: GENERATED\n"
            f"Segment: {segment.segment_id}\n"
            f"Input mode: {input_mode}\n"
            f"Output: {output}\n"
            "Provider audio stream: present\n"
            "External TTS calls: 0\n"
            "Status: awaiting human review"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
