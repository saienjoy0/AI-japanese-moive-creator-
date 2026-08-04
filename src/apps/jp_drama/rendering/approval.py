"""Hash-bound human approval manifests for provider keyframes."""

from __future__ import annotations

import json
import os
import struct
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .ffmpeg import file_sha256


APPROVAL_SCHEMA_VERSION = "1.0.0"


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
    asset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    mime_type: Literal["image/png"] = "image/png"
    generated_by: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    approved: Literal[True] = True
    approved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

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


def create_approval_manifest(
    *,
    shot_id: str,
    asset_path: str | Path,
    generated_by: str,
    operation_id: str,
    output_path: str | Path,
) -> ApprovedKeyframeManifest:
    asset = Path(asset_path).resolve()
    if not asset.is_file() or asset.stat().st_size == 0:
        raise ApprovalError(f"approved keyframe does not exist or is empty: {asset}")
    width, height = png_dimensions(asset)
    if width * 16 != height * 9:
        raise ApprovalError(
            f"approved keyframe must be exact 9:16 portrait media, got {width}x{height}"
        )
    manifest = ApprovedKeyframeManifest(
        shot_id=shot_id,
        asset_path=str(asset),
        asset_sha256=file_sha256(asset),
        width=width,
        height=height,
        generated_by=generated_by,
        operation_id=operation_id,
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

    asset = Path(manifest.asset_path).resolve()
    if not asset.is_file() or asset.stat().st_size == 0:
        raise ApprovalError(f"approved keyframe is missing: {asset}")
    width, height = png_dimensions(asset)
    if (width, height) != (manifest.width, manifest.height):
        raise ApprovalError("approved keyframe dimensions changed after approval")
    if file_sha256(asset) != manifest.asset_sha256:
        raise ApprovalError("approved keyframe hash changed after approval")
    return manifest, asset
