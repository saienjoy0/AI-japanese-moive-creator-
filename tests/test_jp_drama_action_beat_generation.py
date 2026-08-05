from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.apps.jp_drama.generation import (
    ProviderSegmentationProfile,
    compile_generation_plan,
)
from src.apps.jp_drama.ingestion import FixtureStructuredScriptLLM, ingest_script
from src.apps.jp_drama.preparation import compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering.provider_config import LiveProviderConfig
from src.apps.jp_drama.rendering.provider_registry import build_default_provider_registry


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
FIXED_TIME = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


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
    config_payload = json.loads(PROVIDERS.read_text(encoding="utf-8"))
    config_payload["dashscope"]["provider_clip_seconds"] = 15
    config = LiveProviderConfig.model_validate(config_payload)
    plan = compile_generation_plan(
        prepared,
        profile=ProviderSegmentationProfile.load(PROFILE),
        registry=build_default_provider_registry(config),
    )
    return ingestion, prepared, plan


def test_action_beats_remove_fixed_time_segmentation_blockers() -> None:
    ingestion, prepared, plan = _compile()

    assert len(ingestion.structured_script.beats) == 3
    assert sum(
        len(beat.action_beats) for beat in ingestion.structured_script.beats
    ) == 9
    assert len(prepared.storyboard_frame_drafts) == 9
    assert prepared.project_draft.target_duration_seconds == 45
    assert sum(
        frame.duration_seconds for frame in prepared.storyboard_frame_drafts
    ) == 45

    error_codes = {item.code for item in plan.readiness_report.errors}
    assert "insufficient_segmentation_evidence" not in error_codes
    assert "route_multi_shot_not_migrated" not in error_codes
    assert "capability_mismatch" not in error_codes
    assert plan.readiness_report.planning_ready is True
    assert plan.readiness_report.execution_route_ready is True


def test_wan_plan_contains_only_single_editorial_shot_units() -> None:
    _, prepared, plan = _compile()

    assert plan.target_frame_count == 45 * 30
    assert sum(item.editorial_frame_count for item in plan.segments) == 45 * 30
    assert all(len(item.editorial_shots) == 1 for item in plan.segments)
    assert all(item.provider_route_id == "wan/i2v" for item in plan.segments)
    assert all(2 <= item.requested_duration_seconds <= 15 for item in plan.segments)
    assert all(
        item.used_end_frame <= item.requested_duration_seconds * item.timeline_fps
        for item in plan.segments
    )

    source_dialogue = sorted(
        cue.text
        for frame in prepared.storyboard_frame_drafts
        for cue in frame.dialogue_cues
    )
    assigned_dialogue = sorted(
        dialogue.text
        for segment in plan.segments
        for dialogue in segment.dialogue_slices
    )
    assert assigned_dialogue == source_dialogue


def test_action_beat_generation_plan_is_byte_deterministic() -> None:
    _, _, first = _compile()
    _, _, second = _compile()

    assert first.content_digest == second.content_digest
    assert first.to_canonical_json(indent=None) == second.to_canonical_json(indent=None)
    assert [item.segment_id for item in first.segments] == [
        item.segment_id for item in second.segments
    ]
