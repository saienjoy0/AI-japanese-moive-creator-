from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.apps.jp_drama.assets.models import (
    ApprovedAssetBundle,
    ApprovedReferenceAsset,
)
from src.apps.jp_drama.assets.reference_resolution import (
    ReferenceResolutionError,
    build_reference_selection_manifest,
)
from src.apps.jp_drama.generation.happyhorse_r2v import (
    HAPPYHORSE_R2V_ROUTE_ID,
    derive_happyhorse_r2v_plan_and_bundle,
)
from src.apps.jp_drama.generation.models import (
    ContinuityContract,
    EditorialShot,
    GenerationCostPlan,
    GenerationPlanEpisode,
    GenerationReadinessReport,
    GenerationRenderGraph,
    GenerationSegment,
    GenerationTaskNode,
    PromptBundle,
    ReferenceAssetRequirement,
    SegmentComplexity,
)
from src.apps.jp_drama.production.reference_prompt import (
    CreativeOverride,
    TimelineSection,
    build_reference_prompt,
)
from src.apps.jp_drama.rendering.happyhorse_r2v_contract import (
    HappyHorseR2VApprovalManifest,
)
from src.apps.jp_drama.rendering.provider_ledger import ProviderOperationRecord
from src.apps.jp_drama.workflows.render_happyhorse_r2v_segment_canary import (
    _task_expired,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z1ZkAAAAASUVORK5CYII="
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _asset(path: Path, asset_id: str, subject_id: str, role: str):
    return ApprovedReferenceAsset(
        asset_id=asset_id,
        role=role,
        subject_id=subject_id,
        continuity_group_id="continuity-1",
        required_for_segment_ids=["E01-G01"],
        approval_status="approved",
        asset_path=str(path),
        asset_sha256=_sha(path),
        mime_type="image/png",
        width=1,
        height=1,
        generated_by="test",
        operation_id=f"test:{asset_id}",
        approved_at=datetime.now(timezone.utc),
        approved_by="test",
    )


def _source_contract(tmp_path: Path):
    refs = [
        ("ref_char_C01_x", "C01", "character_master"),
        ("ref_loc_S01_x", "S01", "location_master"),
        ("ref_prop_P03_x", "P03", "prop_master"),
        ("ref_prop_P04_x", "P04", "prop_master"),
        ("ref_loc_S05_x", "S05", "location_master"),
    ]
    assets = []
    requirements = []
    for index, (asset_id, subject_id, role) in enumerate(refs):
        path = tmp_path / f"{index}.png"
        path.write_bytes(PNG_1X1 + bytes([index]))
        assets.append(_asset(path, asset_id, subject_id, role))
        requirements.append(
            ReferenceAssetRequirement(
                asset_id=asset_id,
                role={
                    "character_master": "character",
                    "location_master": "location",
                    "prop_master": "prop",
                }[role],
                subject_id=subject_id,
                continuity_group_id="continuity-1",
                required_for_segment_ids=["E01-G01"],
                generation_status="available",
            )
        )

    complexity = SegmentComplexity(
        score=4,
        level="medium",
        character_complexity=1,
        action_complexity=1,
        dialogue_complexity=1,
        camera_complexity=1,
        continuity_complexity=0,
        object_interaction_complexity=0,
        reasons=["fixture"],
    )
    shot = EditorialShot(
        editorial_shot_id="editorial_E01-G01",
        segment_id="E01-G01",
        order_within_segment=1,
        start_frame=0,
        end_frame=240,
        framing="medium close-up",
        camera_movement="slow push-in",
        visual_action="少年が絵具を混ぜ、港を思い出して隣を見る",
        emotion="frustrated",
        character_ids=["C01"],
        source_beat_ids=["beat-1"],
        source_shot_ids=["E01-G01"],
    )
    segment = GenerationSegment(
        segment_id="E01-G01",
        order=1,
        parent_shot_ids=["E01-G01"],
        continuity_group_id="continuity-1",
        provider_route_id="minimax/h3-reference-av",
        timeline_fps=24,
        editorial_start_frame=0,
        editorial_end_frame=240,
        editorial_frame_count=240,
        editorial_duration_seconds=Decimal("10"),
        requested_duration_seconds=10,
        used_start_frame=0,
        used_end_frame=240,
        complexity=complexity,
        editorial_shots=[shot],
        character_ids=["C01"],
        location_id="S01",
        prop_ids=["P03", "P04"],
        dialogue_slices=[],
        reference_asset_ids=[item[0] for item in refs],
        prompt_bundle=PromptBundle(
            narrative_summary="色の足りない海",
            visual_prompt="明治期の教室と横浜港の記憶",
            motion_prompt="少年が絵具を混ぜて落胆する",
            camera_prompt="slow push-in",
            timed_shot_prompt="0-10秒",
            dialogue_prompt="海は、こんな色じゃない",
            audio_prompt="静かな教室と遠い汽笛",
            negative_constraints=["modern objects"],
        ),
        audio_strategy="native_av",
    )
    plan = GenerationPlanEpisode.build_with_digest(
        generation_plan_episode_id="source-plan",
        source_episode_id="episode-1",
        source_prepared_episode_digest="sha256:" + "a" * 64,
        policy_digest="sha256:" + "b" * 64,
        provider_profile_id="h3",
        provider_route_id="minimax/h3-reference-av",
        timeline_fps=24,
        target_frame_count=240,
        target_duration_seconds=Decimal("10"),
        segments=[segment],
        continuity_contracts=[
            ContinuityContract(
                continuity_group_id="continuity-1",
                character_appearance_locks={"C01": "same boy"},
                location_id="S01",
                location_lock="same classroom",
                prop_state_locks={"P03": "unfinished", "P04": "same box"},
                reference_asset_ids=[item[0] for item in refs],
            )
        ],
        reference_asset_requirements=requirements,
        render_graph=GenerationRenderGraph(
            nodes=[
                GenerationTaskNode(
                    task_id="task-source",
                    segment_id="E01-G01",
                    task_type="generate_native_av",
                    external_api_required=True,
                    provider_route_id="minimax/h3-reference-av",
                )
            ]
        ),
        cost_plan=GenerationCostPlan(
            reference_image_calls=0,
            video_calls=1,
            tts_calls=0,
            native_audio_calls=1,
            expected_calls=1,
            hard_maximum_calls=1,
        ),
        readiness_report=GenerationReadinessReport(
            planning_ready=True,
            execution_route_ready=True,
        ),
    )
    bundle = ApprovedAssetBundle.build_with_digest(
        bundle_id="source-bundle",
        source_episode_id="episode-1",
        source_prepared_episode_digest=plan.source_prepared_episode_digest,
        generation_plan_digest=plan.content_digest,
        assets=assets,
        voice_profiles=[],
    )
    return plan, bundle


def test_derived_plan_keeps_s05_and_removes_first_frame_dependency(tmp_path) -> None:
    source_plan, source_bundle = _source_contract(tmp_path)
    plan, bundle = derive_happyhorse_r2v_plan_and_bundle(
        source_plan,
        source_bundle,
        audio_strategy="native_audio",
        price_snapshot_id="happyhorse-2026-08-06",
        quoted_cost_cny_per_segment=Decimal("6"),
    )
    segment = plan.segments[0]
    assert plan.provider_route_id == HAPPYHORSE_R2V_ROUTE_ID
    assert segment.provider_route_id == HAPPYHORSE_R2V_ROUTE_ID
    assert segment.audio_strategy == "native_av"
    assert "ref_loc_S05_x" in segment.reference_asset_ids
    assert all(item.role != "first_frame" for item in bundle.assets)
    assert bundle.generation_plan_digest == plan.content_digest


def test_reference_selection_preserves_generation_plan_order(tmp_path) -> None:
    source_plan, source_bundle = _source_contract(tmp_path)
    plan, bundle = derive_happyhorse_r2v_plan_and_bundle(
        source_plan,
        source_bundle,
        audio_strategy="native_audio",
        price_snapshot_id="happyhorse-2026-08-06",
        quoted_cost_cny_per_segment=Decimal("6"),
    )
    selection = build_reference_selection_manifest(
        plan,
        bundle,
        segment_id="E01-G01",
        audio_strategy="native_audio",
    )
    assert [item.subject_id for item in selection.images] == [
        "C01",
        "S01",
        "P03",
        "P04",
        "S05",
    ]
    assert "provider_url" not in selection.to_canonical_json()


def test_reference_selection_rejects_duplicate_bytes(tmp_path) -> None:
    source_plan, source_bundle = _source_contract(tmp_path)
    duplicated = source_bundle.assets[1].model_copy(
        update={
            "asset_path": source_bundle.assets[0].asset_path,
            "asset_sha256": source_bundle.assets[0].asset_sha256,
            "width": source_bundle.assets[0].width,
            "height": source_bundle.assets[0].height,
        }
    )
    source_bundle = ApprovedAssetBundle.build_with_digest(
        bundle_id=source_bundle.bundle_id,
        source_episode_id=source_bundle.source_episode_id,
        source_prepared_episode_digest=source_bundle.source_prepared_episode_digest,
        generation_plan_digest=source_bundle.generation_plan_digest,
        assets=[source_bundle.assets[0], duplicated, *source_bundle.assets[2:]],
        voice_profiles=[],
    )
    plan, bundle = derive_happyhorse_r2v_plan_and_bundle(
        source_plan,
        source_bundle,
        audio_strategy="native_audio",
        price_snapshot_id="happyhorse-2026-08-06",
        quoted_cost_cny_per_segment=Decimal("6"),
    )
    with pytest.raises(ReferenceResolutionError, match="same approved bytes"):
        build_reference_selection_manifest(
            plan,
            bundle,
            segment_id="E01-G01",
        )


def test_prompt_binds_every_media_position_and_uses_override(tmp_path) -> None:
    source_plan, source_bundle = _source_contract(tmp_path)
    plan, bundle = derive_happyhorse_r2v_plan_and_bundle(
        source_plan,
        source_bundle,
        audio_strategy="native_audio",
        price_snapshot_id="happyhorse-2026-08-06",
        quoted_cost_cny_per_segment=Decimal("6"),
    )
    selection = build_reference_selection_manifest(
        plan,
        bundle,
        segment_id="E01-G01",
    )
    override = CreativeOverride(
        segment_id="E01-G01",
        timeline=[
            TimelineSection(start_seconds=0, end_seconds=2, action="unfinished painting"),
            TimelineSection(start_seconds=2, end_seconds=4, action="harbor memory"),
            TimelineSection(start_seconds=4, end_seconds=6.5, action="muddy paint"),
            TimelineSection(start_seconds=6.5, end_seconds=10, action="looks right"),
        ],
    )
    prompt = build_reference_prompt(
        plan.segments[0],
        selection,
        audio_strategy="native_audio",
        creative_override=override,
    )
    for number in range(1, 6):
        assert f"[Image {number}]" in prompt.prompt
    assert "2.0-4.0s: harbor memory" in prompt.prompt
    assert len(prompt.prompt) <= 2500


def test_approval_digest_binds_ratio_endpoint_assets_and_price(tmp_path) -> None:
    source_plan, source_bundle = _source_contract(tmp_path)
    plan, bundle = derive_happyhorse_r2v_plan_and_bundle(
        source_plan,
        source_bundle,
        audio_strategy="native_audio",
        price_snapshot_id="happyhorse-2026-08-06",
        quoted_cost_cny_per_segment=Decimal("6"),
    )
    selection = build_reference_selection_manifest(
        plan,
        bundle,
        segment_id="E01-G01",
    )
    prompt = build_reference_prompt(
        plan.segments[0],
        selection,
        audio_strategy="native_audio",
    )
    manifest = HappyHorseR2VApprovalManifest.build_with_digest(
        segment_id="E01-G01",
        generation_plan_digest=plan.content_digest,
        asset_bundle_digest=bundle.content_digest,
        reference_selection_digest=selection.content_digest,
        prompt_bundle_digest=prompt.content_digest,
        prompt_sha256=prompt.prompt_sha256,
        ordered_asset_ids=[item.asset_id for item in selection.images],
        ordered_asset_sha256=[item.local_sha256 for item in selection.images],
        deployment_region="singapore",
        endpoint_origin_hash="sha256:" + "c" * 64,
        workspace_id_hash="sha256:" + "d" * 64,
        resolution="720P",
        ratio="9:16",
        duration=10,
        watermark=False,
        seed=1,
        audio_strategy="native_audio",
        price_snapshot_id="happyhorse-2026-08-06",
        quoted_cost_cny=Decimal("6"),
        max_api_calls=1,
    )
    changed = HappyHorseR2VApprovalManifest.build_with_digest(
        **{
            **manifest.model_dump(exclude={"content_digest"}),
            "endpoint_origin_hash": "sha256:" + "e" * 64,
        }
    )
    assert manifest.content_digest != changed.content_digest


def test_task_expiry_guard_blocks_old_non_succeeded_task() -> None:
    now = datetime.now(timezone.utc)
    record = ProviderOperationRecord(
        operation_id="op",
        stage="render",
        shot_id="E01-G01",
        operation_type="video",
        provider="dashscope",
        model="happyhorse-1.1-r2v",
        status="submitted",
        provider_task_id="task",
        submitted_at=now - timedelta(hours=23, minutes=1),
    )
    assert _task_expired(record, now=now) is True
