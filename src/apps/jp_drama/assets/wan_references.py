"""Hash-bound approved master references for Wan first-frame generation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..preparation.models import PreparedEpisode
from ..rendering.approval import ApprovalError, png_dimensions
from ..rendering.ffmpeg import file_sha256
from .bundle import AssetBundleError, assess_asset_readiness
from .models import ApprovedAssetBundle, ApprovedReferenceAsset


WAN_MASTER_REFERENCE_SCHEMA_VERSION = "1.0.0"
_MASTER_ROLES = {"character_master", "location_master", "prop_master"}


class WanMasterReferenceError(RuntimeError):
    """Approved master images cannot safely drive a Wan first frame."""


class WanReferenceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class WanMasterReference(WanReferenceModel):
    asset_id: str = Field(min_length=1)
    role: Literal["character_master", "location_master", "prop_master"]
    subject_id: str = Field(min_length=1)
    order: int = Field(ge=0)
    asset_path: str = Field(min_length=1)
    asset_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    generated_by: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)


class WanMasterReferenceManifest(WanReferenceModel):
    schema_version: Literal[WAN_MASTER_REFERENCE_SCHEMA_VERSION] = (
        WAN_MASTER_REFERENCE_SCHEMA_VERSION
    )
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_id: str = Field(min_length=1)
    provider_route_id: Literal["wan/i2v"] = "wan/i2v"
    references: list[WanMasterReference] = Field(min_length=1, max_length=9)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> "WanMasterReferenceManifest":
        ids = [item.asset_id for item in self.references]
        if len(ids) != len(set(ids)):
            raise ValueError("Wan master reference IDs must be unique")
        orders = [item.order for item in self.references]
        if orders != list(range(len(self.references))):
            raise ValueError("Wan master reference order must be contiguous")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("Wan master reference manifest digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "WanMasterReferenceManifest":
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
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"

    @property
    def asset_ids(self) -> list[str]:
        return [item.asset_id for item in self.references]

    @property
    def asset_hashes(self) -> list[str]:
        return [item.asset_sha256 for item in self.references]

    @property
    def asset_paths(self) -> list[str]:
        return [item.asset_path for item in self.references]


def build_wan_master_reference_manifest(
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    bundle: ApprovedAssetBundle,
    *,
    segment_id: str,
) -> WanMasterReferenceManifest:
    segment = _find_wan_segment(plan, segment_id)
    try:
        readiness = assess_asset_readiness(
            bundle,
            prepared,
            plan,
            stage="keyframe",
            segment_ids=[segment_id],
        )
    except AssetBundleError as exc:
        raise WanMasterReferenceError(str(exc)) from exc
    if not readiness.ready:
        raise WanMasterReferenceError(
            "approved Wan master assets are not ready: "
            + "; ".join(item.message for item in readiness.errors)
        )

    by_id = {item.asset_id: item for item in bundle.assets}
    planned_ids = [
        item
        for item in segment.reference_asset_ids
        if item.startswith(("ref_char_", "ref_loc_", "ref_prop_"))
    ]
    if not planned_ids:
        raise WanMasterReferenceError("Wan segment has no planned master references")
    if len(planned_ids) != len(set(planned_ids)):
        raise WanMasterReferenceError("Wan segment contains duplicate master references")
    if len(planned_ids) > 9:
        raise WanMasterReferenceError("Wan first frame exceeds nine master references")

    references: list[WanMasterReference] = []
    for order, asset_id in enumerate(planned_ids):
        asset = by_id.get(asset_id)
        if asset is None:
            raise WanMasterReferenceError(f"asset bundle is missing {asset_id}")
        references.append(_materialize_reference(asset, order))
    return WanMasterReferenceManifest.build_with_digest(
        generation_plan_digest=plan.content_digest,
        asset_bundle_digest=bundle.content_digest,
        segment_id=segment.segment_id,
        provider_route_id="wan/i2v",
        references=references,
    )


def verify_wan_master_reference_manifest(
    manifest: WanMasterReferenceManifest,
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    bundle: ApprovedAssetBundle,
    *,
    segment_id: str,
) -> WanMasterReferenceManifest:
    rebuilt = build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    if rebuilt.content_digest != manifest.content_digest:
        raise WanMasterReferenceError(
            "Wan master reference files, plan, or AssetBundle changed"
        )
    return rebuilt


def load_wan_master_reference_manifest(
    path: str | Path,
) -> WanMasterReferenceManifest:
    source = Path(path).resolve()
    try:
        return WanMasterReferenceManifest.model_validate_json(
            source.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise WanMasterReferenceError(
            f"cannot load Wan master reference manifest {source}: {exc}"
        ) from exc


def write_wan_master_reference_manifest(
    manifest: WanMasterReferenceManifest,
    path: str | Path,
) -> Path:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(manifest.to_canonical_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def _find_wan_segment(
    plan: GenerationPlanEpisode,
    segment_id: str,
) -> GenerationSegment:
    matches = [item for item in plan.segments if item.segment_id == segment_id]
    if len(matches) != 1:
        raise WanMasterReferenceError(f"unknown or duplicate segment: {segment_id}")
    segment = matches[0]
    if segment.provider_route_id != "wan/i2v":
        raise WanMasterReferenceError(
            f"Wan master references cannot serve route {segment.provider_route_id}"
        )
    return segment


def _materialize_reference(
    asset: ApprovedReferenceAsset,
    order: int,
) -> WanMasterReference:
    if asset.role not in _MASTER_ROLES:
        raise WanMasterReferenceError(
            f"Wan reference {asset.asset_id} has unsupported role {asset.role}"
        )
    if asset.approval_status != "approved":
        raise WanMasterReferenceError(f"Wan reference {asset.asset_id} is not approved")
    if (
        asset.mime_type != "image/png"
        or not asset.asset_path
        or not asset.asset_sha256
        or not asset.generated_by
        or not asset.operation_id
    ):
        raise WanMasterReferenceError(
            f"Wan reference {asset.asset_id} lacks approved PNG lineage"
        )
    path = Path(asset.asset_path).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise WanMasterReferenceError(f"approved Wan reference is missing: {path}")
    if file_sha256(path) != asset.asset_sha256:
        raise WanMasterReferenceError(
            f"approved Wan reference hash changed: {asset.asset_id}"
        )
    try:
        width, height = png_dimensions(path)
    except ApprovalError as exc:
        raise WanMasterReferenceError(str(exc)) from exc
    if (width, height) != (asset.width, asset.height):
        raise WanMasterReferenceError(
            f"approved Wan reference dimensions changed: {asset.asset_id}"
        )
    return WanMasterReference(
        asset_id=asset.asset_id,
        role=asset.role,
        subject_id=asset.subject_id,
        order=order,
        asset_path=str(path),
        asset_sha256=asset.asset_sha256,
        width=width,
        height=height,
        generated_by=asset.generated_by,
        operation_id=asset.operation_id,
    )
