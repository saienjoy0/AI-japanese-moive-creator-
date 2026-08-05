from __future__ import annotations

import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.apps.jp_drama.assets import (
    WanFirstFrameError,
    WanMasterReferenceError,
    apply_asset_approvals,
    build_wan_master_reference_manifest,
    load_wan_master_reference_manifest,
    register_wan_first_frame,
    verify_wan_first_frame_ready,
    verify_wan_master_reference_manifest,
    write_bundle,
)
from src.apps.jp_drama.rendering.approval import create_approval_manifest
from src.apps.jp_drama.rendering.provider_config import (
    LiveProviderConfig,
    ProviderConfigurationError,
)
from src.apps.jp_drama.rendering.wan_master_tasks import (
    WanMasterReferenceLiveTaskExecutor,
)
from src.apps.jp_drama.seedance_storyboard import (
    SUPPORTED_ROUTES,
    build_storyboard_asset_bundle,
    build_storyboard_prepared_episode,
    compile_storyboard_generation_plan,
    load_project_directory,
    parse_project,
)
from src.apps.jp_drama.rendering.minimax_h3_adapter import build_h3_first_provider_registry
from src.apps.jp_drama.rendering.minimax_h3_config import MiniMaxH3ProviderConfig
from src.apps.jp_drama.rendering.segment_canary import materialize_generation_segment_canary
from src.apps.jp_drama.workflows.prepare_wan_master_keyframe import main as prepare_main
from src.apps.jp_drama.workflows.register_wan_master_keyframe import main as register_main


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "jp_drama" / "seedance_storyboard" / "upstream_fixture"
LIVE_CONFIG = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"
H3_CONFIG = ROOT / "examples" / "jp_drama" / "minimax_h3_live_provider.json"
FIXED_TIME = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)


def _package():
    return parse_project(load_project_directory(FIXTURE))


def _registry():
    return build_h3_first_provider_registry(
        LiveProviderConfig.load(LIVE_CONFIG),
        MiniMaxH3ProviderConfig.load(H3_CONFIG),
    )


def _route():
    package = _package()
    prepared = build_storyboard_prepared_episode(package, "E01")
    plan = compile_storyboard_generation_plan(
        package,
        prepared,
        "E01",
        route_id=SUPPORTED_ROUTES["wan"],
        registry=_registry(),
    )
    return prepared, plan


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _write_png(path: Path, *, width: int, height: int, value: int) -> None:
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


def _approved_masters(prepared, plan, tmp_path: Path):
    pending = build_storyboard_asset_bundle(prepared, plan)
    bindings: dict[str, dict[str, str]] = {}
    index = 0
    for asset in pending.assets:
        if asset.role not in {"character_master", "location_master", "prop_master"}:
            continue
        path = tmp_path / "masters" / f"{asset.asset_id}.png"
        _write_png(path, width=72, height=72, value=40 + index * 20)
        bindings[asset.asset_id] = {
            "path": str(path),
            "generated_by": "fixture-master",
            "operation_id": f"fixture:{asset.asset_id}",
        }
        index += 1
    return apply_asset_approvals(
        pending,
        {
            "approved_by": "ci-reviewer",
            "assets": bindings,
            "voices": {},
        },
        approved_at=FIXED_TIME,
    )


def _write_inputs(tmp_path: Path, prepared, plan, bundle):
    prepared_path = tmp_path / "prepared.json"
    plan_path = tmp_path / "plan.json"
    bundle_path = tmp_path / "bundle.json"
    prepared_path.write_text(prepared.to_canonical_json() + "\n", encoding="utf-8")
    plan_path.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")
    write_bundle(bundle_path, bundle)
    return prepared_path, plan_path, bundle_path


class FakeImageModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(self, prompt: str, output: str, **kwargs):
        self.calls.append({"prompt": prompt, "output": output, **kwargs})
        _write_png(Path(output), width=90, height=160, value=128)
        return output


class FakeUnusedProvider:
    pass


