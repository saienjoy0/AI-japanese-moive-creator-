from __future__ import annotations

import json
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
    ProviderCoreError,
    ReferenceAsset,
    ShotGenerationSpec,
)
from src.apps.jp_drama.rendering.provider_execution_ledger import (
    H3ExecutionLedgerStore,
    H3ExecutionRecord,
)


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
                return 200, {}, b'{"task":{"id":"task-1","status":"queued"}}'
            return (
                200,
                {},
                b'{"task":{"id":"task-1","status":"succeeded",'
                b'"content":{"url":"https://cdn.example/video.mp4"},'
                b'"usage":{"total_seconds":8,"input_seconds":0,"output_seconds":8}}}',
            )
        return 200, {"content-type": "video/mp4"}, b"\x00\x00\x00\x18ftypmp42fake-video"


class UnknownSubmissionTransport:
    def request(self, method, url, *, headers, body, timeout_seconds):
        raise MiniMaxH3ClientError(
            "connection closed after POST",
            retryable=True,
            submission_may_have_succeeded=method == "POST",
        )


class QueryAndDownloadRetryTransport:
    def __init__(self) -> None:
        self.post_count = 0
        self.query_count = 0
        self.download_count = 0

    def request(self, method, url, *, headers, body, timeout_seconds):
        if method == "POST":
            self.post_count += 1
            return 200, {}, b'{"task_id":"task-retry"}'
        if "query/video_generation" in url:
            self.query_count += 1
            if self.query_count <= 2:
                return (
                    429,
                    {},
                    b'{"type":"error","error":{"message":"rate limit"}}',
                )
            return (
                200,
                {},
                b'{"task":{"id":"task-retry","status":"succeeded",'
                b'"content":{"url":"https://cdn.example/retry.mp4"},'
                b'"usage":{"total_seconds":8}}}',
            )
        self.download_count += 1
        if self.download_count == 1:
            return 500, {}, b'{"error":{"message":"temporary download failure"}}'
        return 200, {}, b"\x00\x00\x00\x18ftypmp42fake-video"


