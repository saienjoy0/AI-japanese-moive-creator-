"""Provider registry and initial Wan/Seedance route adapters."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .provider_config import LiveProviderConfig
from .provider_core import (
    CostEstimate,
    Money,
    PreparedProviderRequest,
    ProviderAdapter,
    ProviderArtifactSet,
    ProviderCapabilities,
    ProviderCoreError,
    ProviderDescriptor,
    ProviderPollResult,
    ProviderSubmission,
    ShotGenerationSpec,
    ValidationIssue,
    ValidationReport,
)


class ProviderRegistryError(ProviderCoreError):
    """A provider route is missing or registered more than once."""


class ProviderRegistry:
    """Explicit allow-list of provider routes available to an execution plan."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        route_id = adapter.descriptor().route_id
        if route_id in self._adapters:
            raise ProviderRegistryError(f"provider route already registered: {route_id}")
        self._adapters[route_id] = adapter

    def get(self, route_id: str) -> ProviderAdapter | None:
        return self._adapters.get(route_id)

    def require(self, route_id: str) -> ProviderAdapter:
        adapter = self.get(route_id)
        if adapter is None:
            raise ProviderRegistryError(f"unknown provider route: {route_id}")
        return adapter

    def route_ids(self) -> list[str]:
        return sorted(self._adapters)

    def descriptors(self) -> list[ProviderDescriptor]:
        return [self._adapters[key].descriptor() for key in self.route_ids()]


class MockProviderAdapter:
    """Deterministic zero-cost adapter used by CI and planner tests."""

    def __init__(self, route_id: str = "mock/video") -> None:
        self.route_id = route_id

    def descriptor(self) -> ProviderDescriptor:
        provider, model = self.route_id.split("/", 1)
        return ProviderDescriptor(
            route_id=self.route_id,
            provider=provider,
            model=model,
            origin_vendor="local",
            execution_mode="automatic",
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            route_id=self.route_id,
            execution_mode="automatic",
            modalities=["image", "video", "speech"],
            text_to_video=True,
            image_to_video=True,
            reference_to_video=True,
            first_last_frame=True,
            video_continuation=True,
            native_audio=True,
            driving_audio=True,
            reference_voice=True,
            multi_shot=True,
            video_editing=True,
            min_duration_seconds=1,
            max_duration_seconds=60,
            max_reference_images=99,
            max_reference_videos=99,
            max_reference_audios=99,
            supported_aspect_ratios=["9:16"],
            supported_resolutions=["720P", "1080P"],
        )

    def validate(self, request: ShotGenerationSpec) -> ValidationReport:
        return ValidationReport.success()

    def estimate_cost(self, request: ShotGenerationSpec) -> CostEstimate:
        return CostEstimate(
            native_cost=Money(amount=Decimal("0"), currency="CNY"),
            confidence="exact",
            price_snapshot_id="mock-zero-cost",
        )

    def prepare(self, request: ShotGenerationSpec) -> PreparedProviderRequest:
        return _prepared_request(self.route_id, request)

    def submit(self, request: PreparedProviderRequest) -> ProviderSubmission:
        return ProviderSubmission(
            route_id=self.route_id,
            operation_id=request.operation_id,
            status="succeeded",
            provider_task_id=f"mock-{request.request_fingerprint[-12:]}",
        )

    def poll(self, submission: ProviderSubmission) -> ProviderPollResult:
        return ProviderPollResult(
            route_id=self.route_id,
            operation_id=submission.operation_id,
            status="succeeded",
            provider_task_id=submission.provider_task_id,
            output_uris=[f"mock://{submission.operation_id}"],
        )

    def download(
        self,
        result: ProviderPollResult,
        output_dir: str | Path,
    ) -> ProviderArtifactSet:
        destination = Path(output_dir).resolve() / f"{result.operation_id}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(result.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        from .provider_core import ProviderArtifact

        return ProviderArtifactSet(
            route_id=self.route_id,
            operation_id=result.operation_id,
            artifacts=[
                ProviderArtifact(
                    path=str(destination),
                    sha256=f"sha256:{digest}",
                    media_type="application/json",
                    raw=True,
                )
            ],
        )


