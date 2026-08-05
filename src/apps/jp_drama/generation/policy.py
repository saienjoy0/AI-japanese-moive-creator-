"""Segmentation policy separated from provider capability contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import Field, model_validator

from .models import GenerationModel


class DurationBand(GenerationModel):
    minimum_seconds: int = Field(ge=1, le=15)
    target_seconds: int = Field(ge=1, le=15)
    maximum_seconds: int = Field(ge=1, le=15)

    @model_validator(mode="after")
    def validate_order(self) -> "DurationBand":
        if not self.minimum_seconds <= self.target_seconds <= self.maximum_seconds:
            raise ValueError("duration band must satisfy minimum <= target <= maximum")
        return self


class SegmentationPolicy(GenerationModel):
    policy_id: str = Field(min_length=1)
    low: DurationBand = DurationBand(
        minimum_seconds=8,
        target_seconds=10,
        maximum_seconds=12,
    )
    medium: DurationBand = DurationBand(
        minimum_seconds=7,
        target_seconds=8,
        maximum_seconds=10,
    )
    high: DurationBand = DurationBand(
        minimum_seconds=4,
        target_seconds=6,
        maximum_seconds=7,
    )
    very_high: DurationBand = DurationBand(
        minimum_seconds=2,
        target_seconds=4,
        maximum_seconds=5,
    )
    max_internal_editorial_shots: int = Field(default=3, ge=1, le=4)
    allow_low_complexity_15_seconds: bool = True
    max_characters_for_15_seconds: int = Field(default=2, ge=1)
    strict_lip_sync_shortens_segment: bool = True

    def band_for(self, level: str) -> DurationBand:
        return getattr(self, level)


class ProviderSegmentationProfile(GenerationModel):
    profile_id: str = Field(min_length=1)
    route_id: str = Field(min_length=1)
    policy: SegmentationPolicy
    hard_budget_currency: str | None = None
    hard_budget_amount: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_budget(self) -> "ProviderSegmentationProfile":
        if (self.hard_budget_currency is None) != (self.hard_budget_amount is None):
            raise ValueError("hard budget currency and amount must be provided together")
        return self

    @property
    def policy_digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    @classmethod
    def load(cls, path: str | Path) -> "ProviderSegmentationProfile":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(payload)
