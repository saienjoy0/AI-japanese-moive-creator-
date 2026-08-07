"""Prepare exactly one GenerationSegment for a paid MiniMax H3 Canary."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..assets.bundle import prepared_content_digest
from ..generation.compiler import segment_to_generation_spec
from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..preparation.models import PreparedEpisode
from .minimax_h3_adapter import MiniMaxH3Adapter
from .minimax_h3_executor import authoritative_h3_cost_usd
from .minimax_h3_models import H3ReferenceBundle, H3VideoGenerationRequest
from .provider_core import PreparedProviderRequest, ReferenceAsset, ShotGenerationSpec


class MiniMaxH3CanaryError(ValueError):
    """A generation segment cannot be executed by the H3 Canary safely."""


class H3CanaryAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    asset_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1)
    duration_seconds: float | None = Field(default=None, ge=0)
    fps: float | None = Field(default=None, ge=0)
    aspect_ratio: float | None = Field(default=None, gt=0)
    width_px: int | None = Field(default=None, ge=1)
    height_px: int | None = Field(default=None, ge=1)
    media_format: str = Field(min_length=1)
    codec: str | None = None


class H3CanaryAssetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    segment_id: str = Field(min_length=1)
    assets: list[H3CanaryAsset] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "H3CanaryAssetManifest":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class PreparedH3Canary(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    segment: GenerationSegment
    spec: ShotGenerationSpec
    prepared_request: PreparedProviderRequest
    h3_request: H3VideoGenerationRequest
    reference_bundle: H3ReferenceBundle
    authoritative_cost_usd: Decimal
    price_snapshot_id: str


def find_h3_segment(plan: GenerationPlanEpisode, segment_id: str) -> GenerationSegment:
    matches = [item for item in plan.segments if item.segment_id == segment_id]
    if len(matches) != 1:
        raise MiniMaxH3CanaryError(f"unknown or duplicate generation segment: {segment_id}")
    segment = matches[0]
    allowed_routes = {
        "minimax/h3-reference-av",
        "minimax/h3-first-frame",
        "minimax/h3-text",
    }
    if segment.provider_route_id not in allowed_routes:
        raise MiniMaxH3CanaryError(
            f"H3 Canary requires a registered MiniMax H3 route, not {segment.provider_route_id}"
        )
    return segment


def prepare_h3_canary(
    prepared_episode: PreparedEpisode,
    plan: GenerationPlanEpisode,
    *,
    segment_id: str,
    asset_manifest: H3CanaryAssetManifest,
    adapter: MiniMaxH3Adapter,
) -> PreparedH3Canary:
    if plan.source_prepared_episode_digest != prepared_content_digest(prepared_episode):
        raise MiniMaxH3CanaryError(
            "generation plan does not belong to the supplied PreparedEpisode"
        )
    segment = find_h3_segment(plan, segment_id)
    if segment.provider_route_id != adapter.route_id:
        raise MiniMaxH3CanaryError(
            f"segment route {segment.provider_route_id} does not match adapter {adapter.route_id}"
        )
    if asset_manifest.segment_id != segment.segment_id:
        raise MiniMaxH3CanaryError("asset manifest segment_id does not match selected segment")

    base_spec = segment_to_generation_spec(
        segment,
        plan.source_prepared_episode_digest,
        adapter.capabilities(),
    )
    by_id = {item.asset_id: item for item in asset_manifest.assets}
    if len(by_id) != len(asset_manifest.assets):
        raise MiniMaxH3CanaryError("asset manifest contains duplicate asset_id values")
    expected = {item.asset_id for item in base_spec.references}
    supplied = set(by_id)
    if supplied != expected:
        missing = sorted(expected - supplied)
        extra = sorted(supplied - expected)
        raise MiniMaxH3CanaryError(
            f"asset manifest does not exactly cover planned references; missing={missing}, extra={extra}"
        )
    materialized_references: list[ReferenceAsset] = []
    for reference in sorted(base_spec.references, key=lambda item: item.order):
        asset = by_id[reference.asset_id]
        materialized_references.append(
            reference.model_copy(
                update={
                    "uri": asset.url,
                    "sha256": asset.sha256,
                    "size_bytes": asset.size_bytes,
                    "duration_seconds": asset.duration_seconds,
                    "fps": asset.fps,
                    "aspect_ratio": asset.aspect_ratio,
                    "width_px": asset.width_px,
                    "height_px": asset.height_px,
                    "media_format": asset.media_format,
                    "codec": asset.codec,
                }
            )
        )
    spec = base_spec.model_copy(update={"references": materialized_references})
    report = adapter.validate(spec)
    if not report.valid:
        raise MiniMaxH3CanaryError(
            "H3 Canary provider validation failed: "
            + "; ".join(item.message for item in report.errors)
        )
    prepared_request = adapter.prepare(spec)
    h3_request = H3VideoGenerationRequest.model_validate(prepared_request.payload)
    bundle_payload = prepared_request.metadata.get("h3_reference_bundle")
    if not isinstance(bundle_payload, dict):
        raise MiniMaxH3CanaryError("prepared request is missing h3_reference_bundle")
    bundle = H3ReferenceBundle.model_validate(bundle_payload)
    try:
        bundle.require_valid(max_request_bytes=adapter.config.max_request_bytes)
    except ValueError as exc:
        raise MiniMaxH3CanaryError(f"H3 asset preflight failed: {exc}") from exc
    cost = authoritative_h3_cost_usd(adapter.config, h3_request, bundle)
    return PreparedH3Canary(
        segment=segment,
        spec=spec,
        prepared_request=prepared_request,
        h3_request=h3_request,
        reference_bundle=bundle,
        authoritative_cost_usd=cost,
        price_snapshot_id=f"minimax-h3-{adapter.config.price_snapshot_date}",
    )


def write_prepared_h3_canary(canary: PreparedH3Canary, path: str | Path) -> None:
    payload = {
        "segment_id": canary.segment.segment_id,
        "provider_route_id": canary.segment.provider_route_id,
        "request_fingerprint": canary.prepared_request.request_fingerprint,
        "authoritative_cost_usd": str(canary.authoritative_cost_usd),
        "price_snapshot_id": canary.price_snapshot_id,
        "prepared_request": canary.prepared_request.model_dump(mode="json", exclude_none=True),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )