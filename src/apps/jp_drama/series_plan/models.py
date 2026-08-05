"""Strict contracts for multi-episode storyboard generation plans and assets."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SERIES_PLAN_SCHEMA_VERSION = "2.0"
ASSET_CATALOG_SCHEMA_VERSION = "1.0"
SERIES_MANIFEST_SCHEMA_VERSION = "1.0.0"


class SeriesModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class SeriesSource(SeriesModel):
    title: str = Field(min_length=1)
    author: str = Field(min_length=1)
    source_kind: Literal["public_domain_literary_work"]
    input_method: str = Field(min_length=1)
    input_date: date
    adaptation_note: str = Field(min_length=1)


class SeriesProduction(SeriesModel):
    language: Literal["ja-JP"]
    format: Literal["three_episode_series"]
    episode_count: int = Field(ge=1)
    aspect_ratio: Literal["9:16"]
    timeline_fps: int = Field(ge=1)
    episode_duration_seconds: int = Field(gt=0)
    episode_frame_count: int = Field(gt=0)
    total_editorial_duration_seconds: int = Field(gt=0)
    total_editorial_frame_count: int = Field(gt=0)
    visual_style: str = Field(min_length=1)
    tone: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_totals(self) -> "SeriesProduction":
        if self.episode_duration_seconds * self.timeline_fps != self.episode_frame_count:
            raise ValueError("episode seconds and FPS must equal episode_frame_count")
        if self.episode_count * self.episode_duration_seconds != self.total_editorial_duration_seconds:
            raise ValueError("episode durations must equal total editorial duration")
        if self.episode_count * self.episode_frame_count != self.total_editorial_frame_count:
            raise ValueError("episode frames must equal total editorial frames")
        return self


class SeriesArcEntry(SeriesModel):
    title: str = Field(min_length=1)
    dramatic_question: str = Field(min_length=1)
    emotion_arc: str = Field(min_length=1)
    ending_hook: str | None = None


class PropStateContract(SeriesModel):
    description: str = Field(min_length=1)
    states: dict[str, str] = Field(min_length=1)


class SeriesContinuity(SeriesModel):
    character_ids: list[str] = Field(min_length=1)
    fixed_character_fields: list[str] = Field(min_length=1)
    locations: dict[str, str] = Field(min_length=1)
    prop_state_tracking: dict[str, PropStateContract] = Field(default_factory=dict)


class ProviderPolicy(SeriesModel):
    preferred_provider: str = Field(min_length=1)
    preferred_resolution: str = Field(min_length=1)
    fallback_providers: list[str] = Field(default_factory=list)
    native_audio_canary: bool
    subtitle_rendering: Literal["postprocess"]
    generated_text_in_video: bool
    requested_segment_duration_seconds: int = Field(ge=1)
    notes: list[str] = Field(default_factory=list)


class SeriesDialogue(SeriesModel):
    speaker: str = Field(min_length=1)
    mode: Literal[
        "spoken",
        "inner_monologue",
        "off_screen_then_on_screen",
        "voice_over",
        "memory_voice",
    ] = "spoken"
    text: str = Field(min_length=1)


class SeriesSegment(SeriesModel):
    segment_id: str = Field(pattern=r"^E[0-9]{2,3}-G[0-9]{2,3}$")
    title: str = Field(min_length=1)
    editorial_start_frame: int = Field(ge=0)
    editorial_end_frame: int = Field(gt=0)
    requested_duration_seconds: int = Field(ge=1)
    location_ids: list[str] = Field(min_length=1)
    character_ids: list[str] = Field(default_factory=list)
    background_character_ids: list[str] = Field(default_factory=list)
    prop_ids: list[str] = Field(default_factory=list)
    central_action: str = Field(min_length=1)
    emotion_start: str = Field(min_length=1)
    emotion_end: str = Field(min_length=1)
    end_state: str | None = None
    dialogue: list[SeriesDialogue] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_segment(self) -> "SeriesSegment":
        if self.editorial_end_frame <= self.editorial_start_frame:
            raise ValueError("segment end frame must be greater than start frame")
        for values, label in (
            (self.location_ids, "location_ids"),
            (self.character_ids, "character_ids"),
            (self.background_character_ids, "background_character_ids"),
            (self.prop_ids, "prop_ids"),
            (self.risk_tags, "risk_tags"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        return self

    @property
    def editorial_frame_count(self) -> int:
        return self.editorial_end_frame - self.editorial_start_frame

    @property
    def visual_character_ids(self) -> list[str]:
        return list(dict.fromkeys([*self.character_ids, *self.background_character_ids]))


class SeriesEpisode(SeriesModel):
    episode_id: str = Field(pattern=r"^E[0-9]{2,3}$")
    title: str = Field(min_length=1)
    editorial_duration_seconds: int = Field(gt=0)
    editorial_frame_count: int = Field(gt=0)
    segments: list[SeriesSegment] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_episode(self) -> "SeriesEpisode":
        expected = 0
        prefix = self.episode_id + "-G"
        for order, segment in enumerate(self.segments, start=1):
            if not segment.segment_id.startswith(prefix):
                raise ValueError("segment ID must match episode ID")
            if segment.editorial_start_frame != expected:
                raise ValueError("episode segments must be contiguous from frame zero")
            expected = segment.editorial_end_frame
            if segment.segment_id != f"{self.episode_id}-G{order:02d}":
                raise ValueError("segment IDs must be contiguous and ordered")
        if expected != self.editorial_frame_count:
            raise ValueError("segment frames must equal episode frame count")
        return self


class EditorialChecks(SeriesModel):
    episode_count: int = Field(ge=1)
    segment_count: int = Field(ge=1)
    all_episode_timelines_contiguous: bool
    each_episode_seconds: int = Field(gt=0)
    total_editorial_seconds: int = Field(gt=0)
    all_segments_seconds: int = Field(gt=0)
    all_segments_within_4_to_15_seconds: bool
    no_generated_text_required: bool
    subtitles_required: bool


class SeriesGenerationPlan(SeriesModel):
    schema_version: Literal[SERIES_PLAN_SCHEMA_VERSION]
    project_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source: SeriesSource
    production: SeriesProduction
    series_arc: dict[str, SeriesArcEntry] = Field(min_length=1)
    continuity_contract: SeriesContinuity
    provider_policy: ProviderPolicy
    episodes: list[SeriesEpisode] = Field(min_length=1)
    editorial_checks: EditorialChecks
    acceptance_criteria: list[str] = Field(min_length=1)
    manual_review_required: list[str] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_prop_states(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        continuity = dict(payload.get("continuity_contract") or {})
        raw_tracking = dict(continuity.get("prop_state_tracking") or {})
        normalized: dict[str, dict[str, object]] = {}
        for prop_id, raw in raw_tracking.items():
            if not isinstance(raw, dict):
                raise ValueError(f"prop state contract {prop_id} must be an object")
            raw_copy = dict(raw)
            description = raw_copy.pop("description", None)
            normalized[prop_id] = {
                "description": description,
                "states": raw_copy,
            }
        continuity["prop_state_tracking"] = normalized
        payload["continuity_contract"] = continuity
        return payload

    @model_validator(mode="after")
    def validate_series(self) -> "SeriesGenerationPlan":
        expected_ids = [f"E{index:02d}" for index in range(1, self.production.episode_count + 1)]
        actual_ids = [item.episode_id for item in self.episodes]
        if actual_ids != expected_ids:
            raise ValueError(f"episodes must be ordered exactly as {expected_ids}")
        if set(self.series_arc) != set(expected_ids):
            raise ValueError("series_arc must cover every episode exactly once")
        if any(item.editorial_duration_seconds != self.production.episode_duration_seconds for item in self.episodes):
            raise ValueError("every episode duration must match production contract")
        if any(item.editorial_frame_count != self.production.episode_frame_count for item in self.episodes):
            raise ValueError("every episode frame count must match production contract")
        segments = [item for episode in self.episodes for item in episode.segments]
        if len(segments) != self.editorial_checks.segment_count:
            raise ValueError("editorial segment count does not match episodes")
        if self.editorial_checks.episode_count != len(self.episodes):
            raise ValueError("editorial episode count does not match episodes")
        if not self.editorial_checks.all_episode_timelines_contiguous:
            raise ValueError("source contract reports non-contiguous episode timelines")
        if not self.editorial_checks.all_segments_within_4_to_15_seconds:
            raise ValueError("source contract reports unsupported provider segment duration")
        return self


class AssetCatalogEntry(SeriesModel):
    asset_id: str = Field(pattern=r"^[CSP][0-9]{2,3}$")
    kind: Literal["character", "scene", "prop"]
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    story_function: str | None = None
    prompt: str = Field(min_length=1)
    negative_prompt: str = Field(min_length=1)
    used_in_episode_ids: list[str] = Field(min_length=1)
    voice_identity_required: bool | None = None
    instance_rules: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind(self) -> "AssetCatalogEntry":
        expected = {"C": "character", "S": "scene", "P": "prop"}[self.asset_id[0]]
        if self.kind != expected:
            raise ValueError(f"asset {self.asset_id} prefix requires kind={expected}")
        if self.kind == "character" and self.voice_identity_required is None:
            raise ValueError("character asset requires voice_identity_required")
        if self.kind != "character" and self.voice_identity_required is not None:
            raise ValueError("only character assets may define voice_identity_required")
        if len(self.used_in_episode_ids) != len(set(self.used_in_episode_ids)):
            raise ValueError("used_in_episode_ids must be unique")
        return self


class SeriesAssetCatalog(SeriesModel):
    schema_version: Literal[ASSET_CATALOG_SCHEMA_VERSION]
    project_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_material_file: str = Field(min_length=1)
    visual_style: str = Field(min_length=1)
    continuity_rules: list[str] = Field(min_length=1)
    assets: list[AssetCatalogEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> "SeriesAssetCatalog":
        ids = [item.asset_id for item in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("asset catalogue IDs must be unique")
        return self

    @property
    def by_id(self) -> dict[str, AssetCatalogEntry]:
        return {item.asset_id: item for item in self.assets}


class SeriesEpisodeArtifact(SeriesModel):
    episode_id: str = Field(min_length=1)
    episode_number: int = Field(ge=1)
    prepared_episode_file: str = Field(min_length=1)
    prepared_episode_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generation_plan_files: dict[str, str] = Field(min_length=1)
    generation_plan_digests: dict[str, str] = Field(min_length=1)
    asset_bundle_files: dict[str, str] = Field(min_length=1)
    asset_bundle_digests: dict[str, str] = Field(min_length=1)


class SeriesProductionManifest(SeriesModel):
    schema_version: Literal[SERIES_MANIFEST_SCHEMA_VERSION] = SERIES_MANIFEST_SCHEMA_VERSION
    project_id: str = Field(min_length=1)
    series_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_title: str = Field(min_length=1)
    source_author: str = Field(min_length=1)
    rights_status: Literal["public_domain"]
    source_repository: str = Field(min_length=1)
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    series_plan_file: str = Field(min_length=1)
    series_plan_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_catalog_file: str = Field(min_length=1)
    asset_catalog_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    series_content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    episode_count: int = Field(ge=1)
    segment_count: int = Field(ge=1)
    timeline_fps: int = Field(ge=1)
    episode_frame_count: int = Field(gt=0)
    total_frame_count: int = Field(gt=0)
    episodes: list[SeriesEpisodeArtifact] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    manual_review_required: list[str] = Field(min_length=1)
    external_api_calls: Literal[0] = 0
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> "SeriesProductionManifest":
        if len(self.episodes) != self.episode_count:
            raise ValueError("manifest episode count does not match episode artifacts")
        if self.total_frame_count != self.episode_count * self.episode_frame_count:
            raise ValueError("manifest total frames do not match episode frames")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("series manifest content digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "SeriesProductionManifest":
        provisional = cls.model_construct(**data, content_digest="sha256:" + "0" * 64)
        return cls.model_validate({**data, "content_digest": provisional.compute_content_digest()})

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


def canonical_digest(*payloads: object) -> str:
    canonical = json.dumps(
        payloads,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
