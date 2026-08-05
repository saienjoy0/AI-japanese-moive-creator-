from __future__ import annotations

import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.apps.jp_drama.assets import (
    apply_asset_approvals,
    assess_asset_readiness,
    build_pending_asset_bundle,
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


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _write_png(path: Path, width: int, height: int, value: int = 127) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join(
        b"\x00" + bytes([value, value, value]) * width
        for _ in range(height)
    )
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(payload)


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
    payload = json.loads(PROVIDERS.read_text(encoding="utf-8"))
    payload["dashscope"]["provider_clip_seconds"] = 15
    config = LiveProviderConfig.model_validate(payload)
    plan = compile_generation_plan(
        prepared,
        profile=ProviderSegmentationProfile.load(PROFILE),
        registry=build_default_provider_registry(config),
    )
    return prepared, plan


def _bindings_for_all(bundle, tmp_path: Path, *, duplicate_voice: bool = False):
    assets: dict[str, dict] = {}
    by_segment: dict[str, list[str]] = {}
    for asset in bundle.assets:
        if asset.role != "first_frame":
            for segment_id in asset.required_for_segment_ids:
                by_segment.setdefault(segment_id, []).append(asset.asset_id)

    for index, asset in enumerate(bundle.assets):
        if asset.role == "first_frame":
            path = tmp_path / "frames" / f"{asset.asset_id}.png"
            _write_png(path, 90, 160, 40 + index % 150)
            manifest = tmp_path / "manifests" / f"{asset.asset_id}.json"
            create_approval_manifest(
                shot_id=asset.subject_id,
                asset_path=path,
                generated_by="fixture-image",
                operation_id=f"fixture:{asset.asset_id}",
                output_path=manifest,
            )
            assets[asset.asset_id] = {
                "path": str(path),
                "approval_manifest_path": str(manifest),
                "verified_against_asset_ids": sorted(
                    by_segment.get(asset.subject_id, [])
                ),
            }
        else:
            path = tmp_path / "masters" / f"{asset.asset_id}.png"
            _write_png(path, 64, 64, 40 + index % 150)
            assets[asset.asset_id] = {
                "path": str(path),
                "generated_by": "fixture-master",
                "operation_id": f"fixture:{asset.asset_id}",
            }

    voices = {}
    for index, profile in enumerate(bundle.voice_profiles):
        voices[profile.source_character_id] = {
            "provider": "fixture-tts",
            "voice_id": "shared-voice" if duplicate_voice else f"voice-{index + 1}",
            "language": "ja-JP",
            "speaking_rate": 1.0,
            "pronunciation_dictionary": {"美緒": "みお"},
        }
    return {
        "approved_by": "ci-reviewer",
        "assets": assets,
        "voices": voices,
    }


def test_pending_bundle_reports_stage_specific_blockers() -> None:
    prepared, plan = _compile()
    bundle = build_pending_asset_bundle(prepared, plan)

    assert bundle.assets
    assert len(bundle.voice_profiles) == 2
    assert all(item.approval_status == "pending" for item in bundle.assets)
    assert all(
        item.approval_status == "pending" for item in bundle.voice_profiles
    )

    preflight = assess_asset_readiness(
        bundle,
        prepared,
        plan,
        stage="preflight",
        segment_ids=[plan.segments[0].segment_id],
    )
    assert preflight.ready is True
    assert preflight.warnings

    keyframe = assess_asset_readiness(
        bundle,
        prepared,
        plan,
        stage="keyframe",
        segment_ids=[plan.segments[0].segment_id],
    )
    assert keyframe.ready is False
    assert "required_asset_not_ready" in {item.code for item in keyframe.errors}

    render = assess_asset_readiness(
        bundle,
        prepared,
        plan,
        stage="render",
        segment_ids=[plan.segments[0].segment_id],
    )
    assert render.ready is False
    assert "voice_profile_not_ready" not in {
        item.code for item in render.errors
    }


def test_complete_bundle_passes_full_episode_readiness(tmp_path: Path) -> None:
    prepared, plan = _compile()
    pending = build_pending_asset_bundle(prepared, plan)
    approved = apply_asset_approvals(
        pending,
        _bindings_for_all(pending, tmp_path),
        approved_at=FIXED_TIME,
    )

    report = assess_asset_readiness(
        approved,
        prepared,
        plan,
        stage="full_episode",
    )

    assert report.ready is True
    assert report.errors == []
    assert all(item.approval_status == "approved" for item in approved.assets)
    assert all(
        item.approval_status == "approved" for item in approved.voice_profiles
    )
    assert len(
        {
            (item.provider, item.voice_id)
            for item in approved.voice_profiles
        }
    ) == len(approved.voice_profiles)


def test_first_frame_lineage_and_hash_are_hard_gates(tmp_path: Path) -> None:
    prepared, plan = _compile()
    pending = build_pending_asset_bundle(prepared, plan)
    bindings = _bindings_for_all(pending, tmp_path)
    first = next(item for item in pending.assets if item.role == "first_frame")
    bindings["assets"][first.asset_id]["verified_against_asset_ids"] = []
    incomplete = apply_asset_approvals(
        pending,
        bindings,
        approved_at=FIXED_TIME,
    )

    lineage = assess_asset_readiness(
        incomplete,
        prepared,
        plan,
        stage="render",
        segment_ids=[first.subject_id],
    )
    assert lineage.ready is False
    assert "first_frame_lineage_incomplete" in {
        item.code for item in lineage.errors
    }

    complete = apply_asset_approvals(
        pending,
        _bindings_for_all(pending, tmp_path / "complete"),
        approved_at=FIXED_TIME,
    )
    master = next(
        item for item in complete.assets if item.role == "character_master"
    )
    Path(master.asset_path).write_bytes(Path(master.asset_path).read_bytes() + b"tamper")
    tampered = assess_asset_readiness(
        complete,
        prepared,
        plan,
        stage="full_episode",
    )
    assert tampered.ready is False
    assert any(
        item.asset_id == master.asset_id and "hash" in item.message
        for item in tampered.errors
    )


def test_duplicate_approved_voice_identity_is_rejected(tmp_path: Path) -> None:
    prepared, plan = _compile()
    pending = build_pending_asset_bundle(prepared, plan)

    with pytest.raises(ValidationError, match="is shared"):
        apply_asset_approvals(
            pending,
            _bindings_for_all(pending, tmp_path, duplicate_voice=True),
            approved_at=FIXED_TIME,
        )


def test_bundle_is_bound_to_exact_plan_and_prepared_episode(tmp_path: Path) -> None:
    prepared, plan = _compile()
    pending = build_pending_asset_bundle(prepared, plan)
    approved = apply_asset_approvals(
        pending,
        _bindings_for_all(pending, tmp_path),
        approved_at=FIXED_TIME,
    )
    changed = prepared.model_copy(
        update={
            "project_draft": prepared.project_draft.model_copy(
                update={"title": prepared.project_draft.title + " changed"}
            )
        }
    )

    report = assess_asset_readiness(
        approved,
        changed,
        plan,
        stage="full_episode",
    )
    assert report.ready is False
    assert "asset_bundle_prepared_mismatch" in {
        item.code for item in report.errors
    }
