from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.apps.jp_drama.assets import (
    ApprovedAssetBundle,
    build_pending_asset_bundle,
)
from src.apps.jp_drama.generation import (
    ExecutionBudgetError,
    ProviderSegmentationProfile,
    build_execution_budget,
    compile_generation_plan,
)
from src.apps.jp_drama.ingestion import FixtureStructuredScriptLLM, ingest_script
from src.apps.jp_drama.preparation import compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering.provider_config import LiveProviderConfig
from src.apps.jp_drama.rendering.provider_ledger import (
    CanaryProviderLedger,
    ProviderOperationRecord,
)
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
FIXED_TIME = datetime(2026, 8, 5, 4, 0, tzinfo=timezone.utc)


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
    provider_payload = json.loads(PROVIDERS.read_text(encoding="utf-8"))
    provider_payload["dashscope"]["provider_clip_seconds"] = 15
    config = LiveProviderConfig.model_validate(provider_payload)
    plan = compile_generation_plan(
        prepared,
        profile=ProviderSegmentationProfile.load(PROFILE),
        registry=build_default_provider_registry(config),
    )
    return prepared, plan, config


def _approved_bundle(prepared, plan) -> ApprovedAssetBundle:
    pending = build_pending_asset_bundle(prepared, plan)
    assets = []
    for asset in pending.assets:
        update = {
            "approval_status": "approved",
            "asset_path": f"/approved/{asset.asset_id}.png",
            "asset_sha256": "sha256:" + "1" * 64,
            "mime_type": "image/png",
            "width": 90 if asset.role == "first_frame" else 64,
            "height": 160 if asset.role == "first_frame" else 64,
            "approved_at": FIXED_TIME,
            "approved_by": "budget-test",
        }
        if asset.role == "first_frame":
            update["approval_manifest_path"] = f"/approved/{asset.asset_id}.json"
        assets.append(asset.model_copy(update=update))
    voices = [
        item.model_copy(
            update={
                "voice_id": f"voice-{index + 1}",
                "approval_status": "approved",
                "approved_at": FIXED_TIME,
                "approved_by": "budget-test",
            }
        )
        for index, item in enumerate(pending.voice_profiles)
    ]
    return ApprovedAssetBundle.build_with_digest(
        bundle_id=pending.bundle_id,
        source_episode_id=pending.source_episode_id,
        source_prepared_episode_digest=pending.source_prepared_episode_digest,
        generation_plan_digest=pending.generation_plan_digest,
        assets=assets,
        voice_profiles=voices,
    )


def test_without_approved_frames_budget_contains_all_33_calls() -> None:
    _, plan, config = _compile()
    budget = build_execution_budget(
        plan,
        config,
        hard_maximum_calls=40,
        hard_limit_cny=Decimal("100"),
    )

    assert budget.remaining_api_calls == 33
    assert budget.committed_api_calls == 0
    assert budget.total_exposure_api_calls == 33
    assert len([item for item in budget.operations if item.component == "first_frame"]) == 15
    assert len([item for item in budget.operations if item.component == "video"]) == 15
    assert len([item for item in budget.operations if item.component == "tts"]) == 3
    assert budget.unknown_components == []
    assert budget.payment_approved is True
    assert budget.remaining_cost_cny > Decimal("48.899655")


def test_approved_first_frames_reduce_remaining_calls_to_video_and_tts() -> None:
    prepared, plan, config = _compile()
    bundle = _approved_bundle(prepared, plan)
    budget = build_execution_budget(
        plan,
        config,
        asset_bundle=bundle,
        hard_maximum_calls=20,
        hard_limit_cny=Decimal("50"),
    )

    assert budget.remaining_api_calls == 18
    assert budget.total_exposure_api_calls == 18
    assert all(
        item.status == "satisfied_by_asset"
        for item in budget.operations
        if item.component == "first_frame"
    )
    tts = [item for item in budget.operations if item.component == "tts"]
    assert len(tts) == 3
    assert all(item.quantity > 0 for item in tts)
    assert all(item.estimated_cost_cny > 0 for item in tts)
    assert budget.unknown_components == []
    assert budget.payment_approved is True
    assert budget.total_exposure_cny < Decimal("50")


