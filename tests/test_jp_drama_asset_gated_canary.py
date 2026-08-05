from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

from src.apps.jp_drama.assets import (
    apply_asset_approvals,
    build_pending_asset_bundle,
    write_bundle,
)
from src.apps.jp_drama.generation import (
    ProviderSegmentationProfile,
    compile_generation_plan,
)
from src.apps.jp_drama.ingestion import FixtureStructuredScriptLLM, ingest_script
from src.apps.jp_drama.preparation import compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering.approval import create_approval_manifest
from src.apps.jp_drama.rendering.provider_config import LiveProviderConfig
from src.apps.jp_drama.rendering.provider_registry import build_default_provider_registry


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples" / "jp_drama" / "script_ingestion" / "sample_script.md"
FIXTURE = (
    ROOT
    / "examples"
    / "jp_drama"
    / "script_ingestion"
    / "structured_script_fixture.json"
)
CATALOG = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
PROFILE = ROOT / "examples" / "jp_drama" / "generation" / "wan27_profile.json"
PROVIDERS = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"
FIXED_TIME = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _png(path: Path, width: int, height: int, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join(
        b"\x00" + bytes([value, value, value]) * width
        for _ in range(height)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _compile():
    ingestion = ingest_script(
        SCRIPT.read_text(encoding="utf-8"),
        llm=FixtureStructuredScriptLLM(FIXTURE),
        created_at=FIXED_TIME,
    )
    prepared = compile_episode(
        ingestion.episode_package,
        catalog=load_model_catalog(CATALOG),
        strict=True,
    )
    provider_payload = json.loads(PROVIDERS.read_text(encoding="utf-8"))
    provider_payload["dashscope"]["provider_clip_seconds"] = 15
    config = LiveProviderConfig.model_validate(provider_payload)
    plan = compile_generation_plan(
        prepared,
        profile=ProviderSegmentationProfile.load(PROFILE),
        registry=build_default_provider_registry(config),
    )
    return prepared, plan, provider_payload


def _approve_everything(prepared, plan, tmp_path: Path):
    pending = build_pending_asset_bundle(prepared, plan)
    master_by_segment: dict[str, list[str]] = {}
    for asset in pending.assets:
        if asset.role != "first_frame":
            for segment_id in asset.required_for_segment_ids:
                master_by_segment.setdefault(segment_id, []).append(asset.asset_id)

    bindings = {"approved_by": "ci-reviewer", "assets": {}, "voices": {}}
    for index, asset in enumerate(pending.assets):
        if asset.role == "first_frame":
            path = tmp_path / "frames" / f"{asset.asset_id}.png"
            _png(path, 90, 160, 30 + index)
            manifest = tmp_path / "manifests" / f"{asset.asset_id}.json"
            create_approval_manifest(
                shot_id=asset.subject_id,
                asset_path=path,
                generated_by="test-frame",
                operation_id=f"test:{asset.asset_id}",
                output_path=manifest,
            )
            bindings["assets"][asset.asset_id] = {
                "path": str(path),
                "approval_manifest_path": str(manifest),
                "verified_against_asset_ids": sorted(
                    master_by_segment.get(asset.subject_id, [])
                ),
            }
        else:
            path = tmp_path / "masters" / f"{asset.asset_id}.png"
            _png(path, 64, 64, 30 + index)
            bindings["assets"][asset.asset_id] = {
                "path": str(path),
                "generated_by": "test-master",
                "operation_id": f"test:{asset.asset_id}",
            }

    for index, profile in enumerate(pending.voice_profiles):
        bindings["voices"][profile.source_character_id] = {
            "provider": "qwen3-test",
            "voice_id": f"approved-voice-{index + 1}",
            "language": "ja-JP",
            "speaking_rate": 1.0,
        }
    return apply_asset_approvals(
        pending,
        bindings,
        approved_at=FIXED_TIME,
    )


def _run(
    *,
    prepared_path: Path,
    plan_path: Path,
    providers_path: Path,
    bundle_path: Path,
    segment_id: str,
    output_path: Path,
    report_path: Path,
    stage: str,
):
    environment = os.environ.copy()
    for key in (
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_BASE_URL",
        "DASHSCOPE_UPLOAD_BASE_URL",
        "DASHSCOPE_WORKSPACE_ID",
    ):
        environment.pop(key, None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "src.apps.jp_drama.workflows.render_generation_segment_canary",
            "--prepared-input",
            str(prepared_path),
            "--generation-plan",
            str(plan_path),
            "--segment-id",
            segment_id,
            "--output",
            str(output_path),
            "--providers",
            str(providers_path),
            "--asset-bundle",
            str(bundle_path),
            "--stage",
            stage,
            "--max-cost-cny",
            "20.0",
            "--report",
            str(report_path),
            "--print-report",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_approved_bundle_reaches_provider_boundary_and_injects_voices(
    tmp_path: Path,
) -> None:
    prepared, plan, provider_payload = _compile()
    dialogue_segment = next(item for item in plan.segments if item.dialogue_slices)
    approved = _approve_everything(prepared, plan, tmp_path / "assets")

    prepared_path = tmp_path / "prepared.json"
    plan_path = tmp_path / "plan.json"
    providers_path = tmp_path / "providers.json"
    bundle_path = tmp_path / "bundle.json"
    output_path = tmp_path / "segment.mp4"
    report_path = tmp_path / "report.json"
    prepared_path.write_text(prepared.to_canonical_json() + "\n", encoding="utf-8")
    plan_path.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")
    providers_path.write_text(
        json.dumps(provider_payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    write_bundle(bundle_path, approved)

    result = _run(
        prepared_path=prepared_path,
        plan_path=plan_path,
        providers_path=providers_path,
        bundle_path=bundle_path,
        segment_id=dialogue_segment.segment_id,
        output_path=output_path,
        report_path=report_path,
        stage="render",
    )

    assert result.returncode == 6
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["asset_bundle_present"] is True
    assert report["asset_readiness"]["ready"] is True
    assert report["status"] == "failed"
    assert report["delegate_exit_code"] == 6
    assert report["external_api_calls"] == 0
    assert not output_path.exists()

    derived = tmp_path / f".segment_{dialogue_segment.segment_id}_providers.json"
    payload = json.loads(derived.read_text(encoding="utf-8"))
    voices = payload["dashscope"]["voice_by_character"]
    assert len(voices) == 2
    assert len(set(voices.values())) == 2


def test_pending_bundle_blocks_keyframe_before_provider_submission(
    tmp_path: Path,
) -> None:
    prepared, plan, provider_payload = _compile()
    segment = plan.segments[0]
    pending = build_pending_asset_bundle(prepared, plan)

    prepared_path = tmp_path / "prepared.json"
    plan_path = tmp_path / "plan.json"
    providers_path = tmp_path / "providers.json"
    bundle_path = tmp_path / "pending.json"
    output_path = tmp_path / "segment.mp4"
    report_path = tmp_path / "report.json"
    prepared_path.write_text(prepared.to_canonical_json() + "\n", encoding="utf-8")
    plan_path.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")
    providers_path.write_text(
        json.dumps(provider_payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    write_bundle(bundle_path, pending)

    result = _run(
        prepared_path=prepared_path,
        plan_path=plan_path,
        providers_path=providers_path,
        bundle_path=bundle_path,
        segment_id=segment.segment_id,
        output_path=output_path,
        report_path=report_path,
        stage="keyframe",
    )

    assert result.returncode == 6
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert report["asset_gate"] == "blocked_before_provider_submission"
    assert report["asset_readiness"]["ready"] is False
    assert report["external_api_calls"] == 0
    assert not output_path.exists()
