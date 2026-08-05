from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.generation import (
    ProviderSegmentationProfile,
    compile_generation_plan,
    select_safe_canary_candidate,
)
from src.apps.jp_drama.preparation import PreparedEpisode, compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering.provider_config import LiveProviderConfig
from src.apps.jp_drama.rendering.provider_registry import build_default_provider_registry
from src.apps.jp_drama.rendering.segment_canary import (
    SEGMENT_CANARY_PROTOCOL,
    SegmentCanaryError,
    materialize_generation_segment_canary,
    prepared_content_digest,
    validate_segment_canary_contract,
)


ROOT = Path(__file__).resolve().parents[1]
EPISODE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
PROFILE_PATH = ROOT / "examples" / "jp_drama" / "generation" / "wan27_profile.json"
PROVIDERS_PATH = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"


def _prepared() -> PreparedEpisode:
    payload = json.loads(EPISODE_PATH.read_text(encoding="utf-8"))
    package = EpisodePackage.model_validate(payload)
    return compile_episode(
        package,
        catalog=load_model_catalog(CATALOG_PATH),
        strict=True,
        source_payload=payload,
    )


def _provider_payload(*, clip_seconds: int = 15) -> dict:
    payload = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    payload["dashscope"]["provider_clip_seconds"] = clip_seconds
    return payload


def _plan(prepared: PreparedEpisode, config: LiveProviderConfig):
    return compile_generation_plan(
        prepared,
        profile=ProviderSegmentationProfile.load(PROFILE_PATH),
        registry=build_default_provider_registry(config),
    )


