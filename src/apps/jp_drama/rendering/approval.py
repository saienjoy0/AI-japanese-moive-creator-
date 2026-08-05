"""Hash-bound human approval manifests for provider keyframes."""

from __future__ import annotations

import json
import os
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ffmpeg import file_sha256


APPROVAL_SCHEMA_VERSION = "1.0.0"
MAX_PORTRAIT_RATIO_ERROR = 0.01


class ApprovalError(RuntimeError):
    """An approval manifest or its image asset is invalid."""


class ApprovedKeyframeManifest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )

    schema_version: Literal[APPROVAL_SCHEMA_VERSION] = APPROVAL_SCHEMA_VERSION
    shot_id: str = Field(min_length=1)
    asset_path: str = Field(min_length=1)
    asset_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    mime_type: Literal["image/png"] = "image/png"
    generated_by: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    approved: Literal[True] = True
    approved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    master_reference_manifest_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    master_reference_asset_ids: list[str] = Field(default_factory=list)
    master_reference_asset_hashes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_master_lineage(self) -> "ApprovedKeyframeManifest":
        has_digest = self.master_reference_manifest_digest is not None
        has_ids = bool(self.master_reference_asset_ids)
        has_hashes = bool(self.master_reference_asset_hashes)
        if has_digest:
            if not has_ids or not has_hashes:
                raise ValueError(
                    "master-reference approval requires asset IDs and hashes"
                )
            if len(self.master_reference_asset_ids) != len(
                self.master_reference_asset_hashes
            ):
                raise ValueError(
                    "master-reference asset IDs and hashes must have equal length"
                )
            if len(self.master_reference_asset_ids) != len(
                set(self.master_reference_asset_ids)
            ):
                raise ValueError("master-reference asset IDs must be unique")
            for digest in self.master_reference_asset_hashes:
                if not digest.startswith("sha256:") or len(digest) != 71:
                    raise ValueError("master-reference asset hash is invalid")
        elif has_ids or has_hashes:
            raise ValueError(
                "master-reference asset lineage requires manifest digest"
            )
        return self

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"


def png_dimensions(path: str | Path) -> tuple[int, int]:
    image = Path(path)
    with image.open("rb") as handle:
        signature = handle.read(8)
        if signature != b"\x89PNG\r\n\x1a\n":
            raise ApprovalError(f"approved keyframe is not a PNG: {image}")
        length_bytes = handle.read(4)
        chunk_type = handle.read(4)
        if len(length_bytes) != 4 or chunk_type != b"IHDR":
            raise ApprovalError(f"approved keyframe has no valid PNG IHDR: {image}")
        length = struct.unpack(">I", length_bytes)[0]
        data = handle.read(length)
        if len(data) < 8:
            raise ApprovalError(f"approved keyframe PNG IHDR is truncated: {image}")
        width, height = struct.unpack(">II", data[:8])
    if width <= 0 or height <= 0:
        raise ApprovalError(f"approved keyframe has invalid dimensions: {width}x{height}")
    return width, height


def _validate_portrait_ratio(width: int, height: int) -> None:
    actual = width / height
    target = 9 / 16
    relative_error = abs(actual - target) / target
    if relative_error > MAX_PORTRAIT_RATIO_ERROR:
        raise ApprovalError(
            f"approved keyframe must be 9:16 portrait media, got {width}x{height}"
        )


def create_approval_manifest(
    *,
    shot_id: str,
    asset_path: str | Path,
    generated_by: str,
    operation_id: str,
    output_path: str | Path,
    master_reference_manifest_digest: str | None = None,
    master_reference_asset_ids: list[str] | None = None,
    master_reference_asset_hashes: list[str] | None = None,
) -> ApprovedKeyframeManifest:
    asset = Path(asset_path).resolve()
    if not asset.is_file() or asset.stat().st_size == 0:
        raise ApprovalError(f"approved keyframe does not exist or is empty: {asset}")
    width, height = png_dimensions(asset)
    _validate_portrait_ratio(width, height)
    manifest = ApprovedKeyframeManifest(
        shot_id=shot_id,
        asset_path=str(asset),
        asset_sha256=file_sha256(asset),
        width=width,
        height=height,
        generated_by=generated_by,
        operation_id=operation_id,
        master_reference_manifest_digest=master_reference_manifest_digest,
        master_reference_asset_ids=list(master_reference_asset_ids or []),
        master_reference_asset_hashes=list(master_reference_asset_hashes or []),
    )
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(manifest.to_canonical_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return manifest


def load_and_verify_approval(
    path: str | Path,
    *,
    expected_shot_id: str,
    expected_generated_by: str | None = None,
    expected_master_reference_manifest_digest: str | None = None,
    expected_master_reference_asset_ids: list[str] | None = None,
    expected_master_reference_asset_hashes: list[str] | None = None,
) -> tuple[ApprovedKeyframeManifest, Path]:
    manifest_path = Path(path).resolve()
    try:
        manifest = ApprovedKeyframeManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ApprovalError(f"cannot load approval manifest: {exc}") from exc

    if manifest.shot_id != expected_shot_id:
        raise ApprovalError(
            f"approval belongs to shot {manifest.shot_id}, expected {expected_shot_id}"
        )
    if expected_generated_by and manifest.generated_by != expected_generated_by:
        raise ApprovalError(
            f"approval provider mismatch: {manifest.generated_by} != {expected_generated_by}"
        )
    if (
        expected_master_reference_manifest_digest is not None
        and manifest.master_reference_manifest_digest
        != expected_master_reference_manifest_digest
    ):
        raise ApprovalError(
            "approval master-reference manifest digest does not match"
        )
    if expected_master_reference_asset_ids is not None:
        if manifest.master_reference_asset_ids != list(
            expected_master_reference_asset_ids
        ):
            raise ApprovalError("approval master-reference asset IDs do not match")
    if expected_master_reference_asset_hashes is not None:
        if manifest.master_reference_asset_hashes != list(
            expected_master_reference_asset_hashes
        ):
            raise ApprovalError("approval master-reference asset hashes do not match")

    asset = Path(manifest.asset_path).resolve()
    if not asset.is_file() or asset.stat().st_size == 0:
        raise ApprovalError(f"approved keyframe is missing: {asset}")
    width, height = png_dimensions(asset)
    _validate_portrait_ratio(width, height)
    if (width, height) != (manifest.width, manifest.height):
        raise ApprovalError("approved keyframe dimensions changed after approval")
    if file_sha256(asset) != manifest.asset_sha256:
        raise ApprovalError("approved keyframe hash changed after approval")
    return manifest, asset
