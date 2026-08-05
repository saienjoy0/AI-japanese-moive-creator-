from __future__ import annotations

import json
from pathlib import Path

from src.apps.jp_drama.assets.bundle import prepared_content_digest
from src.apps.jp_drama.rendering.minimax_h3_adapter import (
    build_h3_first_provider_registry,
)
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
from src.apps.jp_drama.workflows.prepare_seedance_storyboard_generation import main


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "jp_drama" / "seedance_storyboard" / "upstream_fixture"
LIVE_CONFIG = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"
H3_CONFIG = ROOT / "examples" / "jp_drama" / "minimax_h3_live_provider.json"


def _package():
    return parse_project(load_project_directory(FIXTURE))


def _registry():
    return build_h3_first_provider_registry(
        LiveProviderConfig.load(LIVE_CONFIG),
        MiniMaxH3ProviderConfig.load(H3_CONFIG),
    )


def test_bridge_builds_deterministic_prepared_episode_with_csp_assets() -> None:
    package = _package()
    first = build_storyboard_prepared_episode(package, "E01")
    second = build_storyboard_prepared_episode(package, "E01")

    assert first.to_canonical_json() == second.to_canonical_json()
    assert first.source_digest == package.content_digest
    assert first.project_draft.episode_number == 1
    assert first.project_draft.series_id is not None
    assert len(first.storyboard_frame_drafts) == 5
    assert first.project_draft.target_duration_seconds == 15
    assert {item.source_character_id for item in first.character_seeds} == {
        "C01",
        "C02",
    }
    assert {item.source_location_id for item in first.location_seeds} == {"S01"}
    assert {item.source_prop_id for item in first.prop_seeds} == {"P02"}
    assert first.readiness_report.external_api_calls == 0


def test_h3_plan_preserves_one_upstream_multishot_prompt_and_asset_slots() -> None:
    package = _package()
    prepared = build_storyboard_prepared_episode(package, "E01")
    plan = compile_storyboard_generation_plan(
        package,
        prepared,
        "E01",
        route_id=SUPPORTED_ROUTES["h3"],
        registry=_registry(),
    )
    bundle = build_storyboard_asset_bundle(prepared, plan)

    assert plan.provider_route_id == "minimax/h3-reference-av"
    assert len(plan.segments) == 1
    assert len(plan.segments[0].editorial_shots) == 5
    assert plan.segments[0].requested_duration_seconds == 15
    assert plan.segments[0].audio_strategy == "native_av"
    assert "0-3秒画面" in plan.segments[0].prompt_bundle.timed_shot_prompt
    assert plan.readiness_report.planning_ready is True
    assert plan.readiness_report.execution_route_ready is True
    assert {item.subject_id for item in bundle.assets} == {
        "C01",
        "C02",
        "S01",
        "P02",
    }
    assert bundle.source_prepared_episode_digest == prepared_content_digest(prepared)
    assert bundle.generation_plan_digest == plan.content_digest


def test_wan_plan_splits_only_at_explicit_upstream_timeline_boundaries() -> None:
    package = _package()
    prepared = build_storyboard_prepared_episode(package, "E01")
    plan = compile_storyboard_generation_plan(
        package,
        prepared,
        "E01",
        route_id=SUPPORTED_ROUTES["wan"],
        registry=_registry(),
    )
    bundle = build_storyboard_asset_bundle(prepared, plan)

    assert plan.provider_route_id == "wan/i2v"
    assert len(plan.segments) == 5
    assert all(len(item.editorial_shots) == 1 for item in plan.segments)
    assert all(item.requested_duration_seconds == 3 for item in plan.segments)
    first_frames = [
        item for item in plan.reference_asset_requirements if item.role == "first_frame"
    ]
    assert len(first_frames) == 5
    assert len(bundle.assets) == 9
    assert plan.cost_plan.reference_image_calls == 5
    assert plan.cost_plan.video_calls == 5
    assert plan.readiness_report.execution_route_ready is True
    assert "wan_audio_postproduction_required" in {
        item.code for item in plan.readiness_report.warnings
    }


def test_seedance_plan_remains_one_manual_operator_job_without_false_multishot_claim() -> None:
    package = _package()
    prepared = build_storyboard_prepared_episode(package, "E01")
    plan = compile_storyboard_generation_plan(
        package,
        prepared,
        "E01",
        route_id=SUPPORTED_ROUTES["seedance"],
        registry=_registry(),
    )

    assert plan.provider_route_id == "seedance/platform"
    assert len(plan.segments) == 1
    assert len(plan.segments[0].editorial_shots) == 1
    assert "0-3秒画面" in plan.segments[0].prompt_bundle.timed_shot_prompt
    assert plan.readiness_report.execution_route_ready is True
    assert "manual_operator_route" in {
        item.code for item in plan.readiness_report.warnings
    }
    assert plan.cost_plan.unknown_cost_components


def test_continuation_instruction_survives_as_transition_and_readiness_evidence() -> None:
    package = _package()
    prepared = build_storyboard_prepared_episode(package, "E02")
    plan = compile_storyboard_generation_plan(
        package,
        prepared,
        "E02",
        route_id=SUPPORTED_ROUTES["h3"],
        registry=_registry(),
    )

    assert plan.segments[0].transition_in == "continuation"
    assert "将@视频1延长15s" in plan.segments[0].prompt_bundle.timed_shot_prompt
    assert "continuation_asset_required" in {
        item.code for item in plan.readiness_report.warnings
    }


def test_cli_writes_all_routes_and_makes_zero_provider_calls(tmp_path: Path) -> None:
    package = _package()
    package_path = tmp_path / "seedance_storyboard_package.json"
    package_path.write_text(package.to_canonical_json() + "\n", encoding="utf-8")
    output = tmp_path / "bridge-output"

    result = main(
        [
            "--input",
            str(package_path),
            "--output-dir",
            str(output),
            "--episode",
            "E01",
            "--routes",
            "h3",
            "wan",
            "seedance",
            "--live-provider-config",
            str(LIVE_CONFIG),
            "--minimax-h3-config",
            str(H3_CONFIG),
        ]
    )

    assert result == 0
    assert (output / "E01" / "prepared_episode.json").is_file()
    for route in ("h3", "wan", "seedance"):
        assert (
            output / "E01" / route / "generation_plan_episode.json"
        ).is_file()
        assert (output / "E01" / route / "asset_bundle_pending.json").is_file()
    report = json.loads((output / "bridge_report.json").read_text(encoding="utf-8"))
    assert report["external_api_calls"] == 0
    assert report["episodes"]["E01"]["routes"]["h3"]["segments"] == 1
    assert report["episodes"]["E01"]["routes"]["wan"]["segments"] == 5
