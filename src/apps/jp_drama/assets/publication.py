"""Approval-bound publication of local approved assets for MiniMax H3."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.utils.oss_utils import (
    OSSImageUploader,
    get_oss_base_path,
)

from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..rendering.approval import ApprovalError, png_dimensions
from ..rendering.ffmpeg import file_sha256
from ..rendering.minimax_h3_canary import H3CanaryAsset, H3CanaryAssetManifest
from .bundle import AssetBundleError, assess_asset_readiness
from .models import ApprovedAssetBundle, ApprovedReferenceAsset


H3_ASSET_PUBLICATION_SCHEMA_VERSION = "1.0.0"
_H3_ROUTES = {
    "minimax/h3-reference-av",
    "minimax/h3-first-frame",
    "minimax/h3-text",
}


class H3AssetPublicationError(RuntimeError):
    """Approved H3 reference images cannot be published safely."""


class PublicationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class H3PublicationItem(PublicationModel):
    asset_id: str = Field(min_length=1)
    role: Literal["character_master", "location_master", "prop_master"]
    subject_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    local_path: str = Field(min_length=1)
    local_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    mime_type: Literal["image/png"] = "image/png"
    object_relative_path: str = Field(min_length=1)


class H3AssetPublicationPreflight(PublicationModel):
    schema_version: Literal[H3_ASSET_PUBLICATION_SCHEMA_VERSION] = (
        H3_ASSET_PUBLICATION_SCHEMA_VERSION
    )
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_id: str = Field(min_length=1)
    provider_route_id: str = Field(min_length=1)
    items: list[H3PublicationItem] = Field(min_length=1, max_length=9)
    external_storage_calls: Literal[0] = 0
    valid: Literal[True] = True
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_preflight(self) -> "H3AssetPublicationPreflight":
        ids = [item.asset_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("H3 publication asset IDs must be unique")
        orders = [item.order for item in self.items]
        if orders != list(range(len(self.items))):
            raise ValueError("H3 publication item order must be contiguous")
        paths = [item.object_relative_path for item in self.items]
        if len(paths) != len(set(paths)):
            raise ValueError("H3 publication object paths must be unique")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("H3 publication preflight digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "H3AssetPublicationPreflight":
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


class PublishedH3Asset(PublicationModel):
    asset_id: str = Field(min_length=1)
    local_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    object_key: str = Field(min_length=1)
    signed_url: str = Field(pattern=r"^https://")
    signed_url_expires_at: datetime
    uploaded_in_this_run: bool


class H3PublishedAssetManifest(PublicationModel):
    schema_version: Literal[H3_ASSET_PUBLICATION_SCHEMA_VERSION] = (
        H3_ASSET_PUBLICATION_SCHEMA_VERSION
    )
    preflight_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_id: str = Field(min_length=1)
    assets: list[PublishedH3Asset] = Field(min_length=1, max_length=9)
    external_storage_uploads: int = Field(ge=0)
    external_storage_signatures: int = Field(ge=1)
    published_at: datetime
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> "H3PublishedAssetManifest":
        ids = [item.asset_id for item in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("published H3 asset IDs must be unique")
        if self.external_storage_uploads != sum(
            item.uploaded_in_this_run for item in self.assets
        ):
            raise ValueError("external_storage_uploads does not match asset records")
        if self.external_storage_signatures != len(self.assets):
            raise ValueError("every published H3 asset requires one signed URL")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("published H3 manifest digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "H3PublishedAssetManifest":
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


class H3AssetPublisher(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def object_key(self, relative_path: str) -> str: ...

    def object_exists(self, object_key: str) -> bool: ...

    def upload(self, local_path: str, relative_path: str) -> str: ...

    def sign_for_api(self, object_key: str, expires_seconds: int) -> str: ...


class OSSH3AssetPublisher:
    """Strict adapter over LumenX's existing private OSS uploader."""

    def __init__(self, uploader: OSSImageUploader | None = None) -> None:
        self.uploader = uploader or OSSImageUploader()
        self.base_path = get_oss_base_path().strip("/")

    @property
    def is_configured(self) -> bool:
        return bool(self.uploader.is_configured)

    def object_key(self, relative_path: str) -> str:
        relative = relative_path.strip("/")
        return f"{self.base_path}/{relative}" if self.base_path else relative

    def object_exists(self, object_key: str) -> bool:
        return bool(self.uploader.object_exists(object_key))

    def upload(self, local_path: str, relative_path: str) -> str:
        relative = Path(relative_path)
        result = self.uploader.upload_file(
            local_path,
            sub_path=relative.parent.as_posix(),
            custom_filename=relative.name,
        )
        if not result:
            raise H3AssetPublicationError("OSS upload returned no object key")
        expected = self.object_key(relative_path)
        if result != expected:
            raise H3AssetPublicationError(
                f"OSS object key {result} does not match approved target {expected}"
            )
        return result

    def sign_for_api(self, object_key: str, expires_seconds: int) -> str:
        return self.uploader.generate_signed_url(object_key, expires_seconds)


