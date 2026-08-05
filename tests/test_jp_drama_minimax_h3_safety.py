from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from src.apps.jp_drama.rendering.minimax_h3_adapter import MiniMaxH3Adapter
from src.apps.jp_drama.rendering.minimax_h3_approval import (
    MiniMaxH3ApprovalError,
    create_h3_approval_manifest,
    verify_h3_approval_manifest,
)
from src.apps.jp_drama.rendering.minimax_h3_client import MiniMaxH3Client
from src.apps.jp_drama.rendering.minimax_h3_config import MiniMaxH3ProviderConfig
from src.apps.jp_drama.rendering.minimax_h3_executor import (
    MiniMaxH3ExecutionError,
    MiniMaxH3Executor,
)
from src.apps.jp_drama.rendering.minimax_h3_models import H3ReferenceAsset, H3ReferenceBundle
from src.apps.jp_drama.rendering.provider_core import (
    ProviderCapabilitiesRequired,
    ReferenceAsset,
    ShotGenerationSpec,
)
from src.apps.jp_drama.rendering.provider_execution_ledger import H3ExecutionLedgerStore


VALID_MP4 = b"\x00\x00\x00\x18ftypmp42safe-test-video"


class SafetyTransport:
    def __init__(self, *, submit_status=200, submit_body=b'{"task_id":"task-1"}', corrupt_first=False):
        self.submit_status = submit_status
        self.submit_body = submit_body
        self.corrupt_first = corrupt_first
        self.post_count = 0
        self.query_count = 0
        self.download_count = 0

    def request(self, method, url, *, headers, body, timeout_seconds):
        if method == "POST":
            self.post_count += 1
            return self.submit_status, {}, self.submit_body
        if "query/video_generation" in url:
            self.query_count += 1
            return (
                200,
                {},
                b'{"task":{"id":"task-1","status":"succeeded",'
                b'"content":{"url":"https://cdn.example/video.mp4"}}}',
            )
        self.download_count += 1
        if self.corrupt_first and self.download_count == 1:
            return 200, {}, b"<html>bad CDN response</html>"
        return 200, {}, VALID_MP4


def _spec() -> ShotGenerationSpec:
    return ShotGenerationSpec(
        source_digest="sha256:" + "a" * 64,
        shot_id="segment_001",
        task_id="task_segment_001",
        modality="video",
        duration_seconds=8,
        resolution="768P",
        prompt="Vertical Japanese drama scene.",
        references=[
            ReferenceAsset(
                asset_id="ref_char_1",
                uri="https://assets.example/char.png",
                sha256="sha256:" + "b" * 64,
                role="character",
                order=0,
                size_bytes=1024,
                aspect_ratio=9 / 16,
                width_px=720,
                height_px=1280,
                media_format="png",
            )
        ],
        audio_strategy="native_av",
        required_capabilities=ProviderCapabilitiesRequired(
            modality="video",
            text_to_video=True,
            reference_to_video=True,
            native_audio=True,
            multi_shot=True,
        ),
    )


def _executor(tmp_path: Path, transport: SafetyTransport, *, retries=1):
    config = MiniMaxH3ProviderConfig(
        poll_interval_seconds=0.001,
        max_download_retries=retries,
    )
    client = MiniMaxH3Client(config, transport=transport, api_key="test")
    adapter = MiniMaxH3Adapter(config, route_id="minimax/h3-reference-av", client=client)
    prepared = adapter.prepare(_spec())
    executor = MiniMaxH3Executor(
        config,
        client,
        H3ExecutionLedgerStore(tmp_path / "ledger.json"),
        sleep=lambda _: None,
    )
    return prepared, executor


def _run(prepared, executor, tmp_path, *, max_cost=Decimal("1.00")):
    return executor.execute(
        prepared,
        segment_id="segment_001",
        estimated_cost_usd=Decimal("0.01"),
        max_cost_usd=max_cost,
        output_path=tmp_path / "segment.mp4",
        approval_verified=True,
    )


def test_executor_ignores_forged_estimate_and_recomputes_cost(tmp_path: Path) -> None:
    transport = SafetyTransport()
    prepared, executor = _executor(tmp_path, transport)
    with pytest.raises(MiniMaxH3ExecutionError, match="authoritative H3 cost"):
        _run(prepared, executor, tmp_path, max_cost=Decimal("0.10"))
    assert transport.post_count == 0


def test_http_500_submit_becomes_unknown_and_never_reposts(tmp_path: Path) -> None:
    transport = SafetyTransport(submit_status=500, submit_body=b'{"error":{"message":"server"}}')
    prepared, executor = _executor(tmp_path, transport)
    with pytest.raises(MiniMaxH3ExecutionError):
        _run(prepared, executor, tmp_path)
    with pytest.raises(MiniMaxH3ExecutionError, match="refusing a second POST"):
        _run(prepared, executor, tmp_path)
    assert transport.post_count == 1
    assert executor.ledger_store.load().status == "submission_unknown"


def test_unrelated_nested_id_is_not_accepted_as_task_id(tmp_path: Path) -> None:
    transport = SafetyTransport(submit_body=b'{"data":{"id":"wrong"}}')
    prepared, executor = _executor(tmp_path, transport)
    with pytest.raises(MiniMaxH3ExecutionError, match="approved path"):
        _run(prepared, executor, tmp_path)
    assert executor.ledger_store.load().status == "submission_unknown"


def test_corrupt_download_is_removed_and_retried_from_same_task(tmp_path: Path) -> None:
    transport = SafetyTransport(corrupt_first=True)
    prepared, executor = _executor(tmp_path, transport)
    output = _run(prepared, executor, tmp_path)
    assert output.read_bytes() == VALID_MP4
    assert transport.post_count == 1
    assert transport.download_count == 2
    assert executor.ledger_store.load().status == "validated"


def test_reference_video_requires_public_https_and_codec() -> None:
    bundle = H3ReferenceBundle(
        segment_id="segment_001",
        assets=[
            H3ReferenceAsset(
                asset_id="video",
                kind="video",
                url="https://127.0.0.1/video.mp4",
                role="reference_video",
                priority=1,
                size_bytes=1000,
                duration_seconds=3,
                fps=30,
                aspect_ratio=9 / 16,
                width_px=720,
                height_px=1280,
                media_format="mp4",
                sha256="sha256:" + "c" * 64,
            )
        ],
    )
    errors = bundle.preflight_errors()
    assert any("private" in item for item in errors)
    assert "video is missing video codec" in errors


def test_approval_is_invalidated_by_cost_or_fingerprint_change(tmp_path: Path) -> None:
    approval = tmp_path / "approval.json"
    create_h3_approval_manifest(
        segment_id="segment_001",
        request_fingerprint="sha256:" + "a" * 64,
        reference_asset_hashes=["sha256:" + "b" * 64],
        model="MiniMax-H3",
        resolution="768P",
        duration=8,
        authoritative_cost_usd=Decimal("0.64"),
        max_cost_usd=Decimal("1.00"),
        price_snapshot_id="minimax-h3-2026-08-05",
        output_path=approval,
    )
    with pytest.raises(MiniMaxH3ApprovalError, match="request_fingerprint"):
        verify_h3_approval_manifest(
            approval,
            segment_id="segment_001",
            request_fingerprint="sha256:" + "f" * 64,
            reference_asset_hashes=["sha256:" + "b" * 64],
            model="MiniMax-H3",
            resolution="768P",
            duration=8,
            authoritative_cost_usd=Decimal("0.64"),
            max_cost_usd=Decimal("1.00"),
            price_snapshot_id="minimax-h3-2026-08-05",
        )
