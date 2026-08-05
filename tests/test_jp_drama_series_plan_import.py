from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.apps.jp_drama.assets.models import ApprovedAssetBundle
from src.apps.jp_drama.generation.models import GenerationPlanEpisode
from src.apps.jp_drama.preparation.models import PreparedEpisode
from src.apps.jp_drama.series_plan import (
    SeriesPlanError,
    SeriesProductionManifest,
    load_series_inputs,
)
from src.apps.jp_drama.workflows.import_series_production_plan import main


ROOT = Path(__file__).resolve().parents[1]
LIVE_CONFIG = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"
H3_CONFIG = ROOT / "examples" / "jp_drama" / "minimax_h3_live_provider.json"
SOURCE_COMMIT = "3070100f6ff25a3994a749b240f423f703cd294f"


def _source_paths() -> tuple[Path, Path]:
    source_root = os.getenv("STORYBOARD_SOURCE_DIR")
    if not source_root:
        pytest.skip("STORYBOARD_SOURCE_DIR is required for pinned cross-repository fixture")
    project = Path(source_root) / "projects" / "一房の葡萄"
    return (
        project / "一房の葡萄_generation_plan.yaml",
        project / "一房の葡萄_asset_catalog.yaml",
    )


def test_real_one_bunch_series_import_preserves_three_episode_contract(
    tmp_path: Path,
) -> None:
    series_plan, asset_catalog = _source_paths()
    output = tmp_path / "series"

    result = main(
        [
            "--series-plan",
            str(series_plan),
            "--asset-catalog",
            str(asset_catalog),
            "--output-dir",
            str(output),
            "--live-provider-config",
            str(LIVE_CONFIG),
            "--minimax-h3-config",
            str(H3_CONFIG),
            "--source-commit",
            SOURCE_COMMIT,
            "--require-route-ready",
        ]
    )

    assert result == 0
    report = json.loads((output / "import_report.json").read_text(encoding="utf-8"))
    assert report["valid"] is True
    assert report["external_api_calls"] == 0
    assert report["episode_count"] == 3
    assert report["segment_count"] == 15
    assert report["routes"] == ["h3", "wan", "seedance"]
    assert report["route_blockers"] == []

    manifest = SeriesProductionManifest.model_validate_json(
        (output / "series_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.source_title == "一房の葡萄"
    assert manifest.source_author == "有島武郎"
    assert manifest.rights_status == "public_domain"
    assert manifest.source_repository == "saienjoy0/Storyboard-Generator"
    assert manifest.source_commit == SOURCE_COMMIT
    assert manifest.episode_count == 3
    assert manifest.segment_count == 15
    assert manifest.timeline_fps == 24
    assert manifest.episode_frame_count == 1200
    assert manifest.total_frame_count == 3600
    assert manifest.external_api_calls == 0

    expected_ids = ["E01", "E02", "E03"]
    all_dialogue_counts: dict[str, int] = {}
    for number, episode_id in enumerate(expected_ids, start=1):
        episode_dir = output / episode_id
        prepared = PreparedEpisode.model_validate_json(
            (episode_dir / "prepared_episode.json").read_text(encoding="utf-8")
        )
        assert prepared.project_draft.series_id == "hitofusa-no-budou-three-episode"
        assert prepared.project_draft.episode_number == number
        assert prepared.project_draft.fps == 24
        assert prepared.project_draft.target_duration_seconds == 50
        assert len(prepared.storyboard_frame_drafts) == 5
        assert sum(item.duration_seconds for item in prepared.storyboard_frame_drafts) == 50
        assert prepared.readiness_report.generation_ready is True
        assert prepared.readiness_report.external_api_calls == 0

        route_plans: dict[str, GenerationPlanEpisode] = {}
        for route in ("h3", "wan", "seedance"):
            plan = GenerationPlanEpisode.model_validate_json(
                (episode_dir / route / "generation_plan_episode.json").read_text(
                    encoding="utf-8"
                )
            )
            bundle = ApprovedAssetBundle.model_validate_json(
                (episode_dir / route / "asset_bundle_pending.json").read_text(
                    encoding="utf-8"
                )
            )
            route_plans[route] = plan
            assert plan.timeline_fps == 24
            assert plan.target_frame_count == 1200
            assert plan.target_duration_seconds == 50
            assert len(plan.segments) == 5
            assert [item.segment_id for item in plan.segments] == [
                f"{episode_id}-G{index:02d}" for index in range(1, 6)
            ]
            assert all(item.editorial_frame_count == 240 for item in plan.segments)
            assert all(item.requested_duration_seconds == 10 for item in plan.segments)
            assert all(item.used_start_frame == 0 for item in plan.segments)
            assert all(item.used_end_frame == 240 for item in plan.segments)
            assert plan.readiness_report.planning_ready is True
            assert plan.readiness_report.execution_route_ready is True
            assert plan.readiness_report.external_api_calls == 0
            assert bundle.source_prepared_episode_digest == plan.source_prepared_episode_digest
            assert bundle.generation_plan_digest == plan.content_digest
            assert all(item.approval_status == "pending" for item in bundle.assets)
            assert all(item.approval_status == "pending" for item in bundle.voice_profiles)

        assert route_plans["h3"].provider_route_id == "minimax/h3-reference-av"
        assert route_plans["wan"].provider_route_id == "wan/i2v"
        assert route_plans["seedance"].provider_route_id == "seedance/platform"
        assert route_plans["wan"].cost_plan.reference_image_calls == 5
        assert route_plans["wan"].cost_plan.video_calls == 5
        assert route_plans["h3"].cost_plan.video_calls == 5
        assert route_plans["seedance"].cost_plan.unknown_cost_components
        assert "manual_operator_route" in {
            item.code for item in route_plans["seedance"].readiness_report.warnings
        }
        all_dialogue_counts[episode_id] = sum(
            len(item.dialogue_slices) for item in route_plans["h3"].segments
        )

    assert all_dialogue_counts == {"E01": 6, "E02": 9, "E03": 10}

    e01 = PreparedEpisode.model_validate_json(
        (output / "E01" / "prepared_episode.json").read_text(encoding="utf-8")
    )
    p02 = next(item for item in e01.prop_seeds if item.source_prop_id == "P02")
    assert "必ず二個だけ" in p02.visual_prompt

    e02 = PreparedEpisode.model_validate_json(
        (output / "E02" / "prepared_episode.json").read_text(encoding="utf-8")
    )
    e03 = PreparedEpisode.model_validate_json(
        (output / "E03" / "prepared_episode.json").read_text(encoding="utf-8")
    )
    e02_grape = next(item for item in e02.prop_seeds if item.source_prop_id == "P05")
    e03_grape = next(item for item in e03.prop_seeds if item.source_prop_id == "P05")
    assert "最初の日の一房" in e02_grape.visual_prompt
    assert "翌日の別の一房" in e03_grape.visual_prompt
    assert e02_grape.visual_prompt != e03_grape.visual_prompt

    e03_h3 = GenerationPlanEpisode.model_validate_json(
        (output / "E03" / "h3" / "generation_plan_episode.json").read_text(
            encoding="utf-8"
        )
    )
    gate_memory = next(item for item in e03_h3.segments if item.segment_id == "E03-G01")
    memory_voice = next(
        item for item in gate_memory.dialogue_slices if item.speaker_character_id == "C03"
    )
    assert memory_voice.lip_sync_required is False
    assert "C03" not in gate_memory.character_ids


def test_cross_contract_rejects_missing_referenced_asset(tmp_path: Path) -> None:
    series_plan, asset_catalog = _source_paths()
    catalog_payload = asset_catalog.read_text(encoding="utf-8")
    catalog_payload = catalog_payload.replace('  - asset_id: "P07"', '  - asset_id: "P99"', 1)
    broken_catalog = tmp_path / "broken_assets.yaml"
    broken_catalog.write_text(catalog_payload, encoding="utf-8")

    with pytest.raises(SeriesPlanError, match="unknown assets"):
        load_series_inputs(series_plan, broken_catalog)
