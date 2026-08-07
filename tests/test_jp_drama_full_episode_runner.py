from __future__ import annotations

import json
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.apps.jp_drama.assets import apply_asset_approvals, build_pending_asset_bundle
from src.apps.jp_drama.full_episode import (
    FullEpisodeComposer,
    FullEpisodeSegmentError,
    FullEpisodeStateConflictError,
)
from src.apps.jp_drama.generation import (
    ProviderSegmentationProfile,
    compile_generation_plan,
)
from src.apps.jp_drama.ingestion import FixtureStructuredScriptLLM, ingest_script
from src.apps.jp_drama.preparation import compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering.approval import create_approval_manifest
from src.apps.jp_drama.rendering.ffmpeg import ffmpeg, require_ffmpeg
from src.apps.jp_drama.rendering.provider_config import LiveProviderConfig
from src.apps.jp_drama.rendering.provider_registry import build_default_provider_registry
from src.apps.jp_drama.workflows.run_full_episode import EXIT_OK, EXIT_PROVIDER, main


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
FIXED_TIME = datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc)


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
    config = LiveProviderConfig.load(PROVIDERS)
    plan = compile_generation_plan(
        prepared,
        profile=ProviderSegmentationProfile.load(PROFILE),
        registry=build_default_provider_registry(config),
    )
    assert len(plan.segments) == 15
    assert plan.readiness_report.execution_route_ready is True
    return prepared, plan, config


def _provider_clip(path: Path, *, seconds: int, fps: int, index: int, audio: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    video = f"testsrc2=size=90x160:rate={fps}:duration={seconds}"
    if audio:
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            video,
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={330 + index * 11}:sample_rate=48000:duration={seconds}",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-frames:v",
            str(seconds * fps),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            str(path),
        )
    else:
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            video,
            "-frames:v",
            str(seconds * fps),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            str(20 + index % 6),
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        )


