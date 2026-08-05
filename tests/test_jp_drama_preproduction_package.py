from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.apps.jp_drama.preproduction import PreproductionPackageManifest
from src.apps.jp_drama.workflows.build_preproduction_package import main as package_main
from src.apps.jp_drama.workflows.import_series_production_plan import main as import_main


ROOT = Path(__file__).resolve().parents[1]
LIVE_CONFIG = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"
H3_CONFIG = ROOT / "examples" / "jp_drama" / "minimax_h3_live_provider.json"
SOURCE_COMMIT = "3070100f6ff25a3994a749b240f423f703cd294f"


def _source_paths() -> tuple[Path, Path]:
    source_root = os.getenv("STORYBOARD_SOURCE_DIR")
    if not source_root:
        pytest.skip("STORYBOARD_SOURCE_DIR is required")
    project = Path(source_root) / "projects" / "一房の葡萄"
    return (
        project / "一房の葡萄_generation_plan.yaml",
        project / "一房の葡萄_asset_catalog.yaml",
    )


def _import_series(tmp_path: Path) -> tuple[Path, Path, Path]:
    series_plan, asset_catalog = _source_paths()
    output = tmp_path / "series"
    result = import_main(
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
    return output, series_plan, asset_catalog


def test_real_one_bunch_preproduction_package_is_complete_and_zero_call(
    tmp_path: Path,
) -> None:
    series_output, series_plan, asset_catalog = _import_series(tmp_path)
    output = tmp_path / "preproduction"

    result = package_main(
        [
            "--series-output",
            str(series_output),
            "--source-series-plan",
            str(series_plan),
            "--source-asset-catalog",
            str(asset_catalog),
            "--live-provider-config",
            str(LIVE_CONFIG),
            "--output-dir",
            str(output),
        ]
    )

    assert result == 0
    manifest = PreproductionPackageManifest.model_validate_json(
        (output / "preproduction_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.title == "一房の葡萄"
    assert manifest.source_commit == SOURCE_COMMIT
    assert manifest.episode_count == 3
    assert manifest.segment_count == 15
    assert manifest.base_master_asset_count == 17
    assert manifest.variant_review_asset_count >= 1
    assert manifest.voice_identity_count == 4
    assert manifest.first_frame_count == 15
    assert manifest.provider_route_count == 9
    assert manifest.contract_ready is True
    assert manifest.provider_plans_ready is True
    assert manifest.master_assets_ready is False
    assert manifest.voices_ready is False
    assert manifest.first_frames_ready is False
    assert manifest.video_generation_ready is False
    assert manifest.external_api_calls == 0
    assert {item.code for item in manifest.blockers} == {
        "master_assets_pending",
        "voice_identities_pending",
        "first_frames_pending",
        "provider_credentials_runtime_validation_pending",
        "paid_execution_approval_pending",
        "human_quality_review_pending",
    }

    assets = json.loads(
        (output / "asset_creation_checklist.json").read_text(encoding="utf-8")
    )
    assert len(assets) == 17
    by_id = {item["source_asset_id"]: item for item in assets}
    assert set(by_id) == {
        "C01", "C02", "C03", "C90", "C91",
        "S01", "S02", "S03", "S04", "S05",
        "P01", "P02", "P03", "P04", "P05", "P06", "P07",
    }
    assert by_id["C01"]["voice_identity_required"] is True
    assert by_id["C90"]["voice_identity_required"] is False
    assert by_id["P05"]["variant_review_required"] is True
    assert by_id["P05"]["instance_rules"]
    assert all(item["status"] == "pending" for item in assets)
    assert all(item["bundle_bindings"] for item in assets)

    voices = json.loads(
        (output / "voice_identity_checklist.json").read_text(encoding="utf-8")
    )
    assert {item["source_character_id"] for item in voices} == {
        "C01", "C02", "C03", "C91"
    }
    assert all(item["status"] == "pending" for item in voices)

    first_frames = json.loads(
        (output / "first_frame_plan.json").read_text(encoding="utf-8")
    )
    assert len(first_frames) == 15
    assert [item["segment_id"] for item in first_frames] == [
        f"E{episode:02d}-G{segment:02d}"
        for episode in range(1, 4)
        for segment in range(1, 6)
    ]
    assert all(item["master_reference_asset_ids"] for item in first_frames)
    assert all(len(item["master_reference_asset_ids"]) <= 9 for item in first_frames)
    assert all("--stage preflight" in item["keyframe_preflight_command"] for item in first_frames)
    assert all("--execute-paid" in item["keyframe_paid_command_template"] for item in first_frames)
    assert all("<COPY_APPROVAL_DIGEST_FROM_PREFLIGHT>" in item["keyframe_paid_command_template"] for item in first_frames)

    routes = json.loads(
        (output / "provider_route_summary.json").read_text(encoding="utf-8")
    )
    assert len(routes) == 9
    assert {(item["episode_id"], item["route"]) for item in routes} == {
        (episode, route)
        for episode in ("E01", "E02", "E03")
        for route in ("h3", "wan", "seedance")
    }
    assert all(item["planning_ready"] for item in routes)
    assert all(item["execution_route_ready"] for item in routes)
    seedance = [item for item in routes if item["route"] == "seedance"]
    assert all(item["cost"]["unknown_cost_components"] for item in seedance)

    canary = json.loads(
        (output / "canary_recommendation.json").read_text(encoding="utf-8")
    )
    assert canary["recommendation_ready"] is True
    assert canary["recommended_episode_id"] == "E01"
    assert canary["recommended_segment_id"].startswith("E01-G")
    assert len(canary["episode_decisions"]) == 3

    approval_commands = json.loads(
        (output / "bundle_approval_commands.json").read_text(encoding="utf-8")
    )
    assert len(approval_commands) == 9
    assert all("approve_asset_bundle" in item["command"] for item in approval_commands)
    assert len(list((output / "approval_templates").rglob("bindings.template.json"))) == 9

    assert (output / "README.md").is_file()
    assert (output / "source_contract" / "series_plan.yaml").is_file()
    assert (output / "source_contract" / "asset_catalog.yaml").is_file()
    assert (output / "production_contract" / "series_manifest.json").is_file()
    assert len(list((output / "production_contract").rglob("generation_plan_episode.json"))) == 9
    assert len(list((output / "production_contract").rglob("asset_bundle_pending.json"))) == 9

    forbidden = {".mp4", ".mov", ".png", ".jpg", ".jpeg", ".wav", ".mp3"}
    assert not [
        item
        for item in output.rglob("*")
        if item.is_file() and item.suffix.lower() in forbidden
    ]


def test_preproduction_build_failure_preserves_previous_output(tmp_path: Path) -> None:
    series_output, series_plan, asset_catalog = _import_series(tmp_path)
    output = tmp_path / "preproduction"
    output.mkdir()
    marker = output / "approved-previous.txt"
    marker.write_text("keep", encoding="utf-8")

    broken_catalog = tmp_path / "broken-catalog.yaml"
    broken_catalog.write_text(
        asset_catalog.read_text(encoding="utf-8").replace(
            'name: "僕"',
            'name: "別人"',
            1,
        ),
        encoding="utf-8",
    )
    result = package_main(
        [
            "--series-output",
            str(series_output),
            "--source-series-plan",
            str(series_plan),
            "--source-asset-catalog",
            str(broken_catalog),
            "--live-provider-config",
            str(LIVE_CONFIG),
            "--output-dir",
            str(output),
            "--overwrite",
        ]
    )

    assert result == 2
    assert marker.read_text(encoding="utf-8") == "keep"
    assert sorted(item.name for item in output.iterdir()) == [marker.name]
    assert not list(tmp_path.glob(".preproduction.staging-*"))
    assert not list(tmp_path.glob(".preproduction.backup-*"))