def test_manifest_preserves_planned_reference_order_and_roundtrips(tmp_path: Path) -> None:
    prepared, plan = _route()
    bundle = _approved_masters(prepared, plan, tmp_path)
    segment = plan.segments[0]

    manifest = build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment.segment_id,
    )

    expected = [
        item
        for item in segment.reference_asset_ids
        if item.startswith(("ref_char_", "ref_loc_", "ref_prop_"))
    ]
    assert manifest.asset_ids == expected
    assert [item.order for item in manifest.references] == list(range(len(expected)))
    assert manifest.master_asset_set_digest.startswith("sha256:")
    assert len(manifest.references) <= 9

    path = tmp_path / "wan-master-references.json"
    path.write_text(manifest.to_canonical_json(), encoding="utf-8")
    loaded = load_wan_master_reference_manifest(path)
    assert loaded == manifest


def test_executor_sends_exact_approved_master_paths_to_wan_image_model(
    tmp_path: Path,
) -> None:
    prepared, plan = _route()
    bundle = _approved_masters(prepared, plan, tmp_path)
    segment = plan.segments[0]
    manifest = build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment.segment_id,
    )
    config = LiveProviderConfig.load(LIVE_CONFIG)
    materialized = materialize_generation_segment_canary(
        prepared,
        plan,
        segment.segment_id,
        provider_clip_seconds=config.dashscope.provider_clip_seconds,
    )
    image = FakeImageModel()
    executor = WanMasterReferenceLiveTaskExecutor(
        config,
        master_references={segment.segment_id: manifest},
        image_model=image,
        video_model=FakeUnusedProvider(),
        tts_processor=FakeUnusedProvider(),
        require_credentials=False,
        api_call_limit=1,
    )

    output = executor.generate_canary_keyframe(
        materialized,
        shot_id=segment.segment_id,
        output=tmp_path / "keyframe.png",
    )

    assert output.is_file()
    assert len(image.calls) == 1
    assert image.calls[0]["ref_image_paths"] == manifest.asset_paths
    assert executor.external_api_calls == 1


def test_missing_manifest_blocks_before_fake_provider_call(tmp_path: Path) -> None:
    prepared, plan = _route()
    config = LiveProviderConfig.load(LIVE_CONFIG)
    segment = plan.segments[0]
    materialized = materialize_generation_segment_canary(
        prepared,
        plan,
        segment.segment_id,
        provider_clip_seconds=config.dashscope.provider_clip_seconds,
    )
    image = FakeImageModel()
    executor = WanMasterReferenceLiveTaskExecutor(
        config,
        master_references={},
        image_model=image,
        video_model=FakeUnusedProvider(),
        tts_processor=FakeUnusedProvider(),
        require_credentials=False,
        api_call_limit=1,
    )

    with pytest.raises(ProviderConfigurationError, match="requires an approved"):
        executor.generate_canary_keyframe(
            materialized,
            shot_id=segment.segment_id,
            output=tmp_path / "blocked.png",
        )
    assert image.calls == []
    assert executor.external_api_calls == 0


def test_approval_registration_survives_bundle_digest_change_and_revalidates(
    tmp_path: Path,
) -> None:
    prepared, plan = _route()
    bundle = _approved_masters(prepared, plan, tmp_path)
    segment = plan.segments[0]
    manifest = build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment.segment_id,
    )
    keyframe = tmp_path / "approved-keyframe.png"
    _write_png(keyframe, width=90, height=160, value=150)
    approval_path = tmp_path / "approved-keyframe.approval.json"
    create_approval_manifest(
        shot_id=segment.segment_id,
        asset_path=keyframe,
        generated_by="dashscope/wan2.7-image",
        operation_id="task:keyframe:masters",
        output_path=approval_path,
        master_reference_manifest_digest=manifest.content_digest,
        master_reference_asset_ids=manifest.asset_ids,
        master_reference_asset_hashes=manifest.asset_hashes,
    )

    updated = register_wan_first_frame(
        bundle,
        prepared,
        plan,
        manifest,
        segment_id=segment.segment_id,
        approval_manifest_path=approval_path,
        approved_by="human-reviewer",
        approved_at=FIXED_TIME,
    )

    assert updated.content_digest != bundle.content_digest
    verify_wan_master_reference_manifest(
        manifest,
        prepared,
        plan,
        updated,
        segment_id=segment.segment_id,
    )
    asset, verified_path = verify_wan_first_frame_ready(
        updated,
        prepared,
        plan,
        manifest,
        segment_id=segment.segment_id,
    )
    assert verified_path == keyframe.resolve()
    assert asset.verified_against_asset_ids == manifest.asset_ids
    assert asset.approved_by == "human-reviewer"


