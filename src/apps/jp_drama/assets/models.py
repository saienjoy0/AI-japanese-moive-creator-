"""Strict approval contracts for Japanese-drama reference assets and voices."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ASSET_BUNDLE_SCHEMA_VERSION = "1.0.0"
AssetRole = Literal[
    "character_master",
    "character_angle",
    "costume",
    "location_master",
    "prop_master",
    "first_frame",
    "last_frame",
    "voice_reference",
]
ApprovalStatus = Literal["pending", "approved", "rejected"]
AssetReadinessStage = Literal["preflight", "keyframe", "approve", "render", "full_episode"]


class AssetModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class ApprovedReferenceAsset(AssetModel):
    asset_id: str = Field(min_length=1)
    role: AssetRole
    subject_id: str = Field(min_length=1)
    continuity_group_id: str | None = None
    required_for_segment_ids: list[str] = Field(default_factory=list)

    approval_status: ApprovalStatus = "pending"
    asset_path: str | None = None
    asset_sha256: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    mime_type: Literal["image/png", "audio/wav"] | None = None
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, gt=0)

    approval_manifest_path: str | None = None
    verified_against_asset_ids: list[str] = Field(default_factory=list)
    generated_by: str | None = None
    operation_id: str | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def validate_approval_state(self) -> "ApprovedReferenceAsset":
        if len(self.required_for_segment_ids) != len(set(self.required_for_segment_ids)):
            raise ValueError("required_for_segment_ids must be unique")
        if len(self.verified_against_asset_ids) != len(
            set(self.verified_against_asset_ids)
        ):
            raise ValueError("verified_against_asset_ids must be unique")

        if self.approval_status == "approved":
            required = {
                "asset_path": self.asset_path,
                "asset_sha256": self.asset_sha256,
                "mime_type": self.mime_type,
                "approved_at": self.approved_at,
                "approved_by": self.approved_by,
            }
            missing = [key for key, value in required.items() if value in {None, ""}]
            if missing:
                raise ValueError(
                    "approved asset is missing required fields: " + ", ".join(missing)
                )
            if self.mime_type == "image/png" and (
                self.width is None or self.height is None
            ):
                raise ValueError("approved PNG asset requires width and height")
            if self.mime_type == "audio/wav" and self.duration_seconds is None:
                raise ValueError("approved WAV asset requires duration_seconds")
            if self.role == "first_frame" and not self.approval_manifest_path:
                raise ValueError(
                    "approved first_frame requires the hash-bound keyframe approval manifest"
                )
        elif self.approval_status == "rejected" and not self.rejection_reason:
            raise ValueError("rejected asset requires rejection_reason")
        return self


class VoiceIdentityProfile(AssetModel):
    profile_id: str = Field(min_length=1)
    character_seed_id: str = Field(min_length=1)
    source_character_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    voice_id: str | None = None
    language: str = Field(default="ja-JP", min_length=2)
    speaking_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    pronunciation_dictionary: dict[str, str] = Field(default_factory=dict)
    reference_audio_asset_id: str | None = None
    allow_shared_voice: bool = False
    approval_status: ApprovalStatus = "pending"
    approved_at: datetime | None = None
    approved_by: str | None = None
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def validate_approval_state(self) -> "VoiceIdentityProfile":
        if self.approval_status == "approved":
            if not self.voice_id:
                raise ValueError("approved voice profile requires voice_id")
            if self.approved_at is None or not self.approved_by:
                raise ValueError(
                    "approved voice profile requires approved_at and approved_by"
                )
        elif self.approval_status == "rejected" and not self.rejection_reason:
            raise ValueError("rejected voice profile requires rejection_reason")
        return self


class ApprovedAssetBundle(AssetModel):
    schema_version: Literal[ASSET_BUNDLE_SCHEMA_VERSION] = (
        ASSET_BUNDLE_SCHEMA_VERSION
    )
    bundle_id: str = Field(min_length=1)
    source_episode_id: str = Field(min_length=1)
    source_prepared_episode_digest: str = Field(
        pattern=r"^sha256:[a-f0-9]{64}$"
    )
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    assets: list[ApprovedReferenceAsset] = Field(default_factory=list)
    voice_profiles: list[VoiceIdentityProfile] = Field(default_factory=list)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_bundle(self) -> "ApprovedAssetBundle":
        asset_ids = [item.asset_id for item in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset bundle IDs must be unique")
        profiles = [item.profile_id for item in self.voice_profiles]
        if len(profiles) != len(set(profiles)):
            raise ValueError("voice profile IDs must be unique")
        characters = [item.character_seed_id for item in self.voice_profiles]
        if len(characters) != len(set(characters)):
            raise ValueError("each character may have at most one voice profile")

        approved_voice_owners: dict[tuple[str, str], str] = {}
        for profile in self.voice_profiles:
            if profile.approval_status != "approved" or not profile.voice_id:
                continue
            key = (profile.provider, profile.voice_id)
            existing = approved_voice_owners.get(key)
            if (
                existing is not None
                and existing != profile.character_seed_id
                and not profile.allow_shared_voice
            ):
                raise ValueError(
                    f"approved voice {profile.provider}/{profile.voice_id} is shared by "
                    f"{existing} and {profile.character_seed_id}"
                )
            approved_voice_owners[key] = profile.character_seed_id

        known_assets = set(asset_ids)
        for asset in self.assets:
            unknown = set(asset.verified_against_asset_ids) - known_assets
            if unknown:
                raise ValueError(
                    f"asset {asset.asset_id} verification references unknown assets: "
                    f"{sorted(unknown)}"
                )
        for profile in self.voice_profiles:
            if (
                profile.reference_audio_asset_id is not None
                and profile.reference_audio_asset_id not in known_assets
            ):
                raise ValueError(
                    f"voice profile {profile.profile_id} references unknown audio asset"
                )

        if self.content_digest != self.compute_content_digest():
            raise ValueError("asset bundle content_digest does not match canonical content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "ApprovedAssetBundle":
        provisional = cls.model_construct(
            **data,
            content_digest="sha256:" + "0" * 64,
        )
        digest = provisional.compute_content_digest()
        return cls.model_validate({**data, "content_digest": digest})

    def _content_payload(self) -> dict:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return payload

    def compute_content_digest(self) -> str:
        canonical = json.dumps(
            self._content_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def to_canonical_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )


class AssetReadinessIssue(AssetModel):
    code: str = Field(min_length=1)
    severity: Literal["error", "warning"]
    message: str = Field(min_length=1)
    asset_id: str | None = None
    character_seed_id: str | None = None
    segment_id: str | None = None


class AssetReadinessReport(AssetModel):
    stage: AssetReadinessStage
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    selected_segment_ids: list[str] = Field(default_factory=list)
    required_asset_ids: list[str] = Field(default_factory=list)
    required_voice_character_ids: list[str] = Field(default_factory=list)
    ready: bool
    errors: list[AssetReadinessIssue] = Field(default_factory=list)
    warnings: list[AssetReadinessIssue] = Field(default_factory=list)