def build_h3_asset_publication_preflight(
    plan: GenerationPlanEpisode,
    bundle: ApprovedAssetBundle,
    *,
    segment_id: str,
) -> H3AssetPublicationPreflight:
    segment = _find_h3_segment(plan, segment_id)
    try:
        readiness = assess_asset_readiness(
            bundle,
            _prepared_stub_not_allowed(),
            plan,
            stage="keyframe",
            segment_ids=[segment_id],
        )
    except AssertionError:
        # assess_asset_readiness needs PreparedEpisode for digest binding. The public
        # workflow calls the prepared-aware wrapper below; direct use must fail.
        raise H3AssetPublicationError(
            "use build_h3_asset_publication_preflight_for_episode with PreparedEpisode"
        )
    raise AssertionError("unreachable")


def build_h3_asset_publication_preflight_for_episode(
    prepared,
    plan: GenerationPlanEpisode,
    bundle: ApprovedAssetBundle,
    *,
    segment_id: str,
) -> H3AssetPublicationPreflight:
    segment = _find_h3_segment(plan, segment_id)
    try:
        readiness = assess_asset_readiness(
            bundle,
            prepared,
            plan,
            stage="keyframe",
            segment_ids=[segment_id],
        )
    except AssetBundleError as exc:
        raise H3AssetPublicationError(str(exc)) from exc
    if not readiness.ready:
        raise H3AssetPublicationError(
            "approved H3 master assets are not ready: "
            + "; ".join(item.message for item in readiness.errors)
        )
    bundle_by_id = {item.asset_id: item for item in bundle.assets}
    required_ids = [
        item
        for item in segment.reference_asset_ids
        if item.startswith(("ref_char_", "ref_loc_", "ref_prop_"))
    ]
    if not required_ids:
        raise H3AssetPublicationError("H3 segment has no planned reference images")
    if len(required_ids) > 9:
        raise H3AssetPublicationError("H3 segment exceeds nine reference images")
    items: list[H3PublicationItem] = []
    for order, asset_id in enumerate(required_ids):
        asset = bundle_by_id.get(asset_id)
        if asset is None:
            raise H3AssetPublicationError(f"asset bundle is missing {asset_id}")
        items.append(_publication_item(plan, segment, asset, order))
    return H3AssetPublicationPreflight.build_with_digest(
        generation_plan_digest=plan.content_digest,
        asset_bundle_digest=bundle.content_digest,
        segment_id=segment.segment_id,
        provider_route_id=segment.provider_route_id,
        items=items,
        external_storage_calls=0,
        valid=True,
    )


def publish_h3_assets(
    preflight: H3AssetPublicationPreflight,
    *,
    approval_digest: str,
    execute_upload: bool,
    publisher: H3AssetPublisher,
    expires_seconds: int = 1800,
    now: datetime | None = None,
) -> H3PublishedAssetManifest:
    if not execute_upload:
        raise H3AssetPublicationError("publishing requires execute_upload=True")
    if approval_digest != preflight.content_digest:
        raise H3AssetPublicationError("publication approval digest does not match preflight")
    if expires_seconds < 600 or expires_seconds > 7200:
        raise H3AssetPublicationError("signed URL expiry must be between 600 and 7200 seconds")
    if not publisher.is_configured:
        raise H3AssetPublicationError("OSS publisher is not configured")
    timestamp = now or datetime.now(timezone.utc)
    published: list[PublishedH3Asset] = []
    uploads = 0
    for item in preflight.items:
        _verify_local_item(item)
        object_key = publisher.object_key(item.object_relative_path)
        uploaded = False
        if not publisher.object_exists(object_key):
            returned = publisher.upload(item.local_path, item.object_relative_path)
            if returned != object_key:
                raise H3AssetPublicationError("publisher returned an unexpected object key")
            uploads += 1
            uploaded = True
        if not publisher.object_exists(object_key):
            raise H3AssetPublicationError(
                f"published object cannot be verified: {object_key}"
            )
        signed_url = publisher.sign_for_api(object_key, expires_seconds)
        _require_https_url(signed_url)
        published.append(
            PublishedH3Asset(
                asset_id=item.asset_id,
                local_sha256=item.local_sha256,
                object_key=object_key,
                signed_url=signed_url,
                signed_url_expires_at=timestamp + timedelta(seconds=expires_seconds),
                uploaded_in_this_run=uploaded,
            )
        )
    return H3PublishedAssetManifest.build_with_digest(
        preflight_digest=preflight.content_digest,
        generation_plan_digest=preflight.generation_plan_digest,
        asset_bundle_digest=preflight.asset_bundle_digest,
        segment_id=preflight.segment_id,
        assets=published,
        external_storage_uploads=uploads,
        external_storage_signatures=len(published),
        published_at=timestamp,
    )


