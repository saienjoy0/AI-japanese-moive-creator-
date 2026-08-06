"""Deterministic approved-reference selection for Japanese-drama R2V routes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.utils.provider_media import ResolvedMediaInput

from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..rendering.approval import ApprovalError, png_dimensions
from ..rendering.ffmpeg import file_sha256
from .models import ApprovedAssetBundle, AssetReadinessIssue


REFERENCE_SELECTION_SCHEMA_VERSION = "1.0.0"
REFERENCE_PUBLICATION_SCHEMA_VERSION = "1.0.0"

GenerationInputMode = Literal["text", "first_frame", "reference_images"]
R2VAudioStrategy = Literal["native_audio", "external_tts", "silent"]


class ReferenceResolutionError(RuntimeError):
    """Reference selection or publication cannot proceed safely."""


class ReferenceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class ReferenceInputReadinessReport(ReferenceModel):
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_id: str = Field(min_length=1)
    input_mode: GenerationInputMode
    audio_strategy: R2VAudioStrategy
    required_asset_ids: list[str] = Field(default_factory=list)
    required_voice_character_ids: list[str] = Field(default_factory=list)
    ready: bool
    errors: list[AssetReadinessIssue] = Field(default_factory=list)
    warnings: list[AssetReadinessIssue] = Field(default_factory=list)


class SelectedReferenceImage(ReferenceModel):
    asset_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    role: Literal["character_master", "location_master", "prop_master"]
    order: int = Field(ge=0)
    local_path: str = Field(min_length=1)
    local_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ReferenceSelectionManifest(ReferenceModel):
    schema_version: Literal[REFERENCE_SELECTION_SCHEMA_VERSION] = (
        REFERENCE_SELECTION_SCHEMA_VERSION
    )
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_id: str = Field(min_length=1)
    provider_route_id: str = Field(min_length=1)
    images: list[SelectedReferenceImage] = Field(min_length=1, max_length=9)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> "ReferenceSelectionManifest":
        asset_ids = [item.asset_id for item in self.images]
        hashes = [item.local_sha256 for item in self.images]
        orders = [item.order for item in self.images]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("duplicate_reference_asset_id")
        if len(hashes) != len(set(hashes)):
            raise ValueError("duplicate_reference_asset_sha")
        if orders != list(range(len(self.images))):
            raise ValueError("reference image order must be contiguous")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("reference selection digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "ReferenceSelectionManifest":
        provisional = cls.model_construct(
            **data,
            content_digest="sha256:" + "0" * 64,
        )
        return cls.model_validate(
            {**data, "content_digest": provisional.compute_content_digest()}
        )

    def compute_content_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return _digest(payload)

    def to_canonical_json(self) -> str:
        return _json(self.model_dump(mode="json", exclude_none=True))


class PublishedReferenceImage(ReferenceModel):
    asset_id: str = Field(min_length=1)
    local_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    order: int = Field(ge=0)
    provider_url: str = Field(min_length=1)
    source_ref_type: str | None = None


class PublishedReferenceManifest(ReferenceModel):
    schema_version: Literal[REFERENCE_PUBLICATION_SCHEMA_VERSION] = (
        REFERENCE_PUBLICATION_SCHEMA_VERSION
    )
    selection_manifest_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    provider_region: str = Field(min_length=1)
    endpoint_origin_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    workspace_id_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    images: list[PublishedReferenceImage] = Field(min_length=1, max_length=9)
    request_headers: dict[str, str] = Field(default_factory=dict)
    published_at: datetime
    expires_at: datetime
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> "PublishedReferenceManifest":
        if self.expires_at <= self.published_at:
            raise ValueError("published reference lease must expire after publication")
        orders = [item.order for item in self.images]
        if orders != list(range(len(self.images))):
            raise ValueError("published reference order must be contiguous")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("published reference digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "PublishedReferenceManifest":
        provisional = cls.model_construct(
            **data,
            content_digest="sha256:" + "0" * 64,
        )
        return cls.model_validate(
            {**data, "content_digest": provisional.compute_content_digest()}
        )

    def compute_content_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return _digest(payload)

    def assert_usable(
        self,
        selection: ReferenceSelectionManifest,
        *,
        now: datetime | None = None,
    ) -> None:
        if self.selection_manifest_digest != selection.content_digest:
            raise ReferenceResolutionError(
                "published references belong to another selection manifest"
            )
        timestamp = now or datetime.now(timezone.utc)
        if timestamp >= self.expires_at:
            raise ReferenceResolutionError("published reference URLs are expired")
        selected = [
            (item.asset_id, item.local_sha256, item.order)
            for item in selection.images
        ]
        published = [
            (item.asset_id, item.local_sha256, item.order)
            for item in self.images
        ]
        if selected != published:
            raise ReferenceResolutionError(
                "published reference order or hashes differ from approved selection"
            )

    def to_canonical_json(self) -> str:
        return _json(self.model_dump(mode="json", exclude_none=True))


def assess_reference_input_readiness(
    plan: GenerationPlanEpisode,
    bundle: ApprovedAssetBundle,
    *,
    segment_id: str,
    input_mode: GenerationInputMode = "reference_images",
    audio_strategy: R2VAudioStrategy = "native_audio",
) -> ReferenceInputReadinessReport:
    errors: list[AssetReadinessIssue] = []
    warnings: list[AssetReadinessIssue] = []
    segment = _find_segment(plan, segment_id)

    if bundle.generation_plan_digest != plan.content_digest:
        errors.append(
            AssetReadinessIssue(
                code="asset_bundle_plan_mismatch",
                severity="error",
                message="asset bundle does not belong to the supplied GenerationPlan",
                segment_id=segment_id,
            )
        )
    if bundle.source_prepared_episode_digest != plan.source_prepared_episode_digest:
        errors.append(
            AssetReadinessIssue(
                code="asset_bundle_prepared_mismatch",
                severity="error",
                message="asset bundle and GenerationPlan bind different PreparedEpisodes",
                segment_id=segment_id,
            )
        )
    if input_mode != "reference_images":
        errors.append(
            AssetReadinessIssue(
                code="unsupported_generation_input_mode",
                severity="error",
                message="HappyHorse R2V requires input_mode=reference_images",
                segment_id=segment_id,
            )
        )

    required_ids = _required_reference_ids(segment)
    if not required_ids:
        errors.append(
            AssetReadinessIssue(
                code="reference_images_missing",
                severity="error",
                message="segment has no planned character/location/prop references",
                segment_id=segment_id,
            )
        )
    if len(required_ids) > 9:
        errors.append(
            AssetReadinessIssue(
                code="reference_image_limit_exceeded",
                severity="error",
                message=f"segment plans {len(required_ids)} reference images; maximum is 9",
                segment_id=segment_id,
            )
        )

    assets = {item.asset_id: item for item in bundle.assets}
    seen_hashes: dict[str, str] = {}
    for asset_id in required_ids:
        asset = assets.get(asset_id)
        if asset is None:
            errors.append(
                AssetReadinessIssue(
                    code="required_asset_missing",
                    severity="error",
                    message=f"asset bundle is missing {asset_id}",
                    asset_id=asset_id,
                    segment_id=segment_id,
                )
            )
            continue
        problem = _verify_master_asset(asset)
        if problem:
            errors.append(
                AssetReadinessIssue(
                    code="required_asset_not_ready",
                    severity="error",
                    message=problem,
                    asset_id=asset_id,
                    segment_id=segment_id,
                )
            )
        if asset.asset_sha256:
            previous = seen_hashes.get(asset.asset_sha256)
            if previous and previous != asset_id:
                errors.append(
                    AssetReadinessIssue(
                        code="duplicate_reference_asset_sha",
                        severity="error",
                        message=(
                            f"{asset_id} and {previous} resolve to the same approved bytes"
                        ),
                        asset_id=asset_id,
                        segment_id=segment_id,
                    )
                )
            seen_hashes[asset.asset_sha256] = asset_id

    speakers = sorted(
        {item.speaker_character_id for item in segment.dialogue_slices}
    )
    if audio_strategy == "external_tts":
        voices = {item.character_seed_id: item for item in bundle.voice_profiles}
        for speaker in speakers:
            voice = voices.get(speaker)
            if voice is None or voice.approval_status != "approved":
                errors.append(
                    AssetReadinessIssue(
                        code="voice_profile_not_ready",
                        severity="error",
                        message="external_tts requires an approved voice identity",
                        character_seed_id=speaker,
                        segment_id=segment_id,
                    )
                )
    elif audio_strategy == "native_audio" and speakers:
        warnings.append(
            AssetReadinessIssue(
                code="provider_native_voice_uncontrolled",
                severity="warning",
                message=(
                    "HappyHorse has no fixed voice_id input; voice identity must be "
                    "evaluated by a human and is not guaranteed across clips"
                ),
                segment_id=segment_id,
            )
        )

    return ReferenceInputReadinessReport(
        generation_plan_digest=plan.content_digest,
        asset_bundle_digest=bundle.content_digest,
        segment_id=segment_id,
        input_mode=input_mode,
        audio_strategy=audio_strategy,
        required_asset_ids=required_ids,
        required_voice_character_ids=speakers if audio_strategy == "external_tts" else [],
        ready=not errors,
        errors=errors,
        warnings=warnings,
    )


def build_reference_selection_manifest(
    plan: GenerationPlanEpisode,
    bundle: ApprovedAssetBundle,
    *,
    segment_id: str,
    audio_strategy: R2VAudioStrategy = "native_audio",
) -> ReferenceSelectionManifest:
    readiness = assess_reference_input_readiness(
        plan,
        bundle,
        segment_id=segment_id,
        input_mode="reference_images",
        audio_strategy=audio_strategy,
    )
    if not readiness.ready:
        raise ReferenceResolutionError(
            "reference selection is not ready: "
            + "; ".join(item.message for item in readiness.errors)
        )
    segment = _find_segment(plan, segment_id)
    assets = {item.asset_id: item for item in bundle.assets}
    images: list[SelectedReferenceImage] = []
    for order, asset_id in enumerate(readiness.required_asset_ids):
        asset = assets[asset_id]
        images.append(
            SelectedReferenceImage(
                asset_id=asset.asset_id,
                subject_id=asset.subject_id,
                role=asset.role,
                order=order,
                local_path=str(Path(asset.asset_path or "").resolve()),
                local_sha256=str(asset.asset_sha256),
                width=int(asset.width or 0),
                height=int(asset.height or 0),
            )
        )
    return ReferenceSelectionManifest.build_with_digest(
        generation_plan_digest=plan.content_digest,
        asset_bundle_digest=bundle.content_digest,
        segment_id=segment.segment_id,
        provider_route_id=segment.provider_route_id,
        images=images,
    )


def publish_reference_selection(
    selection: ReferenceSelectionManifest,
    resolved_inputs: Sequence[ResolvedMediaInput],
    *,
    provider_region: str,
    endpoint_origin_hash: str,
    workspace_id_hash: str,
    lease_seconds: int = 3600,
    now: datetime | None = None,
) -> PublishedReferenceManifest:
    if len(resolved_inputs) != len(selection.images):
        raise ReferenceResolutionError(
            "resolved provider inputs do not match the approved selection count"
        )
    if not 300 <= lease_seconds <= 7200:
        raise ReferenceResolutionError(
            "reference URL operational lease must be between 300 and 7200 seconds"
        )
    timestamp = now or datetime.now(timezone.utc)
    headers: dict[str, str] = {}
    published: list[PublishedReferenceImage] = []
    for selected, resolved in zip(selection.images, resolved_inputs):
        value = resolved.value.strip()
        if not value:
            raise ReferenceResolutionError("provider reference URL is empty")
        headers.update({key: value for key, value in resolved.headers.items() if value})
        published.append(
            PublishedReferenceImage(
                asset_id=selected.asset_id,
                local_sha256=selected.local_sha256,
                order=selected.order,
                provider_url=value,
                source_ref_type=resolved.media_ref_type,
            )
        )
    manifest = PublishedReferenceManifest.build_with_digest(
        selection_manifest_digest=selection.content_digest,
        provider_region=provider_region,
        endpoint_origin_hash=endpoint_origin_hash,
        workspace_id_hash=workspace_id_hash,
        images=published,
        request_headers=headers,
        published_at=timestamp,
        expires_at=timestamp + timedelta(seconds=lease_seconds),
    )
    manifest.assert_usable(selection, now=timestamp)
    return manifest


def _find_segment(
    plan: GenerationPlanEpisode,
    segment_id: str,
) -> GenerationSegment:
    matches = [item for item in plan.segments if item.segment_id == segment_id]
    if len(matches) != 1:
        raise ReferenceResolutionError(
            f"unknown or duplicate generation segment: {segment_id}"
        )
    return matches[0]


def _required_reference_ids(segment: GenerationSegment) -> list[str]:
    ids = [
        item
        for item in segment.reference_asset_ids
        if item.startswith(("ref_char_", "ref_loc_", "ref_prop_"))
    ]
    if len(ids) != len(set(ids)):
        raise ReferenceResolutionError("duplicate_reference_asset_id")
    return ids


def _verify_master_asset(asset) -> str | None:
    if asset.role not in {"character_master", "location_master", "prop_master"}:
        return f"asset {asset.asset_id} is not a master reference"
    if asset.approval_status != "approved":
        return f"asset {asset.asset_id} is not approved"
    if not asset.asset_path or not asset.asset_sha256:
        return f"approved asset metadata is incomplete: {asset.asset_id}"
    path = Path(asset.asset_path).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        return f"approved asset file is missing or empty: {path}"
    if file_sha256(path) != asset.asset_sha256:
        return f"approved asset hash changed after approval: {asset.asset_id}"
    if asset.mime_type != "image/png":
        return f"approved R2V master must be image/png: {asset.asset_id}"
    try:
        width, height = png_dimensions(path)
    except ApprovalError as exc:
        return str(exc)
    if (width, height) != (asset.width, asset.height):
        return f"approved image dimensions changed after approval: {asset.asset_id}"
    return None


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _json(payload: object) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        )
        + "\n"
    )