class SeedancePlatformAdapter:
    """Manual official-platform route; no browser automation or paid API calls."""

    ROUTE_ID = "seedance/platform"

    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            route_id=self.ROUTE_ID,
            provider="seedance",
            model="platform",
            origin_vendor="bytedance",
            execution_mode="manual",
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            route_id=self.ROUTE_ID,
            execution_mode="manual",
            modalities=["video"],
            text_to_video=True,
            image_to_video=True,
            reference_to_video=True,
            first_last_frame=True,
            video_continuation=True,
            native_audio=True,
            driving_audio=True,
            reference_voice=True,
            multi_shot=True,
            video_editing=True,
            min_duration_seconds=4,
            max_duration_seconds=15,
            max_reference_images=9,
            max_reference_videos=3,
            max_reference_audios=3,
            supported_aspect_ratios=["9:16"],
            supported_resolutions=["720P", "1080P"],
        )

    def validate(self, request: ShotGenerationSpec) -> ValidationReport:
        errors = _validate_against_capabilities(request, self.capabilities())
        return ValidationReport.failure(*errors) if errors else ValidationReport.success()

    def estimate_cost(self, request: ShotGenerationSpec) -> CostEstimate:
        return CostEstimate(confidence="unknown")

    def prepare(self, request: ShotGenerationSpec) -> PreparedProviderRequest:
        return _prepared_request(self.ROUTE_ID, request)

    def submit(self, request: PreparedProviderRequest) -> ProviderSubmission:
        return ProviderSubmission(
            route_id=self.ROUTE_ID,
            operation_id=request.operation_id,
            status="awaiting_operator",
            metadata={
                "action": "generate in the official Seedance platform and import the result",
                "request_fingerprint": request.request_fingerprint,
            },
        )

    def poll(self, submission: ProviderSubmission) -> ProviderPollResult:
        return ProviderPollResult(
            route_id=self.ROUTE_ID,
            operation_id=submission.operation_id,
            status="awaiting_operator",
            error=None,
        )

    def download(
        self,
        result: ProviderPollResult,
        output_dir: str | Path,
    ) -> ProviderArtifactSet:
        raise ProviderCoreError(
            "seedance/platform results must be imported by an operator; automatic download is disabled"
        )


class Wan27PlanningAdapter:
    """Wan route adapter that reuses PR8 pricing and allows injected execution."""

    ROUTE_ID = "wan/i2v"

    def __init__(
        self,
        config: LiveProviderConfig,
        *,
        submitter: Callable[[PreparedProviderRequest], ProviderSubmission] | None = None,
    ) -> None:
        self.config = config
        self.submitter = submitter

    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            route_id=self.ROUTE_ID,
            provider="dashscope",
            model=self.config.dashscope.video_model,
            origin_vendor="alibaba",
            execution_mode="automatic",
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            route_id=self.ROUTE_ID,
            execution_mode="automatic",
            modalities=["video"],
            text_to_video=False,
            image_to_video=True,
            reference_to_video=False,
            first_last_frame=True,
            video_continuation=True,
            native_audio=False,
            driving_audio=False,
            reference_voice=False,
            multi_shot=False,
            video_editing=False,
            min_duration_seconds=2,
            max_duration_seconds=15,
            max_reference_images=2,
            max_reference_videos=1,
            max_reference_audios=0,
            supported_aspect_ratios=["9:16"],
            supported_resolutions=[self.config.dashscope.video_resolution],
        )

    def validate(self, request: ShotGenerationSpec) -> ValidationReport:
        errors = _validate_against_capabilities(request, self.capabilities())
        if request.audio_strategy in {"native_av", "driving_audio"}:
            errors.append(
                ValidationIssue(
                    code="wan_i2v_audio_strategy_not_migrated",
                    message=(
                        "wan/i2v currently uses the PR8 video plus external-audio path; "
                        "native or driving audio moves to a dedicated Wan route later"
                    ),
                )
            )
        return ValidationReport.failure(*errors) if errors else ValidationReport.success()

    def estimate_cost(self, request: ShotGenerationSpec) -> CostEstimate:
        amount = self.config.dashscope.estimate_video_cost_cny(
            min(request.duration_seconds, self.config.dashscope.provider_clip_seconds)
        )
        return CostEstimate(
            native_cost=Money(amount=amount, currency="CNY"),
            confidence="estimated",
            price_snapshot_id=f"dashscope-{self.config.dashscope.price_snapshot_date}",
        )

    def prepare(self, request: ShotGenerationSpec) -> PreparedProviderRequest:
        return _prepared_request(
            self.ROUTE_ID,
            request,
            extra={
                "model": self.config.dashscope.video_model,
                "resolution": self.config.dashscope.video_resolution,
                "provider_clip_seconds": self.config.dashscope.provider_clip_seconds,
            },
        )

    def submit(self, request: PreparedProviderRequest) -> ProviderSubmission:
        if self.submitter is None:
            raise ProviderCoreError(
                "wan/i2v execution remains delegated to Wan27LiveTaskExecutor until the executor migration step"
            )
        return self.submitter(request)

    def poll(self, submission: ProviderSubmission) -> ProviderPollResult:
        raise ProviderCoreError("wan/i2v polling remains delegated to the PR8 executor")

    def download(
        self,
        result: ProviderPollResult,
        output_dir: str | Path,
    ) -> ProviderArtifactSet:
        raise ProviderCoreError("wan/i2v download remains delegated to the PR8 executor")


