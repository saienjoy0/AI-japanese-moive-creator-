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
)
from src.apps.jp_drama.preparation import PreparedEpisode, compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering.provider_config import LiveProviderConfig
from src.apps.jp_drama.rendering.provider_registry import build_default_provider_registry
from src.apps.jp_drama.rendering.segment_canary import (
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


def _plan(prepared: PreparedEpisode, config: LiveProviderConfig):
    return compile_generation_plan(
        prepared,
        profile=ProviderSegmentationProfile.load(PROFILE_PATH),
        registry=build_default_provider_registry(config),
    )


def _preflight_fixture(tmp_path: Path):
    prepared = _prepared()
    provider_payload = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    provider_payload["dashscope"]["provider_clip_seconds"] = 15
    providers = tmp_path / "providers.json"
    providers.write_text(
        json.dumps(provider_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    config = LiveProviderConfig.model_validate(provider_payload)
    plan = _plan(prepared, config)
    segment = plan.segments[0]
    prepared_path = tmp_path / "prepared.json"
    plan_path = tmp_path / "plan.json"
    prepared_path.write_text(prepared.to_canonical_json() + "\n", encoding="utf-8")
    plan_path.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")
    return prepared, plan, segment, providers, prepared_path, plan_path


def _run_preflight(
    *,
    prepared_path: Path,
    plan_path: Path,
    segment_id: str,
    providers: Path,
    output_path: Path,
    report_path: Path,
    max_cost_cny: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("DASHSCOPE_API_KEY", None)
    environment.pop("DASHSCOPE_BASE_URL", None)
    environment.pop("DASHSCOPE_WORKSPACE_ID", None)
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
            "preflight",
            "--allow-experimental-multi-shot",
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


def test_materialized_canary_preserves_segment_prompt_timing_and_identity() -> None:
    prepared = _prepared()
    config = LiveProviderConfig.load(PROVIDERS_PATH)
    plan = _plan(prepared, config)
    segment = next(item for item in plan.segments if item.dialogue_slices)

    materialized = materialize_generation_segment_canary(
        prepared,
        plan,
        segment.segment_id,
        allow_experimental_multi_shot=True,
    )

    frame = materialized.storyboard_frame_drafts[0]
    assert frame.source_shot_id == segment.segment_id
    assert frame.duration_seconds == segment.requested_duration_seconds
    assert frame.visual_description == segment.prompt_bundle.visual_prompt
    assert segment.prompt_bundle.timed_shot_prompt in frame.action
    assert materialized.source_digest != prepared.source_digest
    assert materialized.project_draft.target_duration_seconds == segment.requested_duration_seconds
    assert materialized.render_intents[0].shot_id == segment.segment_id
    assert {node.shot_id for node in materialized.render_graph.nodes} == {segment.segment_id}
    assert all(segment.segment_id in node.task_id for node in materialized.render_graph.nodes)

    expected_offset = segment.used_start_frame / segment.timeline_fps
    assert frame.dialogue_cues
    assert frame.dialogue_cues[0].start_seconds == pytest.approx(
        expected_offset
        + segment.dialogue_slices[0].start_frame / segment.timeline_fps
    )
    assert PreparedEpisode.model_validate_json(materialized.to_canonical_json())


def test_contract_rejects_wrong_prepared_episode_digest() -> None:
    prepared = _prepared()
    config = LiveProviderConfig.load(PROVIDERS_PATH)
    plan = _plan(prepared, config)
    changed = prepared.model_copy(
        update={
            "project_draft": prepared.project_draft.model_copy(
                update={"title": prepared.project_draft.title + " changed"}
            )
        }
    )

    assert prepared_content_digest(changed) != plan.source_prepared_episode_digest
    with pytest.raises(SegmentCanaryError, match="does not belong"):
        validate_segment_canary_contract(
            changed,
            plan,
            plan.segments[0],
            allow_experimental_multi_shot=True,
        )


def test_contract_never_silently_trims_or_enables_multi_shot() -> None:
    prepared = _prepared()
    config = LiveProviderConfig.load(PROVIDERS_PATH)
    plan = _plan(prepared, config)
    multi = next(item for item in plan.segments if len(item.editorial_shots) > 1)

    with pytest.raises(SegmentCanaryError, match="multiple editorial shots"):
        validate_segment_canary_contract(prepared, plan, multi)

    with pytest.raises(SegmentCanaryError, match="refusing to trim"):
        validate_segment_canary_contract(
            prepared,
            plan,
            plan.segments[0],
            provider_clip_seconds=1,
            allow_experimental_multi_shot=True,
        )


def test_contract_rejects_invalid_provider_trim_window() -> None:
    prepared = _prepared()
    config = LiveProviderConfig.load(PROVIDERS_PATH)
    plan = _plan(prepared, config)
    segment = plan.segments[0]
    invalid = segment.model_copy(
        update={
            "used_start_frame": -1,
            "used_end_frame": segment.editorial_frame_count - 1,
        }
    )

    with pytest.raises(SegmentCanaryError, match="trim window is invalid"):
        validate_segment_canary_contract(
            prepared,
            plan,
            invalid,
            allow_experimental_multi_shot=True,
        )


def test_cli_preflight_uses_real_plan_and_makes_zero_paid_calls(tmp_path: Path) -> None:
    _, plan, segment, providers, prepared_path, plan_path = _preflight_fixture(tmp_path)
    output_path = tmp_path / "segment.mp4"
    report_path = tmp_path / "report.json"

    result = _run_preflight(
        prepared_path=prepared_path,
        plan_path=plan_path,
        segment_id=segment.segment_id,
        providers=providers,
        output_path=output_path,
        report_path=report_path,
        max_cost_cny="6.0",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["canary_protocol"] == "generation-segment-canary-v1"
    assert report["segment_id"] == segment.segment_id
    assert report["generation_plan_digest"] == plan.content_digest
    assert report["requested_duration_seconds"] == segment.requested_duration_seconds
    assert report["external_api_calls"] == 0
    assert report["credentials_present"] is False
    assert report["within_requested_call_limit"] is True
    assert report["within_requested_cost_limit"] is True
    assert Path(report["materialized_prepared_episode"]).is_file()
    assert not output_path.exists()


def test_cli_budget_gate_blocks_before_any_paid_submission(tmp_path: Path) -> None:
    _, _, segment, providers, prepared_path, plan_path = _preflight_fixture(tmp_path)
    output_path = tmp_path / "blocked.mp4"
    report_path = tmp_path / "blocked-report.json"

    result = _run_preflight(
        prepared_path=prepared_path,
        plan_path=plan_path,
        segment_id=segment.segment_id,
        providers=providers,
        output_path=output_path,
        report_path=report_path,
        max_cost_cny="5.0",
    )

    assert result.returncode == 6
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["valid"] is False
    assert report["budget_gate"] == "blocked_before_provider_submission"
    assert report["within_requested_cost_limit"] is False
    assert report["external_api_calls"] == 0
    assert report["planned_cost_cny"] == "5.059685886"
    assert not output_path.exists()