def _preflight_fixture(tmp_path: Path):
    prepared = _prepared()
    provider_payload = _provider_payload()
    providers = tmp_path / "providers.json"
    providers.write_text(
        json.dumps(provider_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    config = LiveProviderConfig.model_validate(provider_payload)
    plan = _plan(prepared, config)
    decision = select_safe_canary_candidate(
        plan,
        provider_clip_seconds=config.dashscope.provider_clip_seconds,
    )
    assert decision.selected_segment_id is not None
    segment = next(
        item for item in plan.segments if item.segment_id == decision.selected_segment_id
    )
    prepared_path = tmp_path / "prepared.json"
    plan_path = tmp_path / "plan.json"
    prepared_path.write_text(prepared.to_canonical_json() + "\n", encoding="utf-8")
    plan_path.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")
    return prepared, plan, segment, providers, prepared_path, plan_path


def _run_cli(
    *,
    prepared_path: Path,
    plan_path: Path,
    providers: Path,
    output_path: Path,
    report_path: Path,
    max_cost_cny: str,
    stage: str = "preflight",
    segment_id: str = "auto",
    credentials: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for key in (
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_BASE_URL",
        "DASHSCOPE_UPLOAD_BASE_URL",
        "DASHSCOPE_WORKSPACE_ID",
    ):
        environment.pop(key, None)
    if credentials:
        environment["DASHSCOPE_API_KEY"] = "test-key-not-used-by-preflight"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "src.apps.jp_drama.workflows.render_generation_segment_canary",
            "--prepared-input",
            str(prepared_path),
            "--generation-plan",
            str(plan_path),
            "--segment-id",
            segment_id,
            "--output",
            str(output_path),
            "--providers",
            str(providers),
            "--stage",
            stage,
            "--max-cost-cny",
            max_cost_cny,
            "--report",
            str(report_path),
            "--print-report",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_selector_rejects_multi_shot_and_selects_single_shot() -> None:
    prepared = _prepared()
    config = LiveProviderConfig.model_validate(_provider_payload())
    plan = _plan(prepared, config)

    decision = select_safe_canary_candidate(
        plan,
        provider_clip_seconds=config.dashscope.provider_clip_seconds,
    )

    assert decision.selected_segment_id is not None
    selected = next(
        item for item in plan.segments if item.segment_id == decision.selected_segment_id
    )
    assert len(selected.editorial_shots) == 1
    assert selected.provider_route_id == "wan/i2v"
    assert selected.audio_strategy == "silent"
    assert not selected.dialogue_slices
    rejected_multi = [
        item
        for item in decision.rejected_segments
        if "provider_multi_shot_not_supported" in item.reason_codes
    ]
    assert rejected_multi


def test_materialized_canary_rebuilds_exact_mapping_trace() -> None:
    prepared = _prepared()
    config = LiveProviderConfig.model_validate(_provider_payload())
    plan = _plan(prepared, config)
    decision = select_safe_canary_candidate(
        plan,
        provider_clip_seconds=config.dashscope.provider_clip_seconds,
    )
    assert decision.selected_segment_id is not None
    segment = next(
        item for item in plan.segments if item.segment_id == decision.selected_segment_id
    )

    materialized = materialize_generation_segment_canary(
        prepared,
        plan,
        segment.segment_id,
        provider_clip_seconds=config.dashscope.provider_clip_seconds,
    )

    frame = materialized.storyboard_frame_drafts[0]
    assert frame.source_shot_id == segment.segment_id
    assert frame.duration_seconds == segment.requested_duration_seconds
    assert frame.dialogue_cues == []
    assert materialized.mapping_trace.mapping_coverage == 1.0
    assert len(materialized.mapping_trace.shots) == 1
    assert materialized.mapping_trace.shots[0].source_id == segment.segment_id
    assert materialized.mapping_trace.shots[0].target_id == frame.frame_id
    assert {node.shot_id for node in materialized.render_graph.nodes} == {
        segment.segment_id
    }
    assert [node.task_type for node in materialized.render_graph.nodes] == [
        "generate_video",
        "finalize_shot",
    ]
    assert materialized.render_intents[0].tasks == [
        "generate_video",
        "finalize_shot",
    ]
    assert PreparedEpisode.model_validate_json(materialized.to_canonical_json())


def test_contract_rejects_wrong_prepared_episode_digest() -> None:
    prepared = _prepared()
    config = LiveProviderConfig.model_validate(_provider_payload())
    plan = _plan(prepared, config)
    decision = select_safe_canary_candidate(
        plan,
        provider_clip_seconds=config.dashscope.provider_clip_seconds,
    )
    assert decision.selected_segment_id is not None
    segment = next(
        item for item in plan.segments if item.segment_id == decision.selected_segment_id
    )
    changed = prepared.model_copy(
        update={
            "project_draft": prepared.project_draft.model_copy(
                update={"title": prepared.project_draft.title + " changed"}
            )
        }
    )

    assert prepared_content_digest(changed) != plan.source_prepared_episode_digest
    with pytest.raises(SegmentCanaryError, match="does not belong"):
        validate_segment_canary_contract(changed, plan, segment)


def test_contract_rejects_multi_shot_without_escape_hatch() -> None:
    prepared = _prepared()
    config = LiveProviderConfig.model_validate(_provider_payload())
    plan = _plan(prepared, config)
    multi = next(item for item in plan.segments if len(item.editorial_shots) > 1)

    with pytest.raises(SegmentCanaryError, match="exactly one EditorialShot"):
        validate_segment_canary_contract(prepared, plan, multi)


def test_cli_auto_preflight_makes_zero_paid_calls(tmp_path: Path) -> None:
    _, plan, segment, providers, prepared_path, plan_path = _preflight_fixture(tmp_path)
    output_path = tmp_path / "segment.mp4"
    report_path = tmp_path / "report.json"

    result = _run_cli(
        prepared_path=prepared_path,
        plan_path=plan_path,
        providers=providers,
        output_path=output_path,
        report_path=report_path,
        max_cost_cny="10.0",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["canary_protocol"] == SEGMENT_CANARY_PROTOCOL
    assert report["segment_id"] == segment.segment_id
    assert report["generation_plan_digest"] == plan.content_digest
    assert report["editorial_shot_count"] == 1
    assert report["execution_mode"] == "single_shot"
    assert report["planned_keyframe_calls"] == 1
    assert report["planned_render_calls"] == 1
    assert report["planned_api_calls"] == 2
    assert report["execution_budget"]["unknown_components"] == []
    assert report["external_api_calls"] == 0
    assert report["credentials_present"] is False
    assert report["within_requested_call_limit"] is True
    assert report["within_requested_cost_limit"] is True
    assert report["candidate_selection"]["selected_segment_id"] == segment.segment_id
    assert Path(report["materialized_prepared_episode"]).is_file()
    assert not output_path.exists()


def test_budget_gate_reports_real_credential_presence_without_calling_provider(
    tmp_path: Path,
) -> None:
    _, _, _, providers, prepared_path, plan_path = _preflight_fixture(tmp_path)
    output_path = tmp_path / "blocked.mp4"
    report_path = tmp_path / "blocked-report.json"

    result = _run_cli(
        prepared_path=prepared_path,
        plan_path=plan_path,
        providers=providers,
        output_path=output_path,
        report_path=report_path,
        max_cost_cny="1.0",
        credentials=True,
    )

    assert result.returncode == 6
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert report["status"] == "blocked"
    assert report["budget_gate"] == "blocked_before_provider_submission"
    assert report["credentials_present"] is True
    assert report["external_api_calls"] == 0
    assert report["committed_api_calls"] == 0
    assert report["committed_cost_cny"] == "0"
    assert not output_path.exists()


def test_delegate_failure_still_writes_enriched_report(tmp_path: Path) -> None:
    _, plan, segment, providers, prepared_path, plan_path = _preflight_fixture(tmp_path)
    output_path = tmp_path / "failed.mp4"
    report_path = tmp_path / "failed-report.json"

    result = _run_cli(
        prepared_path=prepared_path,
        plan_path=plan_path,
        providers=providers,
        output_path=output_path,
        report_path=report_path,
        max_cost_cny="10.0",
        stage="keyframe",
    )

    assert result.returncode == 6
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["segment_id"] == segment.segment_id
    assert report["generation_plan_digest"] == plan.content_digest
    assert report["delegate_exit_code"] == 6
    assert report["credentials_present"] is False
    assert report["errors"]
    assert not output_path.exists()