def build_default_provider_registry(config: LiveProviderConfig) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(MockProviderAdapter())
    registry.register(SeedancePlatformAdapter())
    registry.register(Wan27PlanningAdapter(config))
    return registry


def _prepared_request(
    route_id: str,
    request: ShotGenerationSpec,
    *,
    extra: dict | None = None,
) -> PreparedProviderRequest:
    payload = request.model_dump(mode="json", exclude_none=True)
    if extra:
        payload["provider_options"] = extra
    fingerprint_payload = json.dumps(
        {"route_id": route_id, "request": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = f"sha256:{hashlib.sha256(fingerprint_payload).hexdigest()}"
    return PreparedProviderRequest(
        route_id=route_id,
        operation_id=request.task_id,
        request_fingerprint=fingerprint,
        payload=payload,
    )


def _validate_against_capabilities(
    request: ShotGenerationSpec,
    capabilities: ProviderCapabilities,
) -> list[ValidationIssue]:
    errors: list[ValidationIssue] = []
    if not capabilities.supports(request.required_capabilities):
        errors.append(
            ValidationIssue(
                code="capability_mismatch",
                message=f"{capabilities.route_id} does not satisfy required capabilities",
            )
        )
    if not (
        capabilities.min_duration_seconds
        <= request.duration_seconds
        <= capabilities.max_duration_seconds
    ):
        errors.append(
            ValidationIssue(
                code="duration_not_supported",
                message=(
                    f"duration {request.duration_seconds} is outside "
                    f"{capabilities.min_duration_seconds}-{capabilities.max_duration_seconds} seconds"
                ),
            )
        )
    if request.aspect_ratio not in capabilities.supported_aspect_ratios:
        errors.append(
            ValidationIssue(
                code="aspect_ratio_not_supported",
                message=f"aspect ratio is not supported: {request.aspect_ratio}",
            )
        )
    if request.resolution not in capabilities.supported_resolutions:
        errors.append(
            ValidationIssue(
                code="resolution_not_supported",
                message=f"resolution is not supported: {request.resolution}",
            )
        )
    image_count = sum(
        item.role
        in {
            "character",
            "costume",
            "location",
            "prop",
            "storyboard",
            "first_frame",
            "last_frame",
        }
        for item in request.references
    )
    video_count = sum(
        item.role in {"motion_reference", "video_reference"}
        for item in request.references
    )
    audio_count = sum(
        item.role in {"driving_audio", "voice_reference"}
        for item in request.references
    )
    if image_count > capabilities.max_reference_images:
        errors.append(
            ValidationIssue(
                code="too_many_reference_images",
                message=f"reference images exceed {capabilities.max_reference_images}",
            )
        )
    if video_count > capabilities.max_reference_videos:
        errors.append(
            ValidationIssue(
                code="too_many_reference_videos",
                message=f"reference videos exceed {capabilities.max_reference_videos}",
            )
        )
    if audio_count > capabilities.max_reference_audios:
        errors.append(
            ValidationIssue(
                code="too_many_reference_audios",
                message=f"reference audios exceed {capabilities.max_reference_audios}",
            )
        )
    return errors