def _reference_spec(*, with_video: bool = False) -> ShotGenerationSpec:
    references = [
        ReferenceAsset(
            asset_id="character_01",
            uri="https://assets.example/character.png",
            sha256="sha256:" + "b" * 64,
            role="character",
            order=0,
            size_bytes=1_000,
            aspect_ratio=9 / 16,
            width_px=720,
            height_px=1280,
            media_format="png",
        ),
        ReferenceAsset(
            asset_id="dialogue_01",
            uri="https://assets.example/dialogue.wav",
            sha256="sha256:" + "c" * 64,
            role="reference_audio",
            order=1,
            size_bytes=2_000,
            duration_seconds=2,
            media_format="wav",
        ),
    ]
    if with_video:
        references.append(
            ReferenceAsset(
                asset_id="tail_01",
                uri="https://assets.example/tail.mp4",
                sha256="sha256:" + "d" * 64,
                role="video_reference",
                order=2,
                size_bytes=3_000,
                duration_seconds=3,
                fps=30,
                aspect_ratio=9 / 16,
                width_px=720,
                height_px=1280,
                media_format="mp4",
                codec="h264",
            )
        )
    return ShotGenerationSpec(
        source_digest="sha256:" + "a" * 64,
        shot_id="segment_001",
        task_id="segment_001:generate",
        modality="video",
        duration_seconds=8,
        resolution="768P",
        prompt="A Japanese short-drama confrontation, stable identity and vertical framing.",
        references=references,
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


def _executor(tmp_path: Path, transport, **config_overrides):
    config = MiniMaxH3ProviderConfig(
        poll_interval_seconds=0.001,
        **config_overrides,
    )
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
    return config, client, prepared, executor


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


def test_official_last_frame_only_and_reference_adaptive_are_accepted() -> None:
    last_only = H3VideoGenerationRequest(
        content=[
            H3ContentItem.text_item("test"),
            H3ContentItem.media_item(
                "image_url", "https://assets.example/end.png", "last_frame"
            ),
        ],
        resolution="768P",
        duration=8,
        ratio="adaptive",
    )
    assert last_only.mode == "first_frame"

    reference_adaptive = H3VideoGenerationRequest(
        content=[
            H3ContentItem.text_item("test"),
            H3ContentItem.media_item(
                "image_url", "https://assets.example/ref.png", "reference_image"
            ),
        ],
        resolution="768P",
        duration=8,
        ratio="adaptive",
    )
    assert reference_adaptive.mode == "reference"

    omitted_role = H3VideoGenerationRequest(
        content=[
            H3ContentItem.text_item("test"),
            H3ContentItem.media_item(
                "image_url", "https://assets.example/start.png", None
            ),
        ],
        resolution="768P",
        duration=8,
        ratio="adaptive",
    )
    assert omitted_role.mode == "first_frame"


def test_h3_reference_bundle_enforces_limits_and_metadata() -> None:
    bundle = H3ReferenceBundle(
        segment_id="segment_001",
        assets=[
            H3ReferenceAsset(
                asset_id="video_01",
                kind="video",
                url="pending://tail.mp4",
                role="reference_video",
                priority=2,
                size_bytes=51 * 1024 * 1024,
                duration_seconds=16,
                fps=20,
                aspect_ratio=9 / 16,
                width_px=720,
                height_px=1280,
                media_format="avi",
            )
        ],
    )
    errors = bundle.preflight_errors()
    assert "reference video duration exceeds 15 seconds" in errors
    assert "video_01 video duration is outside 2-15 seconds" in errors
    assert "video_01 still uses pending://" in errors
    assert "video_01 is missing sha256" in errors
    assert "video_01 exceeds the video file-size limit" in errors
    assert "video_01 video format is unsupported" in errors


def test_h3_reference_adapter_builds_reference_av_request_and_video_cost() -> None:
    adapter = MiniMaxH3Adapter(
        MiniMaxH3ProviderConfig(resolution="2K"),
        route_id="minimax/h3-reference-av",
    )
    spec = _reference_spec(with_video=True).model_copy(update={"resolution": "2K"})
    assert adapter.validate(spec).valid is True
    prepared = adapter.prepare(spec)
    assert prepared.route_id == "minimax/h3-reference-av"
    assert prepared.payload["resolution"] == "2K"
    assert prepared.payload["duration"] == 8
    assert [item.get("role") for item in prepared.payload["content"][1:]] == [
        "reference_image",
        "reference_audio",
        "reference_video",
    ]
    assert prepared.metadata["h3_reference_bundle"]["assets"][2]["duration_seconds"] == 3
    estimate = adapter.estimate_cost(spec)
    assert estimate.native_cost is not None
    assert estimate.native_cost.amount == Decimal("1.43")


def test_adapter_direct_submit_cannot_bypass_executor() -> None:
    adapter = MiniMaxH3Adapter(
        MiniMaxH3ProviderConfig(), route_id="minimax/h3-reference-av"
    )
    prepared = adapter.prepare(_reference_spec())
    with pytest.raises(ProviderCoreError, match="must use MiniMaxH3Executor"):
        adapter.submit(prepared)


def test_h3_client_and_executor_resume_without_duplicate_post(tmp_path: Path) -> None:
    transport = FakeTransport()
    _, _, prepared, executor = _executor(tmp_path, transport)
    output = executor.execute(
        prepared,
        segment_id="segment_001",
        estimated_cost_usd=Decimal("0.64"),
        max_cost_usd=Decimal("1.00"),
        approval_verified=True,
        output_path=tmp_path / "segment.mp4",
    )
    assert output.exists()
    assert [method for method, _ in transport.calls].count("POST") == 1
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["actual_usage"]["total_seconds"] == 8

    second = executor.execute(
        prepared,
        segment_id="segment_001",
        estimated_cost_usd=Decimal("0.64"),
        max_cost_usd=Decimal("1.00"),
        approval_verified=True,
        output_path=tmp_path / "segment.mp4",
        resume_only=True,
    )
    assert second == output
    assert [method for method, _ in transport.calls].count("POST") == 1


def test_persisted_submitting_state_becomes_unknown_without_post(tmp_path: Path) -> None:
    transport = FakeTransport()
    _, _, prepared, executor = _executor(tmp_path, transport)
    h3_request = H3VideoGenerationRequest.model_validate(prepared.payload)
    record = H3ExecutionRecord(
        request_fingerprint=prepared.request_fingerprint,
        segment_id="segment_001",
        route_id=prepared.route_id,
        model=h3_request.model,
        status="submitting",
        resolution=h3_request.resolution,
        duration=h3_request.duration,
        ratio=h3_request.ratio,
        prompt_sha256="sha256:" + "e" * 64,
        reference_asset_hashes=[
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
        ],
        estimated_cost_usd=Decimal("0.64"),
        max_cost_usd=Decimal("1.00"),
        price_snapshot_id="minimax-h3-2026-08-05",
    )
    executor.ledger_store.write(record)

    with pytest.raises(MiniMaxH3ExecutionError, match="refusing a second POST"):
        executor.execute(
            prepared,
            segment_id="segment_001",
            estimated_cost_usd=Decimal("0.64"),
            max_cost_usd=Decimal("1.00"),
            approval_verified=True,
            output_path=tmp_path / "segment.mp4",
        )
    assert transport.calls == []
    saved = executor.ledger_store.load()
    assert saved is not None
    assert saved.status == "submission_unknown"


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
            max_cost_usd=Decimal("1.00"),
            approval_verified=True,
            output_path=tmp_path / "segment.mp4",
        )
    with pytest.raises(MiniMaxH3ExecutionError, match="refusing a second POST"):
        executor.execute(
            prepared,
            segment_id="segment_001",
            estimated_cost_usd=Decimal("0.64"),
            max_cost_usd=Decimal("1.00"),
            approval_verified=True,
            output_path=tmp_path / "segment.mp4",
        )


