from decimal import Decimal
from pathlib import Path

import pytest

from src.apps.jp_drama.rendering.minimax_h3_config import MiniMaxH3ProviderConfig
from src.apps.jp_drama.workflows.reuse_happyhorse_segment_for_h3 import (
    EXPECTED_SEGMENT_ID,
    H3ContinuationHandoff,
    H3_CN_CONFIG,
    H3_ROUTE,
)


ROOT = Path(__file__).resolve().parents[1]
PLAN_DIGEST = "sha256:" + "a" * 64
APPROVAL_DIGEST = "sha256:" + "b" * 64


def test_china_h3_profile_is_separate_and_charge_safe() -> None:
    config_path = ROOT / H3_CN_CONFIG
    config = MiniMaxH3ProviderConfig.load(config_path)

    assert config.base_url == "https://api.minimaxi.com"
    assert config.submit_url == "https://api.minimaxi.com/v2/video_generation"
    assert config.query_url("task-1") == (
        "https://api.minimaxi.com/v2/query/video_generation/task-1"
    )
    assert config.resolution == "768P"
    assert config.free_image_count == 5
    assert config.estimate_cost_usd(
        duration_seconds=10,
        reference_image_count=5,
        reference_video_seconds=0,
    ) == Decimal("0.80")

    international = MiniMaxH3ProviderConfig.load(
        ROOT / "examples/jp_drama/minimax_h3_live_provider.json"
    )
    assert international.base_url == "https://api.minimax.io"


def test_handoff_never_resubmits_the_existing_first_segment() -> None:
    handoff = H3ContinuationHandoff.build(
        generation_plan_digest=PLAN_DIGEST,
        reused_segment_artifact="output/E01-G01.segment_artifact.json",
        reuse_approval_digest=APPROVAL_DIGEST,
        remaining_segment_ids=["E01-G02", "E01-G03"],
        external_api_calls=0,
    )

    assert handoff.reused_segment_id == EXPECTED_SEGMENT_ID
    assert EXPECTED_SEGMENT_ID not in handoff.remaining_segment_ids
    assert handoff.target_provider_route_id == H3_ROUTE
    assert handoff.provider_config == H3_CN_CONFIG
    assert handoff.external_api_calls == 0
    assert handoff.content_digest == handoff.compute_content_digest()


def test_handoff_rejects_duplicate_or_reused_segment_ids() -> None:
    with pytest.raises(ValueError, match="must not be sent to H3"):
        H3ContinuationHandoff.build(
            generation_plan_digest=PLAN_DIGEST,
            reused_segment_artifact="output/E01-G01.segment_artifact.json",
            reuse_approval_digest=APPROVAL_DIGEST,
            remaining_segment_ids=[EXPECTED_SEGMENT_ID],
            external_api_calls=0,
        )

    with pytest.raises(ValueError, match="must be unique"):
        H3ContinuationHandoff.build(
            generation_plan_digest=PLAN_DIGEST,
            reused_segment_artifact="output/E01-G01.segment_artifact.json",
            reuse_approval_digest=APPROVAL_DIGEST,
            remaining_segment_ids=["E01-G02", "E01-G02"],
            external_api_calls=0,
        )
