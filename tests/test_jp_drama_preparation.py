from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.preparation import ModelCatalog, PreparedEpisode, compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.preparation.models import RenderGraph, RenderTaskNode


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"


@pytest.fixture()
def payload() -> dict:
    return json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def catalog() -> ModelCatalog:
    return load_model_catalog(CATALOG_PATH)


def _compile(payload: dict, catalog: ModelCatalog, *, strict: bool = False) -> PreparedEpisode:
    package = EpisodePackage.model_validate(payload)
    return compile_episode(
        package,
        catalog=catalog,
        strict=strict,
        source_payload=payload,
    )


def test_sample_json_compiles_and_is_ready(payload: dict, catalog: ModelCatalog) -> None:
    prepared = _compile(payload, catalog, strict=True)

    assert prepared.readiness_report.generation_ready is True
    assert prepared.readiness_report.external_api_calls == 0
    assert prepared.readiness_report.errors == []
    assert prepared.readiness_report.warnings == []
    assert prepared.budget_snapshot.estimated_total == 2200


def test_same_input_produces_identical_json(payload: dict, catalog: ModelCatalog) -> None:
    first = _compile(deepcopy(payload), catalog, strict=True)
    second = _compile(deepcopy(payload), catalog, strict=True)

    assert first.to_canonical_json(indent=None) == second.to_canonical_json(indent=None)
    assert first.source_digest == second.source_digest


def test_every_shot_has_one_render_intent(payload: dict, catalog: ModelCatalog) -> None:
    prepared = _compile(payload, catalog)

    shot_ids = {shot["shot_id"] for shot in payload["shot_plan"]["shots"]}
    intent_ids = [intent.shot_id for intent in prepared.render_intents]
    assert set(intent_ids) == shot_ids
    assert len(intent_ids) == len(set(intent_ids))


def test_all_character_location_and_prop_references_resolve(
    payload: dict,
    catalog: ModelCatalog,
) -> None:
    prepared = _compile(payload, catalog)
    character_ids = {seed.seed_id for seed in prepared.character_seeds}
    location_ids = {seed.seed_id for seed in prepared.location_seeds}
    prop_ids = {seed.seed_id for seed in prepared.prop_seeds}

    for frame in prepared.storyboard_frame_drafts:
        assert set(frame.character_seed_ids) <= character_ids
        assert frame.location_seed_id in location_ids
        assert set(frame.prop_seed_ids) <= prop_ids
    assert prepared.mapping_trace.mapping_coverage == 1.0


def test_storyboard_duration_is_45_seconds(payload: dict, catalog: ModelCatalog) -> None:
    prepared = _compile(payload, catalog)
    assert sum(frame.duration_seconds for frame in prepared.storyboard_frame_drafts) == 45


def test_silent_video_with_dialogue_is_rejected(payload: dict, catalog: ModelCatalog) -> None:
    broken = deepcopy(payload)
    broken["shot_plan"]["shots"][0]["render_strategy"] = "silent_video"
    broken["cost_plan"]["shot_estimates"][0]["render_strategy"] = "silent_video"
    broken["cost_plan"]["shot_estimates"][0]["fallback_strategy"] = None

    prepared = _compile(broken, catalog)
    assert "silent_video_has_dialogue" in {
        issue.code for issue in prepared.readiness_report.errors
    }


def test_unsupported_native_av_without_fallback_is_rejected(
    payload: dict,
    catalog: ModelCatalog,
) -> None:
    broken = deepcopy(payload)
    broken["cost_plan"]["shot_estimates"][1]["fallback_strategy"] = None
    changed_catalog = catalog.model_copy(deep=True)
    profile = changed_catalog.get("provider-b", "model-av")
    assert profile is not None
    profile.capabilities.remove("exact_dialogue")

    prepared = _compile(broken, changed_catalog)
    assert "model_capability_missing" in {
        issue.code for issue in prepared.readiness_report.errors
    }


def test_undeclared_strategy_change_is_not_applied(
    payload: dict,
    catalog: ModelCatalog,
) -> None:
    broken = deepcopy(payload)
    broken["cost_plan"]["shot_estimates"][1]["fallback_strategy"] = None
    changed_catalog = catalog.model_copy(deep=True)
    profile = changed_catalog.get("provider-b", "model-av")
    assert profile is not None
    profile.capabilities.remove("exact_dialogue")

    prepared = _compile(broken, changed_catalog)
    assert all(not intent.fallback_applied for intent in prepared.render_intents)
    assert prepared.readiness_report.generation_ready is False


def test_only_explicit_fallback_is_applied(payload: dict, catalog: ModelCatalog) -> None:
    changed_catalog = catalog.model_copy(deep=True)
    profile = changed_catalog.get("provider-b", "model-av")
    assert profile is not None
    profile.capabilities.remove("exact_dialogue")

    prepared = _compile(payload, changed_catalog)
    intent = next(intent for intent in prepared.render_intents if intent.shot_id == "shot_02")
    assert intent.requested_strategy.value == "native_av"
    assert intent.resolved_strategy.value == "video_plus_tts"
    assert intent.fallback_applied is True
    assert "explicitly declared fallback" in (intent.resolution_reason or "")