def materialize_h3_canary_asset_manifest(
    preflight: H3AssetPublicationPreflight,
    published: H3PublishedAssetManifest,
    *,
    now: datetime | None = None,
    minimum_remaining_seconds: int = 300,
) -> H3CanaryAssetManifest:
    if published.preflight_digest != preflight.content_digest:
        raise H3AssetPublicationError("published assets belong to another preflight")
    if published.generation_plan_digest != preflight.generation_plan_digest:
        raise H3AssetPublicationError("published assets belong to another GenerationPlan")
    if published.asset_bundle_digest != preflight.asset_bundle_digest:
        raise H3AssetPublicationError("published assets belong to another AssetBundle")
    if published.segment_id != preflight.segment_id:
        raise H3AssetPublicationError("published assets belong to another segment")
    by_id = {item.asset_id: item for item in published.assets}
    if set(by_id) != {item.asset_id for item in preflight.items}:
        raise H3AssetPublicationError("published asset set differs from approved preflight")
    timestamp = now or datetime.now(timezone.utc)
    assets: list[H3CanaryAsset] = []
    for item in preflight.items:
        _verify_local_item(item)
        record = by_id[item.asset_id]
        if record.local_sha256 != item.local_sha256:
            raise H3AssetPublicationError(
                f"published hash changed for {item.asset_id}"
            )
        if record.signed_url_expires_at <= timestamp + timedelta(
            seconds=minimum_remaining_seconds
        ):
            raise H3AssetPublicationError(
                f"published URL is too close to expiry for {item.asset_id}"
            )
        _require_https_url(record.signed_url)
        assets.append(
            H3CanaryAsset(
                asset_id=item.asset_id,
                url=record.signed_url,
                sha256=item.local_sha256,
                size_bytes=item.size_bytes,
                aspect_ratio=item.width / item.height,
                width_px=item.width,
                height_px=item.height,
                media_format="png",
            )
        )
    return H3CanaryAssetManifest(segment_id=preflight.segment_id, assets=assets)


def _find_h3_segment(
    plan: GenerationPlanEpisode,
    segment_id: str,
) -> GenerationSegment:
    matches = [item for item in plan.segments if item.segment_id == segment_id]
    if len(matches) != 1:
        raise H3AssetPublicationError(f"unknown or duplicate segment: {segment_id}")
    segment = matches[0]
    if segment.provider_route_id not in _H3_ROUTES:
        raise H3AssetPublicationError(
            f"H3 asset publication cannot serve route {segment.provider_route_id}"
        )
    return segment


def _publication_item(
    plan: GenerationPlanEpisode,
    segment: GenerationSegment,
    asset: ApprovedReferenceAsset,
    order: int,
) -> H3PublicationItem:
    if asset.role not in {"character_master", "location_master", "prop_master"}:
        raise H3AssetPublicationError(
            f"H3 reference {asset.asset_id} has unsupported role {asset.role}"
        )
    if asset.approval_status != "approved":
        raise H3AssetPublicationError(f"H3 reference {asset.asset_id} is not approved")
    if asset.mime_type != "image/png" or not asset.asset_path or not asset.asset_sha256:
        raise H3AssetPublicationError(
            f"H3 reference {asset.asset_id} lacks approved PNG metadata"
        )
    path = Path(asset.asset_path).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise H3AssetPublicationError(f"approved asset is missing: {path}")
    if file_sha256(path) != asset.asset_sha256:
        raise H3AssetPublicationError(
            f"approved asset hash changed: {asset.asset_id}"
        )
    try:
        width, height = png_dimensions(path)
    except ApprovalError as exc:
        raise H3AssetPublicationError(str(exc)) from exc
    if (width, height) != (asset.width, asset.height):
        raise H3AssetPublicationError(
            f"approved asset dimensions changed: {asset.asset_id}"
        )
    relative = (
        f"jp-drama/h3/{plan.content_digest[7:19]}/{segment.segment_id}/"
        f"{asset.asset_id}_{asset.asset_sha256[7:23]}.png"
    )
    return H3PublicationItem(
        asset_id=asset.asset_id,
        role=asset.role,
        subject_id=asset.subject_id,
        order=order,
        local_path=str(path),
        local_sha256=asset.asset_sha256,
        size_bytes=path.stat().st_size,
        width=width,
        height=height,
        object_relative_path=relative,
    )


def _verify_local_item(item: H3PublicationItem) -> None:
    path = Path(item.local_path).resolve()
    if not path.is_file() or path.stat().st_size != item.size_bytes:
        raise H3AssetPublicationError(
            f"local approved asset size changed: {item.asset_id}"
        )
    if file_sha256(path) != item.local_sha256:
        raise H3AssetPublicationError(
            f"local approved asset hash changed: {item.asset_id}"
        )
    try:
        dimensions = png_dimensions(path)
    except ApprovalError as exc:
        raise H3AssetPublicationError(str(exc)) from exc
    if dimensions != (item.width, item.height):
        raise H3AssetPublicationError(
            f"local approved asset dimensions changed: {item.asset_id}"
        )


def _require_https_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise H3AssetPublicationError("H3 published reference must be a valid HTTPS URL")


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
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    ) + "\n"


def _prepared_stub_not_allowed():
    raise AssertionError("PreparedEpisode is required")
