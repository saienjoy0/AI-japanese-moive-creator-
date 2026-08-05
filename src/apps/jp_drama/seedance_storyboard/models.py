"""Strict contracts for Seedance2 Storyboard Generator artifacts.

The upstream project is Markdown-first. These models preserve its C/S/P asset
catalogue, upload slots, timed storyboard, sound instructions, ending frame,
and continuation semantics without rewriting the creative rules.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SEEDANCE_STORYBOARD_SCHEMA_VERSION = "1.0.0"
AssetKind = Literal["character", "scene", "prop"]


class StoryboardModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class UpstreamProvenance(StoryboardModel):
    repository: str = "liangdabiao/Seedance2-Storyboard-Generator"
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    skill_path: str = ".claude/skills/seedance-storyboard-generator/SKILL.md"
    usage_note: str = (
        "Upstream README states that the project content is for learning and reference. "
        "Keep attribution and review redistribution rights before commercial distribution."
    )


class StoryboardAsset(StoryboardModel):
    asset_id: str = Field(pattern=r"^[CSP][0-9]{2,3}(?:[._-][A-Za-z0-9]+)?$")
    kind: AssetKind
    name: str = Field(min_length=1, max_length=500)
    prompt: str = Field(min_length=1, max_length=20_000)
    story_function: str | None = Field(default=None, max_length=2000)
    used_in_episode_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_prefix(self) -> "StoryboardAsset":
        expected = {"C": "character", "S": "scene", "P": "prop"}[self.asset_id[0]]
        if self.kind != expected:
            raise ValueError(
                f"asset {self.asset_id} prefix requires kind={expected}, got {self.kind}"
            )
        if len(self.used_in_episode_ids) != len(set(self.used_in_episode_ids)):
            raise ValueError("used_in_episode_ids must be unique")
        return self


class UploadSlot(StoryboardModel):
    slot_name: str = Field(min_length=1, max_length=100)
    asset_id: str = Field(pattern=r"^[CSP][0-9]{2,3}(?:[._-][A-Za-z0-9]+)?$")
    description: str = Field(min_length=1, max_length=2000)


class TimelineBeat(StoryboardModel):
    order: int = Field(ge=1)
    start_seconds: Decimal = Field(ge=0)
    end_seconds: Decimal = Field(gt=0)
    text: str = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_range(self) -> "TimelineBeat":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("timeline beat end must be after start")
        return self


class SeedanceStoryboardEpisode(StoryboardModel):
    episode_id: str = Field(pattern=r"^E[0-9]{1,3}$")
    title: str = Field(min_length=1, max_length=500)
    style_prompt: str = Field(min_length=1, max_length=5000)
    upload_slots: list[UploadSlot] = Field(default_factory=list, max_length=9)
    timeline: list[TimelineBeat] = Field(min_length=1)
    sound_prompt: str | None = Field(default=None, max_length=10_000)
    reference_prompt: str | None = Field(default=None, max_length=10_000)
    ending_frame: str = Field(min_length=1, max_length=10_000)
    continuation_source: str | None = Field(default=None, max_length=100)
    continuation_seconds: Decimal | None = Field(default=None, gt=0)
    raw_prompt: str = Field(min_length=1, max_length=50_000)

    @model_validator(mode="after")
    def validate_episode(self) -> "SeedanceStoryboardEpisode":
        slot_names = [item.slot_name for item in self.upload_slots]
        if len(slot_names) != len(set(slot_names)):
            raise ValueError(f"episode {self.episode_id} upload slot names must be unique")
        slot_assets = [item.asset_id for item in self.upload_slots]
        if len(slot_assets) != len(set(slot_assets)):
            raise ValueError(f"episode {self.episode_id} upload assets must be unique")

        expected_order = list(range(1, len(self.timeline) + 1))
        actual_order = [item.order for item in self.timeline]
        if actual_order != expected_order:
            raise ValueError(
                f"episode {self.episode_id} timeline order must be contiguous"
            )

        tolerance = Decimal("0.001")
        first = self.timeline[0]
        if first.start_seconds > tolerance:
            raise ValueError(
                f"episode {self.episode_id} timeline must start at zero seconds"
            )
        previous_end = first.end_seconds
        for beat in self.timeline[1:]:
            if abs(beat.start_seconds - previous_end) > tolerance:
                raise ValueError(
                    f"episode {self.episode_id} timeline has a gap or overlap before "
                    f"beat {beat.order}"
                )
            previous_end = beat.end_seconds
        if previous_end > Decimal("15") + tolerance:
            raise ValueError(
                f"episode {self.episode_id} exceeds the upstream 15-second segment limit"
            )
        if (self.continuation_source is None) != (self.continuation_seconds is None):
            raise ValueError(
                "continuation_source and continuation_seconds must be configured together"
            )
        return self

    @property
    def duration_seconds(self) -> Decimal:
        return self.timeline[-1].end_seconds


class StoryboardImportIssue(StoryboardModel):
    code: str = Field(min_length=1, max_length=200)
    severity: Literal["error", "warning"]
    message: str = Field(min_length=1, max_length=4000)
    episode_id: str | None = None
    asset_id: str | None = None


class SeedanceStoryboardPackage(StoryboardModel):
    schema_version: Literal[SEEDANCE_STORYBOARD_SCHEMA_VERSION] = (
        SEEDANCE_STORYBOARD_SCHEMA_VERSION
    )
    project_title: str = Field(min_length=1, max_length=500)
    provenance: UpstreamProvenance
    assets: list[StoryboardAsset] = Field(default_factory=list)
    episodes: list[SeedanceStoryboardEpisode] = Field(min_length=1)
    warnings: list[StoryboardImportIssue] = Field(default_factory=list)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_package(self) -> "SeedanceStoryboardPackage":
        asset_ids = [item.asset_id for item in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("asset IDs must be unique")
        episode_ids = [item.episode_id for item in self.episodes]
        if len(episode_ids) != len(set(episode_ids)):
            raise ValueError("episode IDs must be unique")
        if episode_ids != sorted(episode_ids, key=lambda value: int(value[1:])):
            raise ValueError("episodes must be sorted by numeric episode ID")

        known_assets = set(asset_ids)
        for episode in self.episodes:
            unknown = sorted(
                {slot.asset_id for slot in episode.upload_slots} - known_assets
            )
            if unknown:
                raise ValueError(
                    f"episode {episode.episode_id} references unknown assets: {unknown}"
                )

        if self.content_digest != self.compute_content_digest():
            raise ValueError("content_digest does not match canonical package content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "SeedanceStoryboardPackage":
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

    def to_canonical_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
        )
