"""Approval-gated HappyHorse 1.1 R2V canary for one Japanese-drama segment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from ..assets import load_bundle
from ..assets.bundle import prepared_content_digest, write_bundle
from ..assets.reference_resolution import (
    ReferenceResolutionError,
    assess_reference_input_readiness,
    build_reference_selection_manifest,
    publish_reference_selection,
)
from ..generation.happyhorse_r2v import (
    HAPPYHORSE_R2V_ROUTE_ID,
    HappyHorsePlanError,
    derive_happyhorse_r2v_plan_and_bundle,
)
from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..preparation.models import PreparedEpisode
from ..production.reference_prompt import (
    ReferencePromptError,
    build_reference_prompt,
    load_creative_override,
)
from ..rendering.ffmpeg import (
    FFmpegError,
    black_duration,
    ffprobe_json,
    file_sha256,
)
from ..rendering.happyhorse11 import HappyHorse11R2VModel
from ..rendering.happyhorse_r2v_contract import (
    ApprovalContractError,
    HappyHorseR2VApprovalManifest,
    hash_binding,
    load_approval_manifest,
    write_approval_manifest,
)
from ..rendering.provider_config import LiveProviderConfig, ProviderConfigurationError
from ..rendering.provider_ledger import (
    CanaryProviderLedgerStore,
    ProviderLedgerError,
)


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_PROVIDER = 6
PROTOCOL = "happyhorse-1.1-r2v-official-canary-v1"
TASK_EXPIRY_GUARD_HOURS = 23


class HappyHorseR2VCanaryError(RuntimeError):
    """The R2V canary cannot safely proceed."""


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal value: {value}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight, render, or resume one approved Japanese-drama segment "
            "through the official HappyHorse 1.1 R2V asynchronous API."
        )
    )
    parser.add_argument("--prepared-input", required=True)
    parser.add_argument("--source-generation-plan", required=True)
    parser.add_argument("--source-asset-bundle", required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--providers", required=True)
    parser.add_argument("--creative-override")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--stage",
        choices=("preflight", "render", "resume"),
        default="preflight",
    )
    parser.add_argument("--approval-manifest", required=True)
    parser.add_argument("--approval-digest")
    parser.add_argument("--ledger-file")
    parser.add_argument("--published-reference-manifest")
    parser.add_argument("--derived-generation-plan-output")
    parser.add_argument("--derived-asset-bundle-output")
    parser.add_argument("--report")
    parser.add_argument("--resolution", choices=("720P", "1080P"), default="720P")
    parser.add_argument(
        "--audio-strategy",
        choices=("native_audio", "external_tts", "silent"),
        default="native_audio",
    )
    parser.add_argument("--price-snapshot-id", required=True)
    parser.add_argument("--quoted-cost-cny", type=_decimal, required=True)
    parser.add_argument("--max-cost-cny", type=_decimal, default=Decimal("10"))
    parser.add_argument("--reference-url-lease-seconds", type=int, default=3600)
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_prepared(path: str | Path) -> PreparedEpisode:
    return PreparedEpisode.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _load_plan(path: str | Path) -> GenerationPlanEpisode:
    return GenerationPlanEpisode.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def _find_segment(
    plan: GenerationPlanEpisode,
    segment_id: str,
) -> GenerationSegment:
    matches = [item for item in plan.segments if item.segment_id == segment_id]
    if len(matches) != 1:
        raise HappyHorseR2VCanaryError(
            f"unknown or duplicate generation segment: {segment_id}"
        )
    return matches[0]


def _endpoint_binding(config: LiveProviderConfig) -> tuple[str, str, str]:
    endpoint = config.dashscope.endpoint_base_url()
    if endpoint is None:
        raise ProviderConfigurationError(
            "preflight requires DASHSCOPE_BASE_URL or DASHSCOPE_WORKSPACE_ID "
            "so the reviewed request is bound to one endpoint"
        )
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderConfigurationError("DashScope endpoint is not a valid URL")
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    workspace = os.getenv(config.dashscope.workspace_id_env, "").strip() or origin
    return origin, hash_binding(origin), hash_binding(workspace)


def _provider_duration(segment: GenerationSegment) -> int:
    duration = max(
        HappyHorse11R2VModel.MIN_DURATION_SECONDS,
        segment.requested_duration_seconds,
    )
    if duration > HappyHorse11R2VModel.MAX_DURATION_SECONDS:
        raise HappyHorseR2VCanaryError(
            f"segment requests {duration}s but HappyHorse supports at most 15s"
        )
    return duration


def _seed(
    plan: GenerationPlanEpisode,
    segment: GenerationSegment,
    base: int,
) -> int:
    material = (
        f"{plan.content_digest}|{segment.segment_id}|"
        f"{HappyHorse11R2VModel.MODEL_NAME}|{PROTOCOL}"
    ).encode("utf-8")
    offset = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
    return (base + offset) % 2_147_483_648


def _build_approval(
    *,
    plan: GenerationPlanEpisode,
    bundle,
    segment: GenerationSegment,
    selection,
    prompt_bundle,
    config: LiveProviderConfig,
    endpoint_origin_hash: str,
    workspace_id_hash: str,
    resolution: str,
    duration: int,
    seed: int,
    audio_strategy: str,
    price_snapshot_id: str,
    quoted_cost_cny: Decimal,
) -> HappyHorseR2VApprovalManifest:
    return HappyHorseR2VApprovalManifest.build_with_digest(
        segment_id=segment.segment_id,
        generation_plan_digest=plan.content_digest,
        asset_bundle_digest=bundle.content_digest,
        reference_selection_digest=selection.content_digest,
        prompt_bundle_digest=prompt_bundle.content_digest,
        prompt_sha256=prompt_bundle.prompt_sha256,
        ordered_asset_ids=[item.asset_id for item in selection.images],
        ordered_asset_sha256=[item.local_sha256 for item in selection.images],
        deployment_region=config.dashscope.region,
        endpoint_origin_hash=endpoint_origin_hash,
        workspace_id_hash=workspace_id_hash,
        resolution=resolution,
        ratio="9:16",
        duration=duration,
        watermark=config.dashscope.watermark,
        seed=seed,
        audio_strategy=audio_strategy,
        price_snapshot_id=price_snapshot_id,
        quoted_cost_cny=quoted_cost_cny,
        max_api_calls=1,
    )


def _write_plan(path: str | None, plan: GenerationPlanEpisode) -> None:
    if not path:
        return
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    ) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _emit_report(payload: dict, *, path: str | None, print_report: bool) -> None:
    if path:
        _atomic_write_json(Path(path).resolve(), payload)
    if print_report:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _default_ledger(output: Path, segment_id: str) -> Path:
    return output.parent / f".{output.stem}_{segment_id}_happyhorse_r2v_ledger.json"


def _validate_e01_g01_memory_reference(
    segment: GenerationSegment,
    selection,
) -> None:
    if segment.segment_id != "E01-G01":
        return
    subjects = {item.subject_id for item in selection.images}
    if "S05" not in subjects:
        raise HappyHorseR2VCanaryError(
            "E01-G01 source GenerationPlan must explicitly include S05 "
            "for the Yokohama harbor memory"
        )


def _media_facts(path: Path, *, audio_strategy: str, duration: int) -> dict:
    if not path.is_file() or path.stat().st_size == 0:
        raise HappyHorseR2VCanaryError("HappyHorse returned no usable MP4")
    probe = ffprobe_json(path)
    streams = probe.get("streams", [])
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise HappyHorseR2VCanaryError(
            f"expected exactly one video stream, found {len(videos)}"
        )
    video = videos[0]
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise HappyHorseR2VCanaryError("HappyHorse video dimensions are invalid")
    ratio = width / height
    if abs(ratio - 9 / 16) > 0.025:
        raise HappyHorseR2VCanaryError(
            f"HappyHorse output ratio {ratio:.5f} is not vertical 9:16"
        )
    actual_duration = float(probe.get("format", {}).get("duration") or 0.0)
    if actual_duration + 0.08 < duration:
        raise HappyHorseR2VCanaryError(
            f"HappyHorse output {actual_duration:.3f}s is shorter than {duration}s"
        )
    audio_present = bool(audios)
    if audio_strategy == "native_audio" and not audio_present:
        raise HappyHorseR2VCanaryError(
            "happyhorse_native_audio_missing: output has no audio stream"
        )
    if audio_strategy == "silent" and audio_present:
        raise HappyHorseR2VCanaryError(
            "silent audio strategy requested but provider returned an audio stream"
        )
    black = max(0.0, black_duration(path))
    if black > 0.25:
        raise HappyHorseR2VCanaryError(
            f"black-frame duration {black:.3f}s exceeds 0.25s"
        )
    fps_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        fps = float(Fraction(str(fps_text)))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "output": str(path),
        "output_sha256": file_sha256(path),
        "width": width,
        "height": height,
        "fps": fps,
        "duration_seconds": actual_duration,
        "audio_stream_present": audio_present,
        "black_duration_seconds": black,
    }


def _task_expired(record, *, now: datetime | None = None) -> bool:
    if record.status == "succeeded" or record.submitted_at is None:
        return False
    timestamp = now or datetime.now(timezone.utc)
    return timestamp - record.submitted_at >= timedelta(
        hours=TASK_EXPIRY_GUARD_HOURS
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output).resolve()

    try:
        if args.quoted_cost_cny < 0:
            raise HappyHorseR2VCanaryError("quoted-cost-cny must not be negative")
        if args.max_cost_cny < args.quoted_cost_cny:
            raise HappyHorseR2VCanaryError(
                "quoted cost exceeds the immutable maximum cost"
            )
        source_plan = _load_plan(args.source_generation_plan)
        source_bundle = load_bundle(args.source_asset_bundle)
        prepared = _load_prepared(args.prepared_input)
        if prepared_content_digest(prepared) != source_plan.source_prepared_episode_digest:
            raise HappyHorseR2VCanaryError(
                "PreparedEpisode does not match the source GenerationPlan"
            )
        config = LiveProviderConfig.load(args.providers)
        endpoint_origin, endpoint_hash, workspace_hash = _endpoint_binding(config)
        plan, bundle = derive_happyhorse_r2v_plan_and_bundle(
            source_plan,
            source_bundle,
            audio_strategy=args.audio_strategy,
            price_snapshot_id=args.price_snapshot_id,
            quoted_cost_cny_per_segment=args.quoted_cost_cny,
        )
        segment = _find_segment(plan, args.segment_id)
        readiness = assess_reference_input_readiness(
            plan,
            bundle,
            segment_id=segment.segment_id,
            input_mode="reference_images",
            audio_strategy=args.audio_strategy,
        )
        if not readiness.ready:
            raise HappyHorseR2VCanaryError(
                "reference readiness blocked execution: "
                + "; ".join(item.message for item in readiness.errors)
            )
        selection = build_reference_selection_manifest(
            plan,
            bundle,
            segment_id=segment.segment_id,
            audio_strategy=args.audio_strategy,
        )
        _validate_e01_g01_memory_reference(segment, selection)
        override = load_creative_override(args.creative_override)
        prompt_bundle = build_reference_prompt(
            segment,
            selection,
            audio_strategy=args.audio_strategy,
            creative_override=override,
        )
        duration = _provider_duration(segment)
        seed = _seed(plan, segment, config.dashscope.seed_base)
        approval = _build_approval(
            plan=plan,
            bundle=bundle,
            segment=segment,
            selection=selection,
            prompt_bundle=prompt_bundle,
            config=config,
            endpoint_origin_hash=endpoint_hash,
            workspace_id_hash=workspace_hash,
            resolution=args.resolution,
            duration=duration,
            seed=seed,
            audio_strategy=args.audio_strategy,
            price_snapshot_id=args.price_snapshot_id,
            quoted_cost_cny=args.quoted_cost_cny,
        )
        _write_plan(args.derived_generation_plan_output, plan)
        if args.derived_asset_bundle_output:
            write_bundle(args.derived_asset_bundle_output, bundle)
    except (
        OSError,
        ValueError,
        ValidationError,
        ApprovalContractError,
        HappyHorsePlanError,
        HappyHorseR2VCanaryError,
        ProviderConfigurationError,
        ReferencePromptError,
        ReferenceResolutionError,
    ) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    report = {
        "valid": True,
        "stage": args.stage,
        "protocol": PROTOCOL,
        "model": HappyHorse11R2VModel.MODEL_NAME,
        "provider_route_id": HAPPYHORSE_R2V_ROUTE_ID,
        "generation_plan_digest": plan.content_digest,
        "source_generation_plan_digest": source_plan.content_digest,
        "segment_id": segment.segment_id,
        "approved_asset_bundle_digest": bundle.content_digest,
        "input_mode": "reference_images",
        "audio_strategy": args.audio_strategy,
        "asset_readiness": readiness.model_dump(mode="json"),
        "reference_selection": selection.model_dump(mode="json"),
        "prompt_bundle": prompt_bundle.model_dump(mode="json"),
        "endpoint_binding": {
            "region": config.dashscope.region,
            "endpoint_origin": endpoint_origin,
            "endpoint_origin_hash": endpoint_hash,
            "workspace_id_hash": workspace_hash,
        },
        "price": {
            "price_snapshot_id": args.price_snapshot_id,
            "quoted_cost_cny": str(args.quoted_cost_cny),
            "max_cost_cny": str(args.max_cost_cny),
            "billable_task_count": 1,
        },
        "request": {
            "resolution": args.resolution,
            "ratio": "9:16",
            "duration": duration,
            "watermark": config.dashscope.watermark,
            "seed": seed,
            "prompt": prompt_bundle.prompt,
        },
        "approval_digest": approval.content_digest,
    }

    if args.stage == "preflight":
        write_approval_manifest(args.approval_manifest, approval)
        report.update(
            {
                "status": "ready_for_single_paid_submission",
                "provider_submissions_this_run": 0,
                "external_api_calls": 0,
                "next_action": (
                    "review the approval manifest, then rerun with --stage render "
                    "and --approval-digest equal to approval_digest"
                ),
            }
        )
        _emit_report(report, path=args.report, print_report=args.print_report)
        if not args.print_report:
            print(
                "HappyHorse R2V preflight: READY\n"
                f"Segment: {segment.segment_id}\n"
                f"References: {len(selection.images)}\n"
                f"Duration: {duration}s\n"
                f"Resolution: {args.resolution}\n"
                "Ratio: 9:16\n"
                f"Approval digest: {approval.content_digest}\n"
                "External API calls: 0"
            )
        return EXIT_OK

    ledger_path = (
        Path(args.ledger_file).resolve()
        if args.ledger_file
        else _default_ledger(output, segment.segment_id).resolve()
    )
    if args.stage == "resume" and not ledger_path.is_file():
        print("provider error: resume requires an existing ledger", file=sys.stderr)
        return EXIT_PROVIDER

    try:
        approved = load_approval_manifest(args.approval_manifest)
        if approved.content_digest != approval.content_digest:
            raise ApprovalContractError(
                "current plan, assets, prompt, endpoint, or price differs from preflight"
            )
        if not args.approval_digest:
            raise ApprovalContractError(
                "render/resume requires --approval-digest"
            )
        approved.assert_approval_digest(args.approval_digest)
        config.require_environment()

        store = CanaryProviderLedgerStore(ledger_path)
        ledger = store.load_or_create(
            source_digest=approved.content_digest,
            shot_id=segment.segment_id,
            max_api_calls=1,
            max_cost_cny=args.max_cost_cny,
        )
        operation_id = f"{segment.segment_id}:happyhorse-1.1-r2v"
        record, created = store.begin(
            ledger,
            operation_id=operation_id,
            stage="render",
            operation_type="video",
            provider="dashscope",
            model=HappyHorse11R2VModel.MODEL_NAME,
            estimated_cost_cny=args.quoted_cost_cny,
        )
        if args.stage == "resume" and created:
            raise HappyHorseR2VCanaryError(
                "resume cannot create a new provider operation"
            )

        if record.status == "succeeded" and output.is_file():
            facts = _media_facts(
                output,
                audio_strategy=args.audio_strategy,
                duration=duration,
            )
            if record.output_sha256 == facts["output_sha256"]:
                report.update(
                    {
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
            raise HappyHorseR2VCanaryError(
                "a prior submission may exist but no provider task ID was saved; "
                "refusing a duplicate paid request"
            )
        if not created and _task_expired(record):
            raise HappyHorseR2VCanaryError(
                "expired_unrecoverable: provider task exceeded the 23-hour "
                "resume guard; create a new preflight and approval"
            )

        model = HappyHorse11R2VModel(
            {
                "params": {
                    "resolution": args.resolution,
                    "ratio": "9:16",
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
            if created:
                resolved = model.resolve_reference_media_inputs(
                    [item.local_path for item in selection.images]
                )
                published = publish_reference_selection(
                    selection,
                    resolved,
                    provider_region=config.dashscope.region,
                    endpoint_origin_hash=endpoint_hash,
                    workspace_id_hash=workspace_hash,
                    lease_seconds=args.reference_url_lease_seconds,
                )
                if args.published_reference_manifest:
                    _atomic_write_json(
                        Path(args.published_reference_manifest).resolve(),
                        published.model_dump(mode="json"),
                    )
                model.generate_from_resolved(
                    prompt=prompt_bundle.prompt,
                    output_path=str(output),
                    reference_image_urls=[
                        item.provider_url for item in published.images
                    ],
                    resolution=args.resolution,
                    ratio="9:16",
                    duration=duration,
                    watermark=config.dashscope.watermark,
                    seed=seed,
                    extra_headers=published.request_headers,
                )
            else:
                model.generate_from_resolved(
                    prompt=prompt_bundle.prompt,
                    output_path=str(output),
                    reference_image_urls=[],
                    resolution=args.resolution,
                    ratio="9:16",
                    duration=duration,
                    watermark=config.dashscope.watermark,
                    seed=seed,
                )
        finally:
            model.clear_operation()

        facts = _media_facts(
            output,
            audio_strategy=args.audio_strategy,
            duration=duration,
        )
        store.mark_succeeded(
            ledger,
            operation_id,
            output_sha256=facts["output_sha256"],
        )
        report.update(
            {
                "status": "succeeded_awaiting_human_review",
                "provider_submissions_this_run": 1 if created else 0,
                "external_api_calls": 1 if created else 0,
                "ledger_file": str(ledger_path),
                "provider_task_id": ledger.operations[
                    operation_id
                ].provider_task_id,
                "provider_request_id": ledger.operations[
                    operation_id
                ].provider_request_id,
                **facts,
                "next_action": (
                    "human-review identity, reference adherence, shot order, "
                    "Japanese audio, and motion"
                ),
            }
        )
    except (
        OSError,
        ValueError,
        FFmpegError,
        RuntimeError,
        ApprovalContractError,
        HappyHorseR2VCanaryError,
        ProviderConfigurationError,
        ProviderLedgerError,
        ReferenceResolutionError,
    ) as exc:
        try:
            if "ledger" in locals() and "operation_id" in locals():
                if operation_id in ledger.operations:
                    store.mark_unknown(
                        ledger,
                        operation_id,
                        f"{type(exc).__name__}: {exc}",
                    )
        except Exception:
            pass
        status = (
            "expired_unrecoverable"
            if "expired_unrecoverable" in str(exc)
            else "failed"
        )
        report.update(
            {
                "valid": False,
                "status": status,
                "provider_submissions_this_run": (
                    1 if "created" in locals() and created else 0
                ),
                "external_api_calls": (
                    1 if "created" in locals() and created else 0
                ),
                "ledger_file": str(ledger_path),
                "errors": [str(exc)],
                "automatic_retry": False,
            }
        )
        _emit_report(report, path=args.report, print_report=args.print_report)
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    _emit_report(report, path=args.report, print_report=args.print_report)
    if not args.print_report:
        print(
            "HappyHorse R2V Canary: GENERATED\n"
            f"Segment: {segment.segment_id}\n"
            f"Output: {output}\n"
            f"Audio strategy: {args.audio_strategy}\n"
            "Status: awaiting human review"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
