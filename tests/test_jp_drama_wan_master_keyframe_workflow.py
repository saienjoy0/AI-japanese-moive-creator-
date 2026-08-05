from __future__ import annotations

import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

from src.apps.jp_drama.assets import (
    apply_asset_approvals,
    build_wan_master_reference_manifest,
    write_bundle,
    write_wan_master_reference_manifest,
)
from src.apps.jp_drama.rendering.minimax_h3_adapter import build_h3_first_provider_registry
from src.apps.jp_drama.rendering.minimax_h3_config import MiniMaxH3ProviderConfig
from src.apps.jp_drama.rendering.provider_config import LiveProviderConfig
from src.apps.jp_drama.seedance_storyboard import (
    SUPPORTED_ROUTES,
    build_storyboard_asset_bundle,
    build_storyboard_prepared_episode,
    compile_storyboard_generation_plan,
    load_project_directory,
    parse_project,
)
from src.apps.jp_drama.workflows.render_wan_master_keyframe import main as keyframe_main


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "jp_drama" / "seedance_storyboard" / "upstream_fixture"
LIVE_CONFIG = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"
H3_CONFIG = ROOT / "examples" / "jp_drama" / "minimax_h3_live_provider.json"
FIXED_TIME = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _write_png(path: Path, value: int) -> None:
    width = height = 64
    raw = b"".join(
        b"\x00" + bytes([value, value, value]) * width for _ in range(height)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def _inputs(tmp_path: Path):
    package = parse_project(load_project_directory(FIXTURE))
    prepared = build_storyboard_prepared_episode(package, "E01")
    registry = build_h3_first_provider_registry(
        LiveProviderConfig.load(LIVE_CONFIG),
        MiniMaxH3ProviderConfig.load(H3_CONFIG),
    )
    plan = compile_storyboard_generation_plan(
        package,
        prepared,
        "E01",
        route_id=SUPPORTED_ROUTES["wan"],
        registry=registry,
    )
    pending = build_storyboard_asset_bundle(prepared, plan)
    bindings = {}
    for index, asset in enumerate(pending.assets):
        if asset.role not in {"character_master", "location_master", "prop_master"}:
            continue
        path = tmp_path / "masters" / f"{asset.asset_id}.png"
        _write_png(path, 40 + index * 10)
        bindings[asset.asset_id] = {
            "path": str(path),
            "generated_by": "fixture-master",
            "operation_id": f"fixture:{asset.asset_id}",
        }
    bundle = apply_asset_approvals(
        pending,
        {
            "approved_by": "ci-reviewer",
            "assets": bindings,
            "voices": {},
        },
        approved_at=FIXED_TIME,
    )
    segment = plan.segments[0]
    manifest = build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment.segment_id,
    )
    prepared_path = tmp_path / "prepared.json"
    plan_path = tmp_path / "plan.json"
    bundle_path = tmp_path / "bundle.json"
    manifest_path = tmp_path / "master-references.json"
    prepared_path.write_text(prepared.to_canonical_json() + "\n", encoding="utf-8")
    plan_path.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")
    write_bundle(bundle_path, bundle)
    write_wan_master_reference_manifest(manifest, manifest_path)
    return segment.segment_id, prepared_path, plan_path, bundle_path, manifest_path


def _base_args(tmp_path: Path) -> list[str]:
    segment_id, prepared, plan, bundle, manifest = _inputs(tmp_path)
    return [
        "--prepared-input", str(prepared),
        "--generation-plan", str(plan),
        "--asset-bundle", str(bundle),
        "--master-reference-manifest", str(manifest),
        "--providers", str(LIVE_CONFIG),
        "--segment-id", segment_id,
        "--keyframe-output", str(tmp_path / "keyframe.png"),
        "--report", str(tmp_path / "report.json"),
        "--max-api-calls", "1",
        "--max-cost-cny", "5.0",
    ]


def _replace_provider_path(args: list[str], path: Path) -> list[str]:
    updated = list(args)
    updated[updated.index("--providers") + 1] = str(path)
    return updated


def test_preflight_is_zero_call_and_approval_bound(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    args = _base_args(tmp_path)

    assert keyframe_main([*args, "--stage", "preflight"]) == 0

    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["valid"] is True
    assert payload["stage"] == "preflight"
    assert payload["external_api_calls"] == 0
    assert payload["credentials_present"] is False
    assert payload["approval_digest"].startswith("sha256:")
    assert payload["master_reference_manifest_digest"].startswith("sha256:")
    assert payload["reference_asset_ids"]
    assert not (tmp_path / "keyframe.png").exists()
    assert not list(tmp_path.glob("*_provider_ledger.json"))
    assert not list(tmp_path.glob(".*_provider_ledger.json"))


def test_first_frame_preflight_ignores_video_clip_duration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    args = _base_args(tmp_path)
    provider_payload = json.loads(LIVE_CONFIG.read_text(encoding="utf-8"))
    provider_payload["dashscope"]["provider_clip_seconds"] = 1
    tiny_video_limit = tmp_path / "tiny-video-limit.json"
    tiny_video_limit.write_text(
        json.dumps(provider_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    assert keyframe_main(
        [*_replace_provider_path(args, tiny_video_limit), "--stage", "preflight"]
    ) == 0

    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["valid"] is True
    assert payload["external_api_calls"] == 0
    assert payload["provider"] == "dashscope"
    assert not (tmp_path / "keyframe.png").exists()
    assert not list(tmp_path.glob(".*_provider_ledger.json"))


def test_paid_stage_requires_explicit_flag_and_exact_digest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    args = _base_args(tmp_path)
    assert keyframe_main([*args, "--stage", "preflight"]) == 0
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    assert keyframe_main([*args, "--stage", "keyframe"]) == 6
    assert keyframe_main(
        [
            *args,
            "--stage", "keyframe",
            "--execute-paid",
            "--approval-digest", "sha256:" + "0" * 64,
        ]
    ) == 6
    assert not (tmp_path / "keyframe.png").exists()
    assert payload["external_api_calls"] == 0
