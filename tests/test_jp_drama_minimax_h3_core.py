from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.apps.jp_drama.rendering.minimax_h3_adapter import (
    H3_PRODUCTION_ROUTE_PRIORITY,
    MiniMaxH3Adapter,
)
from src.apps.jp_drama.rendering.minimax_h3_client import (
    MiniMaxH3Client,
    MiniMaxH3ClientError,
)
from src.apps.jp_drama.rendering.minimax_h3_config import MiniMaxH3ProviderConfig
from src.apps.jp_drama.rendering.minimax_h3_executor import (
    MiniMaxH3ExecutionError,
    MiniMaxH3Executor,
)
from src.apps.jp_drama.rendering.minimax_h3_models import (
    H3ContentItem,
    H3ReferenceAsset,
    H3ReferenceBundle,
    H3VideoGenerationRequest,
)
from src.apps.jp_drama.rendering.provider_core import (
    ProviderCapabilitiesRequired,
    ReferenceAsset,
    ShotGenerationSpec,
)
from src.apps.jp_drama.rendering.provider_execution_ledger import H3ExecutionLedgerStore


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.query_count = 0

    def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append((method, url))
        if method == "POST":
            return 200, {"x-request-id": "req-1"}, b'{"task_id":"task-1"}'
        if "query/video_generation" in url:
            self.query_count += 1
            if self.query_count == 1:
                return 200, {}, b'{"task":{"task_id":"task-1","status":"queued"}}'
            return (
                200,
                {},
                b'{"task":{"task_id":"task-1","status":"succeeded",'
                b'"content":{"url":"https://cdn.example/video.mp4"}},'
                b'"usage":{"seconds":8}}',
            )
        return 200, {"content-type": "video/mp4"}, b"\x00\x00\x00\x18ftypmp42fake-video"


class UnknownSubmissionTransport:
    def request(self, method, url, *, headers, body, timeout_seconds):
        raise MiniMaxH3ClientError(
            "connection closed after POST",
            retryable=True,
            submission_may_have_succeeded=method == "POST",
        )


def _reference_spec() -> ShotGenerationSpec:
    return ShotGenerationSpec(
        source_digest="sha256:" + "a" * 64,
        shot_id="shot_01",
        task_id="segment_001:generate",
        modality="video",
        duration_seconds=8,
        resolution="768P",
        prompt="A Japanese short-drama confrontation, stable identity and vertical framing.",
        references=[
            ReferenceAsset(
                asset_id="character_01",
                uri="https://assets.example/character.png",
                sha256="sha256:" + "b" * 64,
                role="character",
                order=0,
            ),
            ReferenceAsset(
                asset_id="dialogue_01",
                uri="https://assets.example/dialogue.wav",
                sha256="sha256:" + "c" * 64,
                role="reference_audio",
                order=1,
            ),
        ],
        audio_strategy="native_av",
        required_capabilities=ProviderCapabilitiesRequired(
            modality="video",
            text_to_video=True,
            reference_to_video=True,
            native_audio=True,
            driving_audio=True,
            multi_shot=True,
        ),
    )


def test_h3_config_uses_v2_paths_and_price_snapshot() -> None:
    config = MiniMaxH3ProviderConfig()
    assert config.submit_url.endswith("/v2/video_generation")
    assert config.query_url("task-1").endswith("/v2/query/video_generation/task-1")
    assert config.estimate_cost_usd(duration_seconds=8) == Decimal("0.64")
    assert config.estimate_cost_usd(
        duration_seconds=8,
        reference_image_count=7,
        reference_video_seconds=2,
    ) == Decimal("0.88")


def test_h3_request_modes_cannot_be_mixed() -> None:
    with pytest.raises(ValidationError, match="cannot be mixed"):
        H3VideoGenerationRequest(
            content=[
                H3ContentItem.text_item("test"),
                H3ContentItem.media_item(
                    "image_url", "https://assets.example/start.png", "first_frame"
                ),
                H3ContentItem.media_item(
                    "audio_url", "https://assets.example/voice.wav", "reference_audio"
                ),
            ],
            resolution="768P",
            duration=8,
            ratio="adaptive",
        )