def test_low_hard_limit_blocks_payment_without_changing_quotes() -> None:
    prepared, plan, config = _compile()
    bundle = _approved_bundle(prepared, plan)
    budget = build_execution_budget(
        plan,
        config,
        asset_bundle=bundle,
        hard_maximum_calls=17,
        hard_limit_cny=Decimal("10"),
    )

    assert budget.remaining_api_calls == 18
    assert budget.within_call_limit is False
    assert budget.within_cost_limit is False
    assert budget.payment_approved is False


def test_committed_video_ledger_reduces_remaining_segment_exposure() -> None:
    prepared, plan, config = _compile()
    bundle = _approved_bundle(prepared, plan)
    segment = next(item for item in plan.segments if item.dialogue_slices)
    video_cost = config.dashscope.estimate_video_cost_cny(
        segment.requested_duration_seconds
    )
    ledger = CanaryProviderLedger(
        source_digest="sha256:" + "2" * 64,
        shot_id=segment.segment_id,
        max_api_calls=3,
        max_cost_cny=Decimal("20"),
        operations={
            "video-op": ProviderOperationRecord(
                operation_id="video-op",
                stage="render",
                shot_id=segment.segment_id,
                operation_type="video",
                provider="dashscope",
                model=config.dashscope.video_model,
                status="submitted",
                estimated_cost_cny=video_cost,
                provider_task_id="task-123",
            )
        },
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )

    budget = build_execution_budget(
        plan,
        config,
        asset_bundle=bundle,
        ledgers=[ledger],
        segment_ids=[segment.segment_id],
        hard_maximum_calls=2,
        hard_limit_cny=Decimal("20"),
    )

    video = next(item for item in budget.operations if item.component == "video")
    tts = next(item for item in budget.operations if item.component == "tts")
    frame = next(item for item in budget.operations if item.component == "first_frame")
    assert video.status == "committed"
    assert video.committed_api_calls == 1
    assert video.remaining_api_calls == 0
    assert tts.status == "planned"
    assert frame.status == "satisfied_by_asset"
    assert budget.committed_api_calls == 1
    assert budget.remaining_api_calls == 1
    assert budget.total_exposure_api_calls == 2
    assert budget.committed_cost_cny == video_cost
    assert budget.payment_approved is True


def test_duplicate_committed_component_is_rejected() -> None:
    prepared, plan, config = _compile()
    bundle = _approved_bundle(prepared, plan)
    segment = plan.segments[0]
    operations = {
        key: ProviderOperationRecord(
            operation_id=key,
            stage="render",
            shot_id=segment.segment_id,
            operation_type="video",
            provider="dashscope",
            model=config.dashscope.video_model,
            status="submitted",
            estimated_cost_cny=Decimal("1"),
            provider_task_id=f"task-{key}",
        )
        for key in ("video-a", "video-b")
    }
    ledger = CanaryProviderLedger(
        source_digest="sha256:" + "3" * 64,
        shot_id=segment.segment_id,
        max_api_calls=3,
        max_cost_cny=Decimal("20"),
        operations=operations,
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )

    with pytest.raises(ExecutionBudgetError, match="multiple committed video"):
        build_execution_budget(
            plan,
            config,
            asset_bundle=bundle,
            ledgers=[ledger],
            segment_ids=[segment.segment_id],
            hard_maximum_calls=3,
            hard_limit_cny=Decimal("20"),
        )


def test_same_inputs_create_byte_identical_execution_budget() -> None:
    prepared, plan, config = _compile()
    bundle = _approved_bundle(prepared, plan)
    first = build_execution_budget(
        plan,
        config,
        asset_bundle=bundle,
        hard_maximum_calls=20,
        hard_limit_cny=Decimal("50"),
    )
    second = build_execution_budget(
        plan,
        config,
        asset_bundle=bundle,
        hard_maximum_calls=20,
        hard_limit_cny=Decimal("50"),
    )

    assert first.content_digest == second.content_digest
    assert first.to_canonical_json(indent=None) == second.to_canonical_json(indent=None)
