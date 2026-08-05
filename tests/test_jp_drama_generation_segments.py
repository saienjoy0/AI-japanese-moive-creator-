from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.generation import (
    ProviderSegmentationProfile,
    compile_generation_plan,
)
from src.apps.jp_drama.preparation import PreparedEpisode, compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering.provider_registry import (
    MockProviderAdapter,
    ProviderRegistry,
    SeedancePlatformAdapter,
)


ROOT = Path(__file__).resolve().parents[1]
EPISODE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
PROFILE_PATH = ROOT / "examples" / "jp_drama" / "generation" / "mock_profile.json"
SEEDANCE_PROFILE_PATH = (
    ROOT / "examples" / "jp_drama" / "generation" / "seedance20_profile.json"
)


def _prepared() -> PreparedEpisode:
    payload = json.loads(EPISODE_PATH.read_text(encoding="utf-8"))
    package = EpisodePackage.model_validate(payload)
    return compile_episode(
        package,
        catalog=load_model_catalog(CATALOG_PATH),
        strict=True,
        source_payload=payload,
    )


def _mock_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(MockProviderAdapter())
    return registry


def test_45_second_fixture_compiles_into_variable_segments() -> None:
    plan = compile_generation_plan(
        _prepared(),
        profile=ProviderSegmentationProfile.load(PROFILE_PATH),
        registry=_mock_registry(),
    )

    assert len(plan.segments) == 6
    assert [float(item.editorial_duration_seconds) for item in plan.segments] == [
        6.0,
        9.0,
        9.0,
        6.0,
        7.0,
        8.0,
    ]
    assert len({item.requested_duration_seconds for item in plan.segments}) > 1
    assert all(1 <= len(item.editorial_shots) <= 3 for item in plan.segments)
    assert any(len(item.editorial_shots) > 1 for item in plan.segments)
    assert plan.readiness_report.planning_ready is True
    assert plan.readiness_report.execution_route_ready is True
    assert plan.readiness_report.media_quality_validated is False
    assert plan.readiness_report.external_api_calls == 0


def test_frame_timeline_and_dialogue_coverage_are_exact() -> None:
    prepared = _prepared()
    plan = compile_generation_plan(
        prepared,
        profile=ProviderSegmentationProfile.load(PROFILE_PATH),
        registry=_mock_registry(),
    )

    assert sum(item.editorial_frame_count for item in plan.segments) == 45 * 30
    assert plan.segments[0].editorial_start_frame == 0
    assert plan.segments[-1].editorial_end_frame == 45 * 30
    source_cues = sorted(
        cue.cue_id
        for frame in prepared.storyboard_frame_drafts
        for cue in frame.dialogue_cues
    )
    assigned_cues = sorted(
        item.source_dialogue_id
        for segment in plan.segments
        for item in segment.dialogue_slices
    )
    assert assigned_cues == source_cues
    for segment in plan.segments:
        assert segment.used_end_frame - segment.used_start_frame == segment.editorial_frame_count
        assert segment.used_end_frame <= segment.requested_duration_seconds * segment.timeline_fps
        assert all(
            item.end_frame <= segment.editorial_frame_count
            for item in segment.dialogue_slices
        )


def test_same_input_is_byte_identical() -> None:
    prepared = _prepared()
    profile = ProviderSegmentationProfile.load(PROFILE_PATH)
    first = compile_generation_plan(prepared, profile=profile, registry=_mock_registry())
    second = compile_generation_plan(prepared, profile=profile, registry=_mock_registry())

    assert first.to_canonical_json(indent=None) == second.to_canonical_json(indent=None)
    assert first.content_digest == second.content_digest
    assert [item.segment_id for item in first.segments] == [
        item.segment_id for item in second.segments
    ]


def test_seedance_minimum_is_respected_but_current_route_is_not_overclaimed() -> None:
    registry = ProviderRegistry()
    registry.register(SeedancePlatformAdapter())
    plan = compile_generation_plan(
        _prepared(),
        profile=ProviderSegmentationProfile.load(SEEDANCE_PROFILE_PATH),
        registry=registry,
    )

    assert all(item.requested_duration_seconds >= 4 for item in plan.segments)
    assert all(item.requested_duration_seconds <= 15 for item in plan.segments)
    assert plan.readiness_report.planning_ready is True
    assert plan.readiness_report.execution_route_ready is False
    assert "route_multi_shot_not_migrated" in {
        item.code for item in plan.readiness_report.errors
    }


def test_missing_explicit_boundary_is_reported_instead_of_invented() -> None:
    prepared = _prepared()
    frames = list(prepared.storyboard_frame_drafts)
    frames[0] = frames[0].model_copy(update={"dialogue_cues": []})
    changed = prepared.model_copy(update={"storyboard_frame_drafts": frames})
    plan = compile_generation_plan(
        changed,
        profile=ProviderSegmentationProfile.load(PROFILE_PATH),
        registry=_mock_registry(),
    )

    assert plan.readiness_report.planning_ready is False
    assert "insufficient_segmentation_evidence" in {
        item.code for item in plan.readiness_report.errors
    }
    assert any(
        item.editorial_frame_count == 15 * 30
        for item in plan.segments
        if "shot_01" in item.parent_shot_ids
    )


def test_cli_writes_only_deterministic_offline_artifacts(tmp_path: Path) -> None:
    prepared_path = tmp_path / "prepared_episode.json"
    prepared_path.write_text(_prepared().to_canonical_json() + "\n", encoding="utf-8")
    output = tmp_path / "generation"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.apps.jp_drama.workflows.prepare_generation",
            "--input",
            str(prepared_path),
            "--output-dir",
            str(output),
            "--profile",
            str(PROFILE_PATH),
            "--print-report",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    expected = {
        "generation_plan_episode.json",
        "generation_segments.json",
        "editorial_shots.json",
        "continuity_contracts.json",
        "reference_asset_requirements.json",
        "generation_render_graph.json",
        "generation_cost_plan.json",
        "generation_readiness_report.json",
        "summary.txt",
    }
    assert {item.name for item in output.iterdir()} == expected
    report = json.loads(
        (output / "generation_readiness_report.json").read_text(encoding="utf-8")
    )
    assert report["planning_ready"] is True
    assert report["execution_route_ready"] is True
    assert report["external_api_calls"] == 0
    assert report["media_quality_validated"] is False
