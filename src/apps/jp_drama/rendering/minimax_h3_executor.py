"""Single-segment restart-safe MiniMax H3 executor."""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .minimax_h3_client import MiniMaxH3Client, MiniMaxH3ClientError
from .minimax_h3_config import MiniMaxH3ProviderConfig
from .minimax_h3_models import H3ReferenceBundle, H3VideoGenerationRequest
from .provider_core import PreparedProviderRequest
from .provider_execution_ledger import (
    H3ExecutionLedgerStore,
    H3ExecutionRecord,
)


class MiniMaxH3ExecutionError(RuntimeError):
    """H3 execution failed, was cancelled, or cannot be resumed safely."""


class MiniMaxH3Executor:
    def __init__(
        self,
        config: MiniMaxH3ProviderConfig,
        client: MiniMaxH3Client,
        ledger_store: H3ExecutionLedgerStore,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.client = client
        self.ledger_store = ledger_store
        self.sleep = sleep

    def execute(
        self,
        prepared: PreparedProviderRequest,
        *,
        segment_id: str,
        estimated_cost_usd: Decimal,
        max_cost_usd: Decimal,
        output_path: str | Path,
        resume_only: bool = False,
    ) -> Path:
        if estimated_cost_usd > max_cost_usd:
            raise MiniMaxH3ExecutionError(
                f"estimated H3 cost {estimated_cost_usd} USD exceeds "
                f"max_cost_usd {max_cost_usd} USD"
            )
        h3_request = H3VideoGenerationRequest.model_validate(prepared.payload)
        bundle_payload = prepared.metadata.get("h3_reference_bundle")
        if not isinstance(bundle_payload, dict):
            raise MiniMaxH3ExecutionError(
                "prepared H3 request is missing durable reference preflight metadata"
            )
        bundle = H3ReferenceBundle.model_validate(bundle_payload)
        if bundle.segment_id != segment_id:
            raise MiniMaxH3ExecutionError(
                "reference bundle segment_id does not match executor segment_id"
            )
        try:
            bundle.require_valid(max_request_bytes=self.config.max_request_bytes)
        except ValueError as exc:
            raise MiniMaxH3ExecutionError(f"H3 reference preflight failed: {exc}") from exc

        prompt = next(item.text for item in h3_request.content if item.type == "text")
        reference_hashes = sorted(
            item.sha256 for item in bundle.assets if item.sha256 is not None
        )
        record = H3ExecutionRecord(
            request_fingerprint=prepared.request_fingerprint,
            segment_id=segment_id,
            route_id=prepared.route_id,
            model=h3_request.model,
            resolution=h3_request.resolution,
            duration=h3_request.duration,
            ratio=h3_request.ratio,
            prompt_sha256="sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            reference_asset_hashes=reference_hashes,
            estimated_cost_usd=estimated_cost_usd,
            max_cost_usd=max_cost_usd,
        )
        record = self.ledger_store.load_or_create(record)

        if record.status == "validated" and record.final_video_path:
            path = Path(record.final_video_path)
            if path.exists() and _sha256(path) == record.final_video_sha256:
                return path
        if record.status == "submitting" and record.task_id is None:
            self.ledger_store.set_status(
                record,
                "submission_unknown",
                error=(
                    "process stopped after the durable submitting marker and before task_id "
                    "was persisted; a second POST is forbidden"
                ),
            )
        if record.status == "submission_unknown":
            raise MiniMaxH3ExecutionError(
                "H3 submission result is unknown; refusing a second POST"
            )
        if record.status in {"failed", "cancelled"}:
            raise MiniMaxH3ExecutionError(record.error or f"H3 task {record.status}")

        destination = Path(output_path).resolve()
        if record.raw_video_path and record.raw_video_sha256:
            existing = Path(record.raw_video_path)
            if existing.exists() and _sha256(existing) == record.raw_video_sha256:
                _validate_minimum_mp4(existing)
                digest = _sha256(existing)
                self.ledger_store.mark_validated(record, path=existing, sha256=digest)
                return existing
            record.raw_video_path = None
            record.raw_video_sha256 = None
            self.ledger_store.set_status(record, "succeeded")

        if record.task_id is None:
            if resume_only:
                raise MiniMaxH3ExecutionError("resume mode forbids a new H3 POST")
            self.ledger_store.set_status(record, "submitting")
            try:
                submission = self.client.submit(h3_request)
            except MiniMaxH3ClientError as exc:
                if exc.submission_may_have_succeeded:
                    self.ledger_store.set_status(record, "submission_unknown", error=str(exc))
                else:
                    self.ledger_store.set_status(record, "failed", error=str(exc))
                raise MiniMaxH3ExecutionError(str(exc)) from exc
            self.ledger_store.mark_submitted(
                record,
                task_id=submission.task_id,
                provider_request_id=submission.request_id,
            )

        started = time.monotonic()
        consecutive_poll_failures = 0
        while record.status not in {"succeeded", "failed", "cancelled"}:
            if time.monotonic() - started > self.config.max_poll_seconds:
                raise MiniMaxH3ExecutionError("H3 polling exceeded max_poll_seconds")
            assert record.task_id is not None
            try:
                result = self.client.query(record.task_id)
                consecutive_poll_failures = 0
            except MiniMaxH3ClientError as exc:
                if not exc.retryable:
                    raise MiniMaxH3ExecutionError(str(exc)) from exc
                consecutive_poll_failures += 1
                self.ledger_store.set_status(record, record.status, error=str(exc))
                if consecutive_poll_failures > self.config.max_poll_retries:
                    raise MiniMaxH3ExecutionError(
                        "H3 polling exceeded max_poll_retries"
                    ) from exc
                self.sleep(self.config.poll_interval_seconds)
                continue
            self.ledger_store.mark_polled(
                record,
                status=result.status,
                result_url=result.output_url,
                usage=result.usage,
                error=result.error,
            )
            if result.status in {"queued", "running"}:
                self.sleep(self.config.poll_interval_seconds)

        if record.status in {"failed", "cancelled"}:
            raise MiniMaxH3ExecutionError(record.error or f"H3 task {record.status}")
        if not record.result_url:
            raise MiniMaxH3ExecutionError("succeeded H3 task has no result URL")

        download_error: MiniMaxH3ClientError | None = None
        for attempt in range(self.config.max_download_retries + 1):
            self.ledger_store.set_status(record, "downloading")
            try:
                self.client.download(record.result_url, destination)
                download_error = None
                break
            except MiniMaxH3ClientError as exc:
                download_error = exc
                self.ledger_store.set_status(record, "succeeded", error=str(exc))
                if not exc.retryable or attempt >= self.config.max_download_retries:
                    break
                self.sleep(self.config.poll_interval_seconds)
        if download_error is not None:
            raise MiniMaxH3ExecutionError(
                "H3 download failed after retry policy: " + str(download_error)
            ) from download_error

        digest = _sha256(destination)
        self.ledger_store.mark_downloaded(record, path=destination, sha256=digest)
        _validate_minimum_mp4(destination)
        digest = _sha256(destination)
        self.ledger_store.mark_validated(record, path=destination, sha256=digest)
        return destination


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_minimum_mp4(path: Path) -> None:
    data = path.read_bytes()
    if not data:
        raise MiniMaxH3ExecutionError("downloaded H3 video is empty")
    if len(data) < 12 or b"ftyp" not in data[:64]:
        raise MiniMaxH3ExecutionError("downloaded H3 video is not an MP4 container")