def test_every_resolved_shot_has_cost_information(payload: dict, catalog: ModelCatalog) -> None:
    prepared = _compile(payload, catalog)
    assert len(prepared.budget_snapshot.shot_items) == len(payload["shot_plan"]["shots"])
    assert all(
        item.total_cost == item.primary_cost + item.retry_cost
        for item in prepared.budget_snapshot.shot_items
    )


def test_fallback_recalculates_cost_from_catalog(payload: dict, catalog: ModelCatalog) -> None:
    changed_catalog = catalog.model_copy(deep=True)
    profile = changed_catalog.get("provider-b", "model-av")
    assert profile is not None
    profile.capabilities.remove("exact_dialogue")

    prepared = _compile(payload, changed_catalog)
    intent = next(intent for intent in prepared.render_intents if intent.shot_id == "shot_02")
    assert intent.estimated_primary_cost == 600
    assert intent.reserved_retry_cost == 200
    assert intent.estimated_total_cost == 800


def test_hard_stop_budget_overage_is_an_error(payload: dict, catalog: ModelCatalog) -> None:
    changed_catalog = catalog.model_copy(deep=True)
    profile = changed_catalog.get("provider-b", "model-av")
    assert profile is not None
    profile.capabilities.remove("exact_dialogue")
    quote = profile.fallback_costs[1]
    quote.estimated_primary_cost = 1000
    quote.reserved_retry_cost = 400

    prepared = _compile(payload, changed_catalog)
    budget_issues = [
        issue for issue in prepared.readiness_report.errors if issue.code == "budget_exceeded"
    ]
    assert len(budget_issues) == 1
    assert prepared.readiness_report.generation_ready is False


def test_soft_budget_overage_is_a_warning(payload: dict, catalog: ModelCatalog) -> None:
    soft = deepcopy(payload)
    soft["cost_plan"]["hard_stop"] = False
    soft["cost_plan"]["budget_limit"] = 2000

    prepared = _compile(soft, catalog)
    assert "budget_exceeded" in {
        issue.code for issue in prepared.readiness_report.warnings
    }
    assert prepared.readiness_report.errors == []
    assert prepared.readiness_report.generation_ready is True


def test_api_keys_are_not_required(
    payload: dict,
    catalog: ModelCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in list(os.environ):
        if "API_KEY" in key or key.endswith("_TOKEN"):
            monkeypatch.delenv(key, raising=False)
    assert _compile(payload, catalog).readiness_report.external_api_calls == 0


def test_compiler_does_not_open_network_connections(
    payload: dict,
    catalog: ModelCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden in PR4")

    monkeypatch.setattr(socket, "create_connection", blocked)
    prepared = _compile(payload, catalog)
    assert prepared.readiness_report.external_api_calls == 0


def test_cli_writes_only_offline_preparation_outputs(tmp_path: Path) -> None:
    output = tmp_path / "prepared"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.apps.jp_drama.workflows.prepare_episode",
            "--input",
            str(EXAMPLE_PATH),
            "--output",
            str(output),
            "--catalog",
            str(CATALOG_PATH),
            "--dry-run",
            "--strict",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in output.iterdir()} == {
        "prepared_episode.json",
        "readiness_report.json",
        "summary.txt",
    }
    report = json.loads((output / "readiness_report.json").read_text(encoding="utf-8"))
    assert report["external_api_calls"] == 0
    assert report["generation_ready"] is True


def test_render_graph_is_acyclic(payload: dict, catalog: ModelCatalog) -> None:
    prepared = _compile(payload, catalog)
    task_ids = {node.task_id for node in prepared.render_graph.nodes}
    assert task_ids
    assert all(set(node.depends_on) <= task_ids for node in prepared.render_graph.nodes)


def test_render_graph_rejects_cycles() -> None:
    with pytest.raises(ValueError, match="cycle"):
        RenderGraph(
            nodes=[
                RenderTaskNode(
                    task_id="a",
                    shot_id="shot_01",
                    task_type="finalize_shot",
                    depends_on=["b"],
                    external_api_required=False,
                    provider_required=False,
                ),
                RenderTaskNode(
                    task_id="b",
                    shot_id="shot_01",
                    task_type="mux_audio_video",
                    depends_on=["a"],
                    external_api_required=False,
                    provider_required=False,
                ),
            ]
        )


def test_prepared_canonical_json_round_trips(payload: dict, catalog: ModelCatalog) -> None:
    prepared = _compile(payload, catalog)
    restored = PreparedEpisode.model_validate_json(prepared.to_canonical_json())
    assert restored == prepared


def test_strict_mode_blocks_warnings(payload: dict, catalog: ModelCatalog) -> None:
    warned = deepcopy(payload)
    warned["shot_plan"]["shots"][0]["audio"].pop("bgm_cue")

    normal = _compile(warned, catalog, strict=False)
    strict = _compile(warned, catalog, strict=True)
    assert normal.readiness_report.generation_ready is True
    assert strict.readiness_report.generation_ready is False
    assert "bgm_policy_missing" in {
        issue.code for issue in strict.readiness_report.warnings
    }
