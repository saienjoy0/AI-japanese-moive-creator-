from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.preparation import compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering import (
    CostEstimate,
    DialogueLine,
    LiveProviderConfig,
    MockProviderAdapter,
    ProviderCapabilitiesRequired,
    ProviderCoreError,
    ProviderExecutionPlanner,
    ProviderPlanningError,
    ProviderProfile,
    ProviderRegistry,
    ProviderRegistryError,
    ReferenceAsset,
    SeedancePlatformAdapter,
    ShotGenerationSpec,
    Wan27PlanningAdapter,
    build_default_provider_registry,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
PROVIDER_PATH = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"


def _prepared():
    payload = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    package = EpisodePackage.model_validate(payload)
    return compile_episode(
        package,
        catalog=load_model_catalog(CATALOG_PATH),
        strict=True,
        source_payload=payload,
    )


def _video_spec(
    *,
    audio_strategy: str = "native_av",
    duration_seconds: float = 10,
    references: list[ReferenceAsset] | None = None,
    text_to_video: bool = True,
    image_to_video: bool = False,
) -> ShotGenerationSpec:
    return ShotGenerationSpec(
        source_digest="sha256:" + ("a" * 64),
        shot_id="shot_01",
        task_id="shot_01:generate",
        modality="video",
        duration_seconds=duration_seconds,
        prompt="A Japanese short-drama confrontation in a vertical frame.",
        dialogue=[
            DialogueLine(
                cue_id="cue_01",
                speaker_character_id="character_01",
                text="どういうこと？",
                start_seconds=0.5,
                end_seconds=2.0,
            )
        ],
        references=references or [],
        audio_strategy=audio_strategy,
        required_capabilities=ProviderCapabilitiesRequired(
            modality="video",
            text_to_video=text_to_video,
            image_to_video=image_to_video,
            native_audio=audio_strategy == "native_av",
            driving_audio=audio_strategy == "driving_audio",
        ),
    )


def test_registry_is_explicit_and_rejects_duplicate_routes() -> None:
    registry = ProviderRegistry()
    registry.register(MockProviderAdapter())
    assert registry.route_ids() == ["mock/video"]
    assert registry.require("mock/video").descriptor().execution_mode == "automatic"

    with pytest.raises(ProviderRegistryError, match="already registered"):
        registry.register(MockProviderAdapter())
    with pytest.raises(ProviderRegistryError, match="unknown provider route"):
        registry.require("unknown/video")


def test_seedance_platform_is_manual_native_av_route() -> None:
    adapter = SeedancePlatformAdapter()
    spec = _video_spec()
    assert adapter.validate(spec).valid is True
    assert adapter.estimate_cost(spec).confidence == "unknown"

    prepared = adapter.prepare(spec)
    submission = adapter.submit(prepared)
    assert submission.status == "awaiting_operator"
    assert submission.route_id == "seedance/platform"
    assert adapter.poll(submission).status == "awaiting_operator"

    with pytest.raises(ProviderCoreError, match="imported by an operator"):
        adapter.download(adapter.poll(submission), ROOT / "output" / "unused")


def test_seedance_platform_enforces_official_reference_and_duration_limits() -> None:
    adapter = SeedancePlatformAdapter()
    too_long = _video_spec(duration_seconds=16)
    report = adapter.validate(too_long)
    assert report.valid is False
    assert {item.code for item in report.errors} == {"duration_not_supported"}

    references = [
        ReferenceAsset(
            asset_id=f"image_{index}",
            uri=f"assets/image_{index}.png",
            role="character",
            order=index,
        )
        for index in range(10)
    ]
    too_many_images = _video_spec(references=references)
    report = adapter.validate(too_many_images)
    assert report.valid is False
    assert "too_many_reference_images" in {item.code for item in report.errors}


def test_driving_audio_strategy_requires_a_driving_audio_reference() -> None:
    with pytest.raises(ValueError, match="requires a driving_audio reference"):
        _video_spec(audio_strategy="driving_audio")

    spec = _video_spec(
        audio_strategy="driving_audio",
        references=[
            ReferenceAsset(
                asset_id="voice_01",
                uri="voices/voice_01.wav",
                role="driving_audio",
                order=0,
            )
        ],
    )
    assert spec.audio_strategy == "driving_audio"


def test_wan_planning_adapter_reuses_pr8_costs_and_blocks_unmigrated_audio() -> None:
    config = LiveProviderConfig.load(PROVIDER_PATH)
    adapter = Wan27PlanningAdapter(config)

    native = _video_spec(text_to_video=False, image_to_video=True)
    report = adapter.validate(native)
    assert report.valid is False
    assert "wan_i2v_audio_strategy_not_migrated" in {
        item.code for item in report.errors
    }

    external = _video_spec(
        audio_strategy="external_audio_post",
        text_to_video=False,
        image_to_video=True,
    )
    report = adapter.validate(external)
    assert report.valid is True
    estimate = adapter.estimate_cost(external)
    assert estimate.native_cost is not None
    assert estimate.native_cost.currency == "CNY"
    assert estimate.native_cost.amount > 0

    request = adapter.prepare(external)
    assert request.route_id == "wan/i2v"
    assert request.payload["provider_options"]["model"] == "wan2.7-i2v"
    with pytest.raises(ProviderCoreError, match="delegated to Wan27LiveTaskExecutor"):
        adapter.submit(request)


def test_planner_compiles_prepared_episode_without_changing_it() -> None:
    prepared = _prepared()
    original = prepared.to_canonical_json()
    registry = ProviderRegistry()
    registry.register(MockProviderAdapter())
    planner = ProviderExecutionPlanner(registry)
    profile = ProviderProfile(
        profile_id="mock-pinned",
        routing_mode="pinned",
        route_priority=["mock/video"],
        max_cost_cny=0,
    )

    plan = planner.plan(prepared, profile)
    assert plan.source_digest == prepared.source_digest
    assert plan.profile_id == "mock-pinned"
    assert plan.estimated_total_cny == 0
    assert plan.tasks
    assert {item.route_id for item in plan.tasks.values()} == {"mock/video"}
    assert prepared.to_canonical_json() == original

    restored = type(plan).model_validate_json(plan.to_canonical_json())
    assert restored.execution_plan_digest == plan.execution_plan_digest
    with pytest.raises(ValidationError, match="frozen"):
        plan.profile_id = "changed"
    first_task = next(iter(plan.tasks.values()))
    with pytest.raises(ValidationError, match="frozen"):
        first_task.generation_spec.prompt = "changed"


def test_planner_rejects_a_budget_when_selected_cost_is_unknown() -> None:
    class UnknownCostAdapter(MockProviderAdapter):
        def estimate_cost(self, request: ShotGenerationSpec) -> CostEstimate:
            return CostEstimate(confidence="unknown")

    prepared = _prepared()
    registry = ProviderRegistry()
    registry.register(UnknownCostAdapter("mock/unknown"))
    planner = ProviderExecutionPlanner(registry)
    with pytest.raises(ProviderPlanningError, match="cannot enforce max_cost_cny"):
        planner.plan(
            prepared,
            ProviderProfile(
                profile_id="unknown-cost",
                routing_mode="pinned",
                route_priority=["mock/unknown"],
                max_cost_cny=1,
            ),
        )


def test_planner_requires_compatible_route_and_records_approved_fallback() -> None:
    prepared = _prepared()
    config = LiveProviderConfig.load(PROVIDER_PATH)
    registry = build_default_provider_registry(config)
    planner = ProviderExecutionPlanner(registry)

    with pytest.raises(ProviderPlanningError, match="no compatible provider route"):
        planner.plan(
            prepared,
            ProviderProfile(
                profile_id="wan-only",
                routing_mode="pinned",
                route_priority=["wan/i2v"],
            ),
        )

    registry.register(MockProviderAdapter("mock/fallback"))
    plan = planner.plan(
        prepared,
        ProviderProfile(
            profile_id="mock-fallback",
            routing_mode="ordered_fallback",
            route_priority=["mock/video", "mock/fallback"],
            fallback_requires_approval=True,
        ),
    )
    assert plan.tasks
    assert all(item.fallback_route_id == "mock/fallback" for item in plan.tasks.values())
    assert all(item.fallback_requires_approval for item in plan.tasks.values())
