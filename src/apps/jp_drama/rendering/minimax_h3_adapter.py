"""Provider adapters that make MiniMax H3 the production-first video route."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from .minimax_h3_client import MiniMaxH3Client
from .minimax_h3_config import MiniMaxH3ProviderConfig
from .minimax_h3_models import H3ContentItem, H3VideoGenerationRequest
from .provider_core import (
    CostEstimate,
    Money,
    PreparedProviderRequest,
    ProviderArtifact,
    ProviderArtifactSet,
    ProviderCapabilities,
    ProviderCoreError,
    ProviderDescriptor,
    ProviderPollResult,
    ProviderSubmission,
    ReferenceAsset,
    ShotGenerationSpec,
    ValidationIssue,
    ValidationReport,
)


H3_PRODUCTION_ROUTE_PRIORITY = [
    "minimax/h3-reference-av",
    "minimax/h3-first-frame",
    "minimax/h3-text",
    "wan/i2v",
    "mock/video",
]


class MiniMaxH3Adapter:
    """One H3 V2 route with explicit mode separation and optional live client."""

    def __init__(
        self,
        config: MiniMaxH3ProviderConfig,
        *,
        route_id: Literal[
            "minimax/h3-reference-av",
            "minimax/h3-first-frame",
            "minimax/h3-text",
        ],
        client: MiniMaxH3Client | None = None,
    ) -> None:
        self.config = config
        self.route_id = route_id
        self.client = client

    @property
    def mode(self) -> Literal["reference", "first_frame", "text"]:
        return {
            "minimax/h3-reference-av": "reference",
            "minimax/h3-first-frame": "first_frame",
            "minimax/h3-text": "text",
        }[self.route_id]

    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            route_id=self.route_id,
            provider="minimax",
            model=self.config.model,
            origin_vendor="minimax",
            execution_mode="automatic",
        )

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            route_id=self.route_id,
            execution_mode="automatic",
            modalities=["video"],
            text_to_video=self.mode in {"text", "reference"},
            image_to_video=self.mode == "first_frame",
            reference_to_video=self.mode == "reference",
            first_last_frame=self.mode == "first_frame",
            video_continuation=self.mode in {"reference", "first_frame"},
            native_audio=True,
            driving_audio=self.mode == "reference",
            reference_voice=self.mode == "reference",
            multi_shot=True,
            video_editing=False,
            min_duration_seconds=4,
            max_duration_seconds=15,
            max_reference_images=9 if self.mode == "reference" else 2,
            max_reference_videos=3 if self.mode == "reference" else 0,
            max_reference_audios=3 if self.mode == "reference" else 0,
            supported_aspect_ratios=["9:16"],
            supported_resolutions=["720P", "768P", "2K"],
        )

    def validate(self, request: ShotGenerationSpec) -> ValidationReport:
        errors: list[ValidationIssue] = []
        capabilities = self.capabilities()
        if request.modality != "video":
            errors.append(ValidationIssue(code="modality_not_supported", message="H3 is video-only"))
        if not capabilities.supports(request.required_capabilities):
            errors.append(
                ValidationIssue(
                    code="capability_mismatch",
                    message=f"{self.route_id} does not satisfy required capabilities",
                )
            )
        if request.duration_seconds != int(request.duration_seconds):
            errors.append(
                ValidationIssue(
                    code="integer_duration_required",
                    message="MiniMax H3 requires an integer duration",
                )
            )
        if not 4 <= request.duration_seconds <= 15:
            errors.append(
                ValidationIssue(
                    code="duration_not_supported",
                    message="MiniMax H3 duration must be between 4 and 15 seconds",
                )
            )
        if request.resolution not in {"720P", "768P", "2K"}:
            errors.append(
                ValidationIssue(
                    code="resolution_not_supported",
                    message="MiniMax H3 resolution must be 768P or 2K (720P is a planner alias)",
                )
            )
        frame_refs = [
            item for item in request.references if item.role in {"first_frame", "last_frame"}
        ]
        reference_refs = [
            item
            for item in request.references
            if item.role
            in {
                "character",
                "costume",
                "location",
                "prop",
                "storyboard",
                "motion_reference",
                "video_reference",
                "driving_audio",
                "voice_reference",
                "reference_audio",
            }
        ]
        if frame_refs and reference_refs:
            errors.append(
                ValidationIssue(
                    code="h3_mode_mixed",
                    message="first/last-frame and reference inputs cannot be mixed",
                )
            )
        if self.mode == "text" and request.references:
            errors.append(
                ValidationIssue(
                    code="h3_text_has_references",
                    message="H3 text route accepts no references",
                )
            )
        if self.mode == "first_frame":
            if sum(item.role == "first_frame" for item in request.references) != 1:
                errors.append(
                    ValidationIssue(
                        code="h3_first_frame_required",
                        message="H3 first-frame route requires exactly one first_frame",
                    )
                )
            if reference_refs:
                errors.append(
                    ValidationIssue(
                        code="h3_first_frame_has_reference_inputs",
                        message="H3 first-frame route rejects reference-mode inputs",
                    )
                )
        if self.mode == "reference":
            if frame_refs:
                errors.append(
                    ValidationIssue(
                        code="h3_reference_has_frame_inputs",
                        message="H3 reference route rejects first/last-frame inputs",
                    )
                )
            if not reference_refs:
                errors.append(
                    ValidationIssue(
                        code="h3_reference_input_required",
                        message="H3 reference route requires at least one reference input",
                    )
                )
        image_count = sum(
            item.role in {"character", "costume", "location", "prop", "storyboard"}
            for item in request.references
        )
        video_count = sum(
            item.role in {"motion_reference", "video_reference"}
            for item in request.references
        )
        audio_count = sum(
            item.role in {"driving_audio", "voice_reference", "reference_audio"}
            for item in request.references
        )
        if image_count > 9:
            errors.append(
                ValidationIssue(
                    code="too_many_reference_images",
                    message="H3 reference images exceed 9",
                )
            )
        if video_count > 3:
            errors.append(
                ValidationIssue(
                    code="too_many_reference_videos",
                    message="H3 reference videos exceed 3",
                )
            )
        if audio_count > 3:
            errors.append(
                ValidationIssue(
                    code="too_many_reference_audios",
                    message="H3 reference audios exceed 3",
                )
            )
        return ValidationReport.failure(*errors) if errors else ValidationReport.success()

    def estimate_cost(self, request: ShotGenerationSpec) -> CostEstimate:
        image_count = sum(
            item.role in {"character", "costume", "location", "prop", "storyboard"}
            for item in request.references
        )
        has_video_reference = any(
            item.role in {"motion_reference", "video_reference"}
            for item in request.references
        )
        actual_resolution = (
            self.config.resolution if request.resolution == "720P" else request.resolution
        )
        amount = self.config.estimate_cost_usd(
            duration_seconds=request.duration_seconds,
            reference_image_count=image_count,
            resolution=actual_resolution,
        )
        return CostEstimate(
            native_cost=Money(amount=amount, currency="USD"),
            confidence="estimated" if has_video_reference else "exact",
            price_snapshot_id=f"minimax-h3-{self.config.price_snapshot_date}",
        )

    def prepare(self, request: ShotGenerationSpec) -> PreparedProviderRequest:
        report = self.validate(request)
        if not report.valid:
            raise ProviderCoreError(
                "MiniMax H3 request is invalid: "
                + "; ".join(item.message for item in report.errors)
            )
        content = [H3ContentItem.text_item(_compose_prompt(request))]
        for reference in sorted(request.references, key=lambda item: item.order):
            mapped = _reference_to_h3(reference)
            if mapped is not None:
                content.append(mapped)
        actual_resolution = (
            self.config.resolution if request.resolution == "720P" else request.resolution
        )
        h3_request = H3VideoGenerationRequest(
            model=self.config.model,
            content=content,
            resolution=actual_resolution,
            duration=int(math.ceil(request.duration_seconds)),
            ratio="adaptive" if self.mode == "first_frame" else request.aspect_ratio,
        )
        payload = h3_request.model_dump(mode="json", exclude_none=True)
        fingerprint_payload = json.dumps(
            {
                "route_id": self.route_id,
                "api_schema": "minimax-h3-v2",
                "request": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return PreparedProviderRequest(
            route_id=self.route_id,
            operation_id=request.task_id,
            request_fingerprint=f"sha256:{hashlib.sha256(fingerprint_payload).hexdigest()}",
            payload=payload,
        )

    def submit(self, request: PreparedProviderRequest) -> ProviderSubmission:
        if self.client is None:
            raise ProviderCoreError("MiniMax H3 execution requires an injected client")
        if request.route_id != self.route_id:
            raise ProviderCoreError("prepared request route does not match this H3 adapter")
        result = self.client.submit(H3VideoGenerationRequest.model_validate(request.payload))
        return ProviderSubmission(
            route_id=self.route_id,
            operation_id=request.operation_id,
            status="queued",
            provider_task_id=result.task_id,
            provider_request_id=result.request_id,
            metadata={"request_fingerprint": request.request_fingerprint},
        )

    def poll(self, submission: ProviderSubmission) -> ProviderPollResult:
        if self.client is None:
            raise ProviderCoreError("MiniMax H3 execution requires an injected client")
        if not submission.provider_task_id:
            raise ProviderCoreError("MiniMax H3 submission has no provider task ID")
        result = self.client.query(submission.provider_task_id)
        return ProviderPollResult(
            route_id=self.route_id,
            operation_id=submission.operation_id,
            status=result.status,
            provider_task_id=result.task_id,
            output_uris=[result.output_url] if result.output_url else [],
            error=result.error,
        )

    def download(
        self,
        result: ProviderPollResult,
        output_dir: str | Path,
    ) -> ProviderArtifactSet:
        if self.client is None:
            raise ProviderCoreError("MiniMax H3 execution requires an injected client")
        if result.status != "succeeded" or len(result.output_uris) != 1:
            raise ProviderCoreError("MiniMax H3 result is not downloadable")
        destination = Path(output_dir).resolve() / f"{result.operation_id}.mp4"
        self.client.download(result.output_uris[0], destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return ProviderArtifactSet(
            route_id=self.route_id,
            operation_id=result.operation_id,
            artifacts=[
                ProviderArtifact(
                    path=str(destination),
                    sha256=f"sha256:{digest}",
                    media_type="video/mp4",
                    raw=True,
                )
            ],
        )


def build_h3_first_provider_registry(
    live_config,
    h3_config: MiniMaxH3ProviderConfig,
    *,
    client: MiniMaxH3Client | None = None,
):
    """Build the production registry while preserving all pre-H3 routes."""
    from .provider_registry import (
        MockProviderAdapter,
        ProviderRegistry,
        SeedancePlatformAdapter,
        Wan27ImagePlanningAdapter,
        Wan27PlanningAdapter,
    )

    registry = ProviderRegistry()
    for route_id in H3_PRODUCTION_ROUTE_PRIORITY[:3]:
        registry.register(MiniMaxH3Adapter(h3_config, route_id=route_id, client=client))
    registry.register(Wan27ImagePlanningAdapter(live_config))
    registry.register(Wan27PlanningAdapter(live_config))
    registry.register(MockProviderAdapter())
    registry.register(SeedancePlatformAdapter())
    return registry


def _compose_prompt(request: ShotGenerationSpec) -> str:
    parts = [request.prompt]
    if request.dialogue:
        dialogue = "\n".join(
            f"{line.speaker_character_id}: {line.text}" for line in request.dialogue
        )
        parts.append("Exact Japanese dialogue timing reference:\n" + dialogue)
    if request.negative_prompt:
        parts.append("Avoid: " + request.negative_prompt)
    return "\n\n".join(parts)


def _reference_to_h3(reference: ReferenceAsset) -> H3ContentItem | None:
    if reference.role in {"character", "costume", "location", "prop", "storyboard"}:
        return H3ContentItem.media_item("image_url", reference.uri, "reference_image")
    if reference.role in {"first_frame", "last_frame"}:
        return H3ContentItem.media_item("image_url", reference.uri, reference.role)
    if reference.role in {"motion_reference", "video_reference"}:
        return H3ContentItem.media_item("video_url", reference.uri, "reference_video")
    if reference.role in {"driving_audio", "voice_reference", "reference_audio"}:
        return H3ContentItem.media_item("audio_url", reference.uri, "reference_audio")
    return None