def test_h3_reference_bundle_enforces_limits() -> None:
    bundle = H3ReferenceBundle(
        segment_id="segment_001",
        assets=[
            H3ReferenceAsset(
                asset_id="video_01",
                kind="video",
                url="https://assets.example/tail.mp4",
                role="reference_video",
                priority=2,
                size_bytes=1_000,
                duration_seconds=16,
                fps=30,
                aspect_ratio=9 / 16,
            )
        ],
    )
    errors = bundle.preflight_errors()
    assert "reference video duration exceeds 15 seconds" in errors
    assert "video_01 video duration is outside 2-15 seconds" in errors


def test_h3_reference_adapter_builds_reference_av_request() -> None:
    adapter = MiniMaxH3Adapter(
        MiniMaxH3ProviderConfig(), route_id="minimax/h3-reference-av"
    )
    spec = _reference_spec()
    assert adapter.validate(spec).valid is True
    prepared = adapter.prepare(spec)
    assert prepared.route_id == "minimax/h3-reference-av"
    assert prepared.payload["resolution"] == "768P"
    assert prepared.payload["duration"] == 8
    assert [item.get("role") for item in prepared.payload["content"][1:]] == [
        "reference_image",
        "reference_audio",
    ]
    estimate = adapter.estimate_cost(spec)
    assert estimate.native_cost is not None
    assert estimate.native_cost.amount == Decimal("0.64")


def test_h3_client_and_executor_resume_without_duplicate_post(tmp_path: Path) -> None:
    config = MiniMaxH3ProviderConfig(poll_interval_seconds=0.001)
    transport = FakeTransport()
    client = MiniMaxH3Client(config, transport=transport, api_key="test-key")
    adapter = MiniMaxH3Adapter(
        config, route_id="minimax/h3-reference-av", client=client
    )
    prepared = adapter.prepare(_reference_spec())
    executor = MiniMaxH3Executor(
        config,
        client,
        H3ExecutionLedgerStore(tmp_path / "ledger.json"),
        sleep=lambda _: None,
    )

    output = executor.execute(
        prepared,
        segment_id="segment_001",
        estimated_cost_usd=Decimal("0.64"),
        output_path=tmp_path / "segment.mp4",
    )
    assert output.exists()
    assert [method for method, _ in transport.calls].count("POST") == 1

    second = executor.execute(
        prepared,
        segment_id="segment_001",
        estimated_cost_usd=Decimal("0.64"),
        output_path=tmp_path / "segment.mp4",
        resume_only=True,
    )
    assert second == output
    assert [method for method, _ in transport.calls].count("POST") == 1


def test_submission_unknown_blocks_a_second_post(tmp_path: Path) -> None:
    config = MiniMaxH3ProviderConfig()
    client = MiniMaxH3Client(
        config, transport=UnknownSubmissionTransport(), api_key="test-key"
    )
    adapter = MiniMaxH3Adapter(
        config, route_id="minimax/h3-reference-av", client=client
    )
    prepared = adapter.prepare(_reference_spec())
    executor = MiniMaxH3Executor(
        config,
        client,
        H3ExecutionLedgerStore(tmp_path / "ledger.json"),
        sleep=lambda _: None,
    )

    with pytest.raises(MiniMaxH3ExecutionError, match="connection closed"):
        executor.execute(
            prepared,
            segment_id="segment_001",
            estimated_cost_usd=Decimal("0.64"),
            output_path=tmp_path / "segment.mp4",
        )
    with pytest.raises(MiniMaxH3ExecutionError, match="refusing a second POST"):
        executor.execute(
            prepared,
            segment_id="segment_001",
            estimated_cost_usd=Decimal("0.64"),
            output_path=tmp_path / "segment.mp4",
        )


def test_h3_is_the_declared_production_priority() -> None:
    assert H3_PRODUCTION_ROUTE_PRIORITY[:3] == [
        "minimax/h3-reference-av",
        "minimax/h3-first-frame",
        "minimax/h3-text",
    ]
    assert H3_PRODUCTION_ROUTE_PRIORITY[3] == "wan/i2v"
