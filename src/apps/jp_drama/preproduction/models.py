"""Strict zero-call contracts for production preparation before media generation."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PREPRODUCTION_SCHEMA_VERSION = "1.0.0"
PreparationStatus = Literal["pending", "ready", "blocked", "review_required"]


class PreproductionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class BundleAssetBinding(PreproductionModel):
    episode_id: str = Field(min_length=1)
    route: Literal["h3", "wan", "seedance"]
    bundle_file: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)


class AssetCreationRequirement(PreproductionModel):
    source_asset_id: str = Field(min_length=1)
    kind: Literal["character", "scene", "prop"]
    approval_role: Literal["character_master", "location_master", "prop_master"]
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    story_function: str | None = None
    prompt: str = Field(min_length=1)
    negative_prompt: str = Field(min_length=1)
    used_episode_ids: list[str] = Field(min_length=1)
    used_segment_ids: list[str] = Field(min_length=1)
    instance_rules: dict[str, str] = Field(default_factory=dict)
    voice_identity_required: bool = False
    bundle_bindings: list[BundleAssetBinding] = Field(min_length=1)
    base_master_image_required: Literal[True] = True
    variant_review_required: bool = False
    status: Literal["pending"] = "pending"

    @model_validator(mode="after")
    def validate_unique_usage(self) -> "AssetCreationRequirement":
        for values, label in (
            (self.used_episode_ids, "used_episode_ids"),
            (self.used_segment_ids, "used_segment_ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        keys = [
            (item.episode_id, item.route, item.bundle_file, item.asset_id)
            for item in self.bundle_bindings
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("bundle bindings must be unique")
        return self


class VoiceCreationRequirement(PreproductionModel):
    source_character_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    language: Literal["ja-JP"] = "ja-JP"
    used_episode_ids: list[str] = Field(min_length=1)
    used_segment_ids: list[str] = Field(min_length=1)
    bundle_files: list[str] = Field(min_length=1)
    profile_ids: list[str] = Field(min_length=1)
    distinct_voice_required: Literal[True] = True
    status: Literal["pending"] = "pending"


class FirstFrameRequirement(PreproductionModel):
    episode_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    wan_plan_file: str = Field(min_length=1)
    wan_pending_bundle_file: str = Field(min_length=1)
    wan_approved_master_bundle_file: str = Field(min_length=1)
    master_reference_asset_ids: list[str] = Field(min_length=1, max_length=9)
    master_reference_manifest_file: str = Field(min_length=1)
    preflight_report_file: str = Field(min_length=1)
    keyframe_file: str = Field(min_length=1)
    keyframe_approval_file: str = Field(min_length=1)
    registered_bundle_file: str = Field(min_length=1)
    prepare_command: str = Field(min_length=1)
    keyframe_preflight_command: str = Field(min_length=1)
    keyframe_paid_command_template: str = Field(min_length=1)
    keyframe_approve_command: str = Field(min_length=1)
    register_command: str = Field(min_length=1)
    status: Literal["blocked"] = "blocked"
    blocker_codes: list[str] = Field(min_length=1)


class RouteCostSummary(PreproductionModel):
    reference_image_calls: int = Field(ge=0)
    video_calls: int = Field(ge=0)
    tts_calls: int = Field(ge=0)
    native_audio_calls: int = Field(ge=0)
    expected_calls: int = Field(ge=0)
    hard_maximum_calls: int = Field(ge=0)
    totals_by_currency: dict[str, Decimal] = Field(default_factory=dict)
    unknown_cost_components: list[str] = Field(default_factory=list)
    pricing_snapshot_dates: list[str] = Field(default_factory=list)


class ProviderRouteSummary(PreproductionModel):
    episode_id: str = Field(min_length=1)
    route: Literal["h3", "wan", "seedance"]
    provider_route_id: str = Field(min_length=1)
    generation_plan_file: str = Field(min_length=1)
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    pending_asset_bundle_file: str = Field(min_length=1)
    pending_asset_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_ids: list[str] = Field(min_length=1)
    planning_ready: bool
    execution_route_ready: bool
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    cost: RouteCostSummary


class CanaryEpisodeDecision(PreproductionModel):
    episode_id: str = Field(min_length=1)
    selection_policy_id: str = Field(min_length=1)
    selected_segment_id: str | None = None
    eligible_segment_ids: list[str] = Field(default_factory=list)
    rejected_segments: list[dict[str, object]] = Field(default_factory=list)


class CanaryRecommendation(PreproductionModel):
    route: Literal["wan"] = "wan"
    recommended_episode_id: str | None = None
    recommended_segment_id: str | None = None
    episode_decisions: list[CanaryEpisodeDecision] = Field(min_length=1)
    recommendation_ready: bool
    reason: str = Field(min_length=1)


class PreproductionBlocker(PreproductionModel):
    code: str = Field(min_length=1)
    scope: Literal["series", "asset", "voice", "first_frame", "provider"]
    message: str = Field(min_length=1)
    item_ids: list[str] = Field(default_factory=list)
    resolved: Literal[False] = False


class PreproductionPackageManifest(PreproductionModel):
    schema_version: Literal[PREPRODUCTION_SCHEMA_VERSION] = PREPRODUCTION_SCHEMA_VERSION
    package_id: str = Field(min_length=1)
    source_series_manifest_file: str = Field(min_length=1)
    source_series_manifest_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_series_content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_repository: str = Field(min_length=1)
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    title: str = Field(min_length=1)
    episode_count: int = Field(ge=1)
    segment_count: int = Field(ge=1)
    base_master_asset_count: int = Field(ge=1)
    variant_review_asset_count: int = Field(ge=0)
    voice_identity_count: int = Field(ge=0)
    first_frame_count: int = Field(ge=1)
    provider_route_count: int = Field(ge=1)
    contract_ready: Literal[True] = True
    provider_plans_ready: bool
    master_assets_ready: Literal[False] = False
    voices_ready: Literal[False] = False
    first_frames_ready: Literal[False] = False
    video_generation_ready: Literal[False] = False
    files: dict[str, str] = Field(min_length=1)
    blockers: list[PreproductionBlocker] = Field(min_length=1)
    external_api_calls: Literal[0] = 0
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> "PreproductionPackageManifest":
        if self.first_frame_count != self.segment_count:
            raise ValueError("every segment must have exactly one first-frame slot")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("preproduction package digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "PreproductionPackageManifest":
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