def test_master_image_change_invalidates_manifest_and_first_frame(tmp_path: Path) -> None:
    prepared, plan = _route()
    bundle = _approved_masters(prepared, plan, tmp_path)
    segment = plan.segments[0]
    manifest = build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment.segment_id,
    )
    changed = Path(manifest.references[0].asset_path)
    _write_png(changed, width=72, height=72, value=250)

    with pytest.raises(WanMasterReferenceError, match="hash changed"):
        verify_wan_master_reference_manifest(
            manifest,
            prepared,
            plan,
            bundle,
            segment_id=segment.segment_id,
        )


def test_zero_call_prepare_and_register_cli(tmp_path: Path) -> None:
    prepared, plan = _route()
    bundle = _approved_masters(prepared, plan, tmp_path)
    segment = plan.segments[0]
    prepared_path, plan_path, bundle_path = _write_inputs(
        tmp_path,
        prepared,
        plan,
        bundle,
    )
    manifest_path = tmp_path / "manifest.json"
    prepare_report = tmp_path / "prepare-report.json"

    assert prepare_main(
        [
            "--prepared-input",
            str(prepared_path),
            "--generation-plan",
            str(plan_path),
            "--asset-bundle",
            str(bundle_path),
            "--segment-id",
            segment.segment_id,
            "--manifest-output",
            str(manifest_path),
            "--report",
            str(prepare_report),
        ]
    ) == 0
    prepare_payload = json.loads(prepare_report.read_text(encoding="utf-8"))
    assert prepare_payload["valid"] is True
    assert prepare_payload["external_api_calls"] == 0

    manifest = load_wan_master_reference_manifest(manifest_path)
    keyframe = tmp_path / "keyframe.png"
    _write_png(keyframe, width=90, height=160, value=170)
    approval_path = tmp_path / "keyframe.approval.json"
    create_approval_manifest(
        shot_id=segment.segment_id,
        asset_path=keyframe,
        generated_by="dashscope/wan2.7-image",
        operation_id="task:keyframe:masters",
        output_path=approval_path,
        master_reference_manifest_digest=manifest.content_digest,
        master_reference_asset_ids=manifest.asset_ids,
        master_reference_asset_hashes=manifest.asset_hashes,
    )
    updated_path = tmp_path / "bundle-with-first-frame.json"
    register_report = tmp_path / "register-report.json"
    assert register_main(
        [
            "--prepared-input",
            str(prepared_path),
            "--generation-plan",
            str(plan_path),
            "--asset-bundle",
            str(bundle_path),
            "--master-reference-manifest",
            str(manifest_path),
            "--approval-manifest",
            str(approval_path),
            "--segment-id",
            segment.segment_id,
            "--approved-by",
            "human-reviewer",
            "--output-bundle",
            str(updated_path),
            "--report",
            str(register_report),
        ]
    ) == 0
    register_payload = json.loads(register_report.read_text(encoding="utf-8"))
    assert register_payload["valid"] is True
    assert register_payload["external_api_calls"] == 0
    assert updated_path.is_file()


def test_registration_rejects_approval_from_different_master_manifest(
    tmp_path: Path,
) -> None:
    prepared, plan = _route()
    bundle = _approved_masters(prepared, plan, tmp_path)
    segment = plan.segments[0]
    manifest = build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment.segment_id,
    )
    keyframe = tmp_path / "wrong-lineage.png"
    _write_png(keyframe, width=90, height=160, value=180)
    approval_path = tmp_path / "wrong-lineage.approval.json"
    create_approval_manifest(
        shot_id=segment.segment_id,
        asset_path=keyframe,
        generated_by="dashscope/wan2.7-image",
        operation_id="task:keyframe:other-masters",
        output_path=approval_path,
        master_reference_manifest_digest="sha256:" + "0" * 64,
        master_reference_asset_ids=manifest.asset_ids,
        master_reference_asset_hashes=manifest.asset_hashes,
    )

    with pytest.raises(WanFirstFrameError, match="digest does not match"):
        register_wan_first_frame(
            bundle,
            prepared,
            plan,
            manifest,
            segment_id=segment.segment_id,
            approval_manifest_path=approval_path,
            approved_by="human-reviewer",
        )
