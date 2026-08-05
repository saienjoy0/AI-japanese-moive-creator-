"""Single-segment restart-safe MiniMax H3 executor."""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .media_probe import MediaProbeError, validate_h3_mp4
from .minimax_h3_client import MiniMaxH3Client, MiniMaxH3ClientError
from .minimax_h3_config import MiniMaxH3ConfigurationError, MiniMaxH3ProviderConfig
from .minimax_h3_models import H3ReferenceBundle, H3VideoGenerationRequest
from .provider_core import PreparedProviderRequest
from .provider_execution_ledger import (
    H3ExecutionLedgerError,
    H3ExecutionLedgerStore,
    H3ExecutionRecord,
)


class MiniMaxH3ExecutionError(RuntimeError):
    """H3 execution failed, was cancelled, or cannot be resumed safely."""


def authoritative_h3_cost_usd(
    config: MiniMaxH3ProviderConfig,
    request: H3VideoGenerationRequest,
    bundle: H3ReferenceBundle,
) -> Decimal:
    """Calculate the charge boundary from the validated provider request itself."""
    return config.estimate_cost_usd(
        duration_seconds=request.duration,
        reference_image_count=bundle.billable_image_count,
        reference_video_seconds=bundle.reference_video_seconds,
        resolution=request.resolution,
    )


class MiniMaxH3Executor:
    def __init__(
        self,
        config: MiniMaxH3ProviderConfig,
        client: MiniMaxH3Client,
        ledger_store: H3ExecutionLedgerStore,
        *,
        sleep: Callable[[float], None] = time.sleep,
        video_validator: Callable[[Path, H3VideoGenerationRequest], None] | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.ledger_store = ledger_store
        self.sleep = sleep
        self.video_validator = video_validator or minimum_h3_video_validator

    def execute(
        self,
        prepared: PreparedProviderRequest,
        *,
        segment_id: str,
        max_cost_usd: Decimal,
        output_path: str | Path,
        estimated_cost_usd: Decimal | None = None,
        approval_verified: bool,
        resume_only: bool = False,
    ) -> Path:
        if max_cost_usd < 0:
            raise MiniMaxH3ExecutionError("max_cost_usd must not be negative")
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

        authoritative_cost = authoritative_h3_cost_usd(self.config, h3_request, bundle)
        if authoritative_cost > max_cost_usd:
            raise MiniMaxH3ExecutionError(
                f"authoritative H3 cost {authoritative_cost} USD exceeds "
                f"max_cost_usd {max_cost_usd} USD"
            )
        if not approval_verified:
            raise MiniMaxH3ExecutionError("H3 request approval is missing or invalid")
        try:
            self.client.require_credentials()
        except MiniMaxH3ConfigurationError as exc:
            raise MiniMaxH3ExecutionError(str(exc)) from exc

        prompt = next(item.text for item in h3_request.content if item.type == "text")
        assert prompt is not None
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
            estimated_cost_usd=estimated_cost_usd or authoritative_cost,
            authoritative_cost_usd=authoritative_cost,
            max_cost_usd=max_cost_usd,
            price_snapshot_id=f"minimax-h3-{self.config.price_snapshot_date}",
        )
        try:
            record = self.ledger_store.load_or_create(record)
            if record.status == "planned":
                self.ledger_store.mark_approved(record)
        except H3ExecutionLedgerError as exc:
            raise MiniMaxH3ExecutionError(str(exc)) from exc

        destination = Path(output_path).resolve()
        recovered = self._reuse_or_recover_download(record, h3_request)
        if recovered is not None:
            return recovered

        if record.status == "submitting" and record.task_id is None:
            self.ledger_store.mark_submission_unknown(
                record,
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

        if record.task_id is None:
            if resume_only:
                raise MiniMaxH3ExecutionError("resume mode forbids a new H3 POST")
            try:
                self.ledger_store.mark_submission_intent(record)
            except H3ExecutionLedgerError as exc:
                raise MiniMaxH3ExecutionError(str(exc)) from exc
            try:
                submission = self.client.submit(h3_request)
            except MiniMaxH3ClientError as exc:
                if exc.submission_may_have_succeeded:
                    self.ledger_store.mark_submission_unknown(record, error=str(exc))
                else:
                    self.ledger_store.mark_failed(record, error=str(exc))
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
                if result.task_id != record.task_id:
                    raise MiniMaxH3ExecutionError(
                        f"H3 query task_id mismatch: expected {record.task_id}, got {result.task_id}"
                    )
                consecutive_poll_failures = 0
            except MiniMaxH3ClientError as exc:
                if not exc.retryable:
                    raise MiniMaxH3ExecutionError(str(exc)) from exc
                consecutive_poll_failures += 1
                if consecutive_poll_failures > self.config.max_poll_retries:
                    raise MiniMaxH3ExecutionError("H3 polling exceeded max_poll_retries") from exc
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

        last_error: Exception | None = None
        for attempt in range(self.config.max_download_retries + 1):
            self.ledger_store.mark_download_attempt(record)
            try:
                self.client.download(record.result_url, destination)
                self.video_validator(destination, h3_request)
                digest = _sha256(destination)
                self.ledger_store.mark_downloaded(record, path=destination, sha256=digest)
                self.ledger_store.mark_validated(record, path=destination, sha256=digest)
                return destination
            except (MiniMaxH3ClientError, MediaProbeError, MiniMaxH3ExecutionError, OSError) as exc:
                last_error = exc
                destination.unlink(missing_ok=True)
                self.ledger_store.mark_download_failed(record, error=str(exc))
                retryable = not isinstance(exc, MiniMaxH3ClientError) or exc.retryable
                if not retryable or attempt >= self.config.max_download_retries:
                    break
                self.sleep(self.config.poll_interval_seconds)
        raise MiniMaxH3ExecutionError(
            "H3 download or validation failed after retry policy: " + str(last_error)
        ) from last_error

    def _reuse_or_recover_download(
        self,
        record: H3ExecutionRecord,
        request: H3VideoGenerationRequest,
    ) -> Path | None:
        candidate_path = record.final_video_path or record.raw_video_path
        candidate_sha = record.final_video_sha256 or record.raw_video_sha256
        if not candidate_path or not candidate_sha:
            return None
        path = Path(candidate_path)
        try:
            if not path.exists() or _sha256(path) != candidate_sha:
                raise MiniMaxH3ExecutionError("persisted H3 video is missing or has a SHA mismatch")
            self.video_validator(path, request)
            digest = _sha256(path)
            if record.status != "validated":
                self.ledger_store.mark_validated(record, path=path, sha256=digest)
            return path
        except (MediaProbeError, MiniMaxH3ExecutionError, OSError) as exc:
            path.unlink(missing_ok=True)
            if record.result_url:
                self.ledger_store.mark_download_failed(record, error=str(exc))
                return None
            raise MiniMaxH3ExecutionError(str(exc)) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def strict_h3_video_validator(path: Path, request: H3VideoGenerationRequest) -> None:
    validate_h3_mp4(
        path,
        expected_duration_seconds=request.duration,
        expected_resolution=request.resolution,
    )


def minimum_h3_video_validator(path: Path, request: H3VideoGenerationRequest) -> None:
    if not path.is_file() or path.stat().st_size < 12:
        raise MiniMaxH3ExecutionError("downloaded H3 video is empty")
    with path.open("rb") as handle:
        if b"ftyp" not in handle.read(64):
            raise MiniMaxH3ExecutionError("downloaded H3 video is not an MP4 container")
