from __future__ import annotations

import json
import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.generation import (
    ProviderSegmentationProfile,
    compile_generation_plan,
)
from src.apps.jp_drama.preparation import PreparedEpisode, compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.production import (
    ProductionComposeError,
    ProductionEpisodeComposer,
    SegmentArtifact,
    SegmentArtifactManifest,
)
from src.apps.jp_drama.rendering.ffmpeg import (
    ffmpeg,
    ffprobe_json,
    file_sha256,
    run_command,
)
from src.apps.jp_drama.rendering.provider_registry import (
    MockProviderAdapter,
    ProviderRegistry,
)
from src.apps.jp_drama.workflows.run_production_episode import main as production_main


ROOT = Path(__file__).resolve().parents[1]
EPISODE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
PROFILE_PATH = ROOT / "examples" / "jp_drama" / "generation" / "mock_profile.json"


def _prepared() -> PreparedEpisode:
    payload = json.loads(EPISODE_PATH.read_text(encoding="utf-8"))
    package = EpisodePackage.model_validate(payload)
    return compile_episode(
        package,
        catalog=load_model_catalog(CATALOG_PATH),
        strict=True,
        source_payload=payload,
    )


def _plan(prepared: PreparedEpisode):
    registry = ProviderRegistry()
    registry.register(MockProviderAdapter())
    return compile_generation_plan(
        prepared,
        profile=ProviderSegmentationProfile.load(PROFILE_PATH),
        registry=registry,
    )


def _count_frames(path: Path) -> int:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ]
    )
    return int(result.stdout.strip())


def _rate(value: str) -> float:
    return float(Fraction(value))


def _artifact(segment, path: Path, plan_digest: str) -> SegmentArtifact:
    probe = ffprobe_json(path)
    video = next(item for item in probe["streams"] if item["codec_type"] == "video")
    return SegmentArtifact(
        segment_id=segment.segment_id,
        generation_plan_digest=plan_digest,
        provider_route_id=segment.provider_route_id,
        output_path=str(path.resolve()),
        output_sha256=file_sha256(path),
        width=int(video["width"]),
        height=int(video["height"]),
        fps=_rate(video["avg_frame_rate"]),
        frame_count=_count_frames(path),
        duration_seconds=float(probe["format"]["duration"]),
        audio_present=False,
        approval_digest="sha256:" + "1" * 64,
        imported_by="pytest",
        valid=True,
    )


def test_segment_artifact_manifest_is_digest_bound() -> None:
    digest = "sha256:" + "a" * 64
    artifact = SegmentArtifact(
        segment_id="segment-1",
        generation_plan_digest=digest,
        provider_route_id="mock/video",
        output_path="/tmp/segment.mp4",
        output_sha256="sha256:" + "b" * 64,
        width=90,
        height=160,
        fps=30,
        frame_count=90,
        duration_seconds=3,
        audio_present=False,
        approval_digest="sha256:" + "c" * 64,
        valid=True,
    )
    manifest = SegmentArtifactManifest.build_with_digest(
        generation_plan_digest=digest,
        artifacts=[artifact],
    )
    assert manifest.content_digest.startswith("sha256:")
    assert (
        SegmentArtifactManifest.model_validate_json(manifest.to_canonical_json())
        == manifest
    )
    changed = json.loads(manifest.to_canonical_json())
    changed["artifacts"][0]["output_path"] = "/tmp/other.mp4"
    with pytest.raises(ValueError, match="digest"):
        SegmentArtifactManifest.model_validate(changed)


def test_valid_segment_artifact_requires_approval_digest() -> None:
    digest = "sha256:" + "a" * 64
    with pytest.raises(ValueError, match="approval_digest"):
        SegmentArtifact(
            segment_id="segment-1",
            generation_plan_digest=digest,
            provider_route_id="mock/video",
            output_path="/tmp/segment.mp4",
            output_sha256="sha256:" + "b" * 64,
            width=90,
            height=160,
            fps=30,
            frame_count=90,
            duration_seconds=3,
            audio_present=False,
            valid=True,
        )