def test_budget_and_pending_asset_preflight_block_before_post(tmp_path: Path) -> None:
    transport = FakeTransport()
    _, _, prepared, executor = _executor(tmp_path, transport)
    with pytest.raises(MiniMaxH3ExecutionError, match="exceeds max_cost_usd"):
        executor.execute(
            prepared,
            segment_id="segment_001",
            estimated_cost_usd=Decimal("0.01"),
            max_cost_usd=Decimal("0.63"),
            approval_verified=True,
            output_path=tmp_path / "segment.mp4",
        )
    assert transport.calls == []

    payload = prepared.metadata["h3_reference_bundle"]
    payload["assets"][0]["url"] = "pending://character.png"
    unsafe = prepared.model_copy(
        update={"metadata": {"h3_reference_bundle": payload}}
    )
    with pytest.raises(MiniMaxH3ExecutionError, match="pending://"):
        executor.execute(
            unsafe,
            segment_id="segment_001",
            estimated_cost_usd=Decimal("0.64"),
            max_cost_usd=Decimal("1.00"),
            approval_verified=True,
            output_path=tmp_path / "segment.mp4",
        )
    assert transport.calls == []


def test_poll_and_download_retry_limits_are_used(tmp_path: Path) -> None:
    transport = QueryAndDownloadRetryTransport()
    _, _, prepared, executor = _executor(
        tmp_path,
        transport,
        max_poll_retries=2,
        max_download_retries=1,
    )
    output = executor.execute(
        prepared,
        segment_id="segment_001",
        estimated_cost_usd=Decimal("0.64"),
        max_cost_usd=Decimal("1.00"),
        approval_verified=True,
        output_path=tmp_path / "segment.mp4",
    )
    assert output.exists()
    assert transport.post_count == 1
    assert transport.query_count == 3
    assert transport.download_count == 2


def test_h3_is_the_declared_production_priority() -> None:
    assert H3_PRODUCTION_ROUTE_PRIORITY[:3] == [
        "minimax/h3-reference-av",
        "minimax/h3-first-frame",
        "minimax/h3-text",
    ]
    assert H3_PRODUCTION_ROUTE_PRIORITY[3] == "wan/i2v"