def _segment_outputs(plan, root: Path) -> dict[str, Path]:
    values = {}
    for index, segment in enumerate(plan.segments, start=1):
        path = root / f"{segment.order:03d}_{segment.segment_id}.mp4"
        _provider_clip(
            path,
            seconds=segment.requested_duration_seconds,
            fps=segment.timeline_fps,
            index=index,
            audio=index % 2 == 0,
        )
        values[segment.segment_id] = path
    return values


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _write_png(path: Path, width: int, height: int, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join(
        b"\x00" + bytes([value, value, value]) * width for _ in range(height)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def _approved_bundle(prepared, plan, root: Path):
    pending = build_pending_asset_bundle(prepared, plan)
    masters_by_segment: dict[str, list[str]] = {}
    for asset in pending.assets:
        if asset.role != "first_frame":
            for segment_id in asset.required_for_segment_ids:
                masters_by_segment.setdefault(segment_id, []).append(asset.asset_id)

    bindings: dict = {"approved_by": "ci-reviewer", "assets": {}, "voices": {}}
    for index, asset in enumerate(pending.assets):
        if asset.role == "first_frame":
            path = root / "frames" / f"{asset.asset_id}.png"
            _write_png(path, 90, 160, 50 + index % 150)
            manifest = root / "manifests" / f"{asset.asset_id}.json"
            create_approval_manifest(
                shot_id=asset.subject_id,
                asset_path=path,
                generated_by="fixture-image",
                operation_id=f"fixture:{asset.asset_id}",
                output_path=manifest,
            )
            bindings["assets"][asset.asset_id] = {
                "path": str(path),
                "approval_manifest_path": str(manifest),
                "verified_against_asset_ids": sorted(
                    masters_by_segment.get(asset.subject_id, [])
                ),
            }
        else:
            path = root / "masters" / f"{asset.asset_id}.png"
            _write_png(path, 64, 64, 50 + index % 150)
            bindings["assets"][asset.asset_id] = {
                "path": str(path),
                "generated_by": "fixture-master",
                "operation_id": f"fixture:{asset.asset_id}",
            }
    for index, profile in enumerate(pending.voice_profiles, start=1):
        bindings["voices"][profile.source_character_id] = {
            "provider": "fixture-tts",
            "voice_id": f"distinct-voice-{index}",
            "language": "ja-JP",
            "speaking_rate": 1.0,
            "pronunciation_dictionary": {},
        }
    return apply_asset_approvals(pending, bindings, approved_at=FIXED_TIME)


@pytest.fixture(scope="module", autouse=True)
def _ffmpeg_available():
    require_ffmpeg()


def test_compose_exact_episode_and_resume_only_changed_segment(tmp_path: Path) -> None:
    _, plan, _ = _compile()
    sources = _segment_outputs(plan, tmp_path / "provider")
    output = tmp_path / "episode.mp4"
    work = tmp_path / "work"
    composer = FullEpisodeComposer(
        plan,
        output_file=output,
        work_dir=work,
        target_width=90,
        target_height=160,
    )

    first = composer.compose(sources)
    assert first.valid is True
    assert len(first.segment_validations) == 15
    assert first.actual_frame_count == plan.target_frame_count
    assert first.segment_order == [item.segment_id for item in plan.segments]
    assert first.video_streams == 1
    assert first.audio_streams == 1
    assert abs(first.duration_seconds - float(plan.target_duration_seconds)) < 0.1

    state_before = json.loads(composer.state_file.read_text(encoding="utf-8"))
    attempts_before = {
        key: value["attempts"] for key, value in state_before["segments"].items()
    }
    second = composer.compose(sources)
    assert second.valid is True
    state_same = json.loads(composer.state_file.read_text(encoding="utf-8"))
    assert {
        key: value["attempts"] for key, value in state_same["segments"].items()
    } == attempts_before

    changed = plan.segments[4]
    _provider_clip(
        sources[changed.segment_id],
        seconds=changed.requested_duration_seconds,
        fps=changed.timeline_fps,
        index=99,
        audio=True,
    )
    third = composer.compose(sources)
    assert third.valid is True
    state_changed = json.loads(composer.state_file.read_text(encoding="utf-8"))
    attempts_after = {
        key: value["attempts"] for key, value in state_changed["segments"].items()
    }
    assert attempts_after[changed.segment_id] == attempts_before[changed.segment_id] + 1
    assert all(
        attempts_after[item.segment_id] == attempts_before[item.segment_id]
        for item in plan.segments
        if item.segment_id != changed.segment_id
    )


def test_compose_rejects_missing_mapping_and_conflicting_state(tmp_path: Path) -> None:
    _, plan, _ = _compile()
    sources = _segment_outputs(plan, tmp_path / "provider")
    missing = dict(sources)
    missing.pop(plan.segments[-1].segment_id)
    composer = FullEpisodeComposer(
        plan,
        output_file=tmp_path / "episode.mp4",
        work_dir=tmp_path / "work",
        target_width=90,
        target_height=160,
    )
    with pytest.raises(FullEpisodeSegmentError, match="missing="):
        composer.compose(missing)

    composer.compose(sources)
    conflict = FullEpisodeComposer(
        plan,
        output_file=tmp_path / "episode.mp4",
        work_dir=tmp_path / "work",
        target_width=180,
        target_height=320,
    )
    with pytest.raises(FullEpisodeStateConflictError, match="target dimensions"):
        conflict.compose(sources)


def test_compose_cli_is_zero_cost_and_paid_render_requires_exact_digest(
    tmp_path: Path,
) -> None:
    prepared, plan, _ = _compile()
    sources = _segment_outputs(plan, tmp_path / "provider")
    prepared_path = tmp_path / "prepared.json"
    plan_path = tmp_path / "plan.json"
    outputs_path = tmp_path / "outputs.json"
    output = tmp_path / "cli-episode.mp4"
    work = tmp_path / "cli-work"
    report_path = tmp_path / "compose-report.json"
    prepared_path.write_text(prepared.to_canonical_json() + "\n", encoding="utf-8")
    plan_path.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")
    outputs_path.write_text(
        json.dumps(
            {"segments": {key: str(value) for key, value in sources.items()}},
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    code = main(
        [
            "--prepared-input",
            str(prepared_path),
            "--generation-plan",
            str(plan_path),
            "--stage",
            "compose",
            "--segment-outputs",
            str(outputs_path),
            "--output",
            str(output),
            "--work-dir",
            str(work),
            "--target-width",
            "90",
            "--target-height",
            "160",
            "--report",
            str(report_path),
        ]
    )
    assert code == EXIT_OK
    compose_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert compose_report["valid"] is True
    assert compose_report["external_api_calls"] == 0
    assert compose_report["validation_report"]["actual_frame_count"] == plan.target_frame_count

    bundle = _approved_bundle(prepared, plan, tmp_path / "assets")
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(bundle.to_canonical_json() + "\n", encoding="utf-8")
    preflight_path = tmp_path / "preflight.json"
    code = main(
        [
            "--prepared-input",
            str(prepared_path),
            "--generation-plan",
            str(plan_path),
            "--stage",
            "preflight",
            "--providers",
            str(PROVIDERS),
            "--asset-bundle",
            str(bundle_path),
            "--output",
            str(tmp_path / "paid-episode.mp4"),
            "--work-dir",
            str(tmp_path / "paid-work"),
            "--report",
            str(preflight_path),
        ]
    )
    assert code == EXIT_OK
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    assert preflight["valid"] is True
    assert preflight["external_api_calls"] == 0
    assert preflight["execution_budget"]["remaining_api_calls"] == 18

    blocked_path = tmp_path / "blocked.json"
    code = main(
        [
            "--prepared-input",
            str(prepared_path),
            "--generation-plan",
            str(plan_path),
            "--stage",
            "render",
            "--providers",
            str(PROVIDERS),
            "--asset-bundle",
            str(bundle_path),
            "--output",
            str(tmp_path / "paid-episode.mp4"),
            "--work-dir",
            str(tmp_path / "paid-work"),
            "--report",
            str(blocked_path),
        ]
    )
    assert code == EXIT_PROVIDER
    blocked = json.loads(blocked_path.read_text(encoding="utf-8"))
    assert blocked["paid_execution_gate"] == "operator_approval_required"
    assert blocked["external_api_calls"] == 0
    assert not (tmp_path / "paid-work" / "provider_outputs").exists()