def test_production_render_is_fail_closed_with_zero_calls(tmp_path: Path) -> None:
    prepared = _prepared()
    plan = _plan(prepared)
    prepared_path = tmp_path / "prepared.json"
    plan_path = tmp_path / "plan.json"
    report_path = tmp_path / "render-blocked.json"
    prepared_path.write_text(prepared.to_canonical_json() + "\n", encoding="utf-8")
    plan_path.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")

    result = production_main(
        [
            "--prepared-input",
            str(prepared_path),
            "--generation-plan",
            str(plan_path),
            "--stage",
            "render",
            "--report",
            str(report_path),
        ]
    )

    assert result == 6
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["paid_execution_gate"] == "common_provider_dispatcher_not_implemented"
    assert report["external_api_calls"] == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_existing_segment_artifacts_compose_exact_episode(tmp_path: Path) -> None:
    prepared = _prepared()
    plan = _plan(prepared)
    artifacts: list[SegmentArtifact] = []
    source_dir = tmp_path / "provider"
    source_dir.mkdir()

    for segment in plan.segments:
        source = source_dir / f"{segment.order:03d}_{segment.segment_id}.mp4"
        frames = segment.requested_duration_seconds * segment.timeline_fps
        ffmpeg(
            "-f",
            "lavfi",
            "-i",
            (
                f"testsrc2=size=90x160:rate={segment.timeline_fps}:"
                f"duration={segment.requested_duration_seconds}"
            ),
            "-frames:v",
            str(frames),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(source),
        )
        artifacts.append(_artifact(segment, source, plan.content_digest))

    manifest = SegmentArtifactManifest.build_with_digest(
        generation_plan_digest=plan.content_digest,
        artifacts=artifacts,
    )
    output = tmp_path / "episode.mp4"
    composer = ProductionEpisodeComposer(
        plan,
        manifest,
        output_file=output,
        work_dir=tmp_path / "work",
        target_width=90,
        target_height=160,
    )

    report = composer.compose(reset=True)

    assert report.valid is True
    assert report.external_api_calls == 0
    assert report.actual_frame_count == plan.target_frame_count
    assert report.expected_frame_count == 45 * 30
    assert report.segment_order == [item.segment_id for item in plan.segments]
    assert len(report.segment_validations) == len(plan.segments)
    assert all(item.valid for item in report.segment_validations)
    assert output.is_file()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_compose_rejects_stale_segment_sha(tmp_path: Path) -> None:
    prepared = _prepared()
    plan = _plan(prepared)
    segment = plan.segments[0]
    source = tmp_path / "segment.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        (
            f"testsrc2=size=90x160:rate={segment.timeline_fps}:"
            f"duration={segment.requested_duration_seconds}"
        ),
        "-frames:v",
        str(segment.requested_duration_seconds * segment.timeline_fps),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(source),
    )
    artifacts = []
    for item in plan.segments:
        artifact = _artifact(segment, source, plan.content_digest).model_copy(
            update={
                "segment_id": item.segment_id,
                "provider_route_id": item.provider_route_id,
                "output_sha256": (
                    "sha256:" + "0" * 64
                    if item.segment_id == segment.segment_id
                    else file_sha256(source)
                ),
            }
        )
        artifacts.append(artifact)
    manifest = SegmentArtifactManifest.build_with_digest(
        generation_plan_digest=plan.content_digest,
        artifacts=artifacts,
    )
    composer = ProductionEpisodeComposer(
        plan,
        manifest,
        output_file=tmp_path / "episode.mp4",
        work_dir=tmp_path / "work",
        target_width=90,
        target_height=160,
    )

    with pytest.raises(ProductionComposeError, match="SHA-256"):
        composer.compose()
