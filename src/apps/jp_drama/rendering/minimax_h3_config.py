"""Strict MiniMax H3 V2 provider configuration and price snapshot."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MiniMaxH3ConfigurationError(RuntimeError):
    """MiniMax H3 configuration is incomplete or invalid."""


class MiniMaxH3ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)

    provider: Literal["minimax"] = "minimax"
    api_key_env: Literal["MINIMAX_API_KEY"] = "MINIMAX_API_KEY"
    base_url: str = "https://api.minimax.io"
    model: Literal["MiniMax-H3"] = "MiniMax-H3"
    resolution: Literal["768P", "2K"] = "768P"
    ratio: Literal["9:16"] = "9:16"
    poll_interval_seconds: float = Field(default=10, gt=0)
    max_poll_seconds: int = Field(default=1800, ge=1)
    request_timeout_seconds: int = Field(default=60, ge=1)
    download_timeout_seconds: int = Field(default=300, ge=1)
    max_poll_retries: int = Field(default=10, ge=0)
    max_download_retries: int = Field(default=3, ge=0)
    max_request_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    output_cost_usd_per_second_768p: Decimal = Field(default=Decimal("0.08"), ge=0)
    output_cost_usd_per_second_2k: Decimal = Field(default=Decimal("0.13"), ge=0)
    video_input_cost_usd_per_second_768p: Decimal = Field(default=Decimal("0.08"), ge=0)
    video_input_cost_usd_per_second_2k: Decimal = Field(default=Decimal("0.13"), ge=0)
    extra_image_cost_usd: Decimal = Field(default=Decimal("0.04"), ge=0)
    free_image_count: int = Field(default=5, ge=0)
    price_snapshot_date: str = Field(default="2026-08-05", pattern=r"^\d{4}-\d{2}-\d{2}$")

    @classmethod
    def load(cls, path: str | Path) -> "MiniMaxH3ProviderConfig":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls.model_validate(payload)
        except Exception as exc:
            raise MiniMaxH3ConfigurationError(f"cannot load MiniMax H3 config: {exc}") from exc

    @property
    def submit_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/v2/video_generation"

    def query_url(self, task_id: str) -> str:
        if not task_id.strip():
            raise MiniMaxH3ConfigurationError("task_id must not be empty")
        return f"{self.base_url.rstrip('/')}/v2/query/video_generation/{task_id}"

    def api_key(self) -> str:
        value = os.getenv(self.api_key_env, "").strip()
        if not value:
            raise MiniMaxH3ConfigurationError(
                f"required provider environment is missing: {self.api_key_env}"
            )
        return value

    def output_rate(self, resolution: str | None = None) -> Decimal:
        selected = resolution or self.resolution
        return (
            self.output_cost_usd_per_second_2k
            if selected == "2K"
            else self.output_cost_usd_per_second_768p
        )

    def video_input_rate(self, resolution: str | None = None) -> Decimal:
        selected = resolution or self.resolution
        return (
            self.video_input_cost_usd_per_second_2k
            if selected == "2K"
            else self.video_input_cost_usd_per_second_768p
        )

    def estimate_cost_usd(
        self,
        *,
        duration_seconds: int | float,
        reference_image_count: int = 0,
        reference_video_seconds: int | float = 0,
        resolution: str | None = None,
    ) -> Decimal:
        output = self.output_rate(resolution) * Decimal(str(duration_seconds))
        video_input = self.video_input_rate(resolution) * Decimal(
            str(reference_video_seconds)
        )
        paid_images = max(0, reference_image_count - self.free_image_count)
        images = self.extra_image_cost_usd * Decimal(paid_images)
        return output + video_input + images
