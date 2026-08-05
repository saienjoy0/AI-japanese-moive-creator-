"""Hash-bound approval manifest for a paid MiniMax H3 segment request."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


H3_APPROVAL_SCHEMA_VERSION = "1.0.0"


class MiniMaxH3ApprovalError(RuntimeError):
    """The paid H3 request is not covered by the supplied approval manifest."""


class MiniMaxH3ApprovalManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    schema_version: Literal[H3_APPROVAL_SCHEMA_VERSION] = H3_APPROVAL_SCHEMA_VERSION
    segment_id: str = Field(min_length=1)
    request_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    reference_asset_hashes: list[str]
    model: str = Field(min_length=1)
    resolution: str = Field(min_length=1)
    duration: int = Field(ge=4, le=15)
    authoritative_cost_usd: Decimal = Field(ge=0)
    max_cost_usd: Decimal = Field(ge=0)
    price_snapshot_id: str = Field(min_length=1)
    approved_at: datetime


def create_h3_approval_manifest(
    *,
    segment_id: str,
    request_fingerprint: str,
    reference_asset_hashes: list[str],
    model: str,
    resolution: str,
    duration: int,
    authoritative_cost_usd: Decimal,
    max_cost_usd: Decimal,
    price_snapshot_id: str,
    output_path: str | Path,
) -> MiniMaxH3ApprovalManifest:
    if authoritative_cost_usd > max_cost_usd:
        raise MiniMaxH3ApprovalError("cannot approve an H3 request above max_cost_usd")
    manifest = MiniMaxH3ApprovalManifest(
        segment_id=segment_id,
        request_fingerprint=request_fingerprint,
        reference_asset_hashes=sorted(reference_asset_hashes),
        model=model,
        resolution=resolution,
        duration=duration,
        authoritative_cost_usd=authoritative_cost_usd,
        max_cost_usd=max_cost_usd,
        price_snapshot_id=price_snapshot_id,
        approved_at=datetime.now(timezone.utc),
    )
    _atomic_write(Path(output_path), manifest.model_dump_json(indent=2) + "\n")
    return manifest


def verify_h3_approval_manifest(
    path: str | Path,
    *,
    segment_id: str,
    request_fingerprint: str,
    reference_asset_hashes: list[str],
    model: str,
    resolution: str,
    duration: int,
    authoritative_cost_usd: Decimal,
    max_cost_usd: Decimal,
    price_snapshot_id: str,
) -> MiniMaxH3ApprovalManifest:
    try:
        manifest = MiniMaxH3ApprovalManifest.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise MiniMaxH3ApprovalError(f"cannot load H3 approval manifest: {exc}") from exc
    expected = {
        "segment_id": segment_id,
        "request_fingerprint": request_fingerprint,
        "reference_asset_hashes": sorted(reference_asset_hashes),
        "model": model,
        "resolution": resolution,
        "duration": duration,
        "authoritative_cost_usd": authoritative_cost_usd,
        "max_cost_usd": max_cost_usd,
        "price_snapshot_id": price_snapshot_id,
    }
    conflicts = [name for name, value in expected.items() if getattr(manifest, name) != value]
    if conflicts:
        raise MiniMaxH3ApprovalError(
            "H3 approval does not match the current paid request: " + ", ".join(conflicts)
        )
    return manifest


def _atomic_write(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
