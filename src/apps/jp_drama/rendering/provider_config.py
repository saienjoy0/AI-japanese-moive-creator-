"""Strict configuration for live Japanese-drama generation providers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


LIVE_PROVIDER_SCHEMA_VERSION = "1.0.0"


class ProviderConfigurationError(RuntimeError):
    """A live provider configuration is invalid or incomplete."""


class ProviderConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class DashScopeProviderConfig(ProviderConfigModel):
    provider: Literal["dashscope"] = "dashscope"
    api_key_env: Literal["DASHSCOPE_API_KEY"] = "DASHSCOPE_API_KEY"
    image_model: str = Field(default="wan2.7-image-pro", min_length=1)
    video_model: str = Field(default="wan2.7-i2v", min_length=1)
    native_av_video_model: str = Field(default="wan2.7-i2v", min_length=1)
    tts_model: str = Field(default="qwen3-tts-flash", min_length=1)
    tts_family: Literal["qwen3", "cosyvoice"] = "qwen3"
    default_voice: str = Field(default="Ono Anna", min_length=1)
    voice_by_character: dict[str, str] = Field(default_factory=dict)
    image_size: str = Field(default="576*1024", pattern=r"^[1-9][0-9]*\*[1-9][0-9]*$")
    video_ratio: Literal["9:16"] = "9:16"
    video_resolution: Literal["480P", "720P", "1080P"] = "720P"
    provider_clip_seconds: int = Field(default=5, ge=1, le=15)
    prompt_extend: bool = True
    watermark: bool = False
    seed_base: int = Field(default=240700, ge=0, le=2_147_483_647)

    @model_validator(mode="after")
    def validate_voice_mapping(self) -> "DashScopeProviderConfig":
        if any(not key or not value for key, value in self.voice_by_character.items()):
            raise ValueError("voice_by_character keys and values must be non-empty")
        return self


class LiveProviderConfig(ProviderConfigModel):
    schema_version: Literal[LIVE_PROVIDER_SCHEMA_VERSION] = LIVE_PROVIDER_SCHEMA_VERSION
    mode: Literal["live"] = "live"
    dashscope: DashScopeProviderConfig = Field(default_factory=DashScopeProviderConfig)

    @classmethod
    def load(cls, path: str | Path) -> "LiveProviderConfig":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls.model_validate(payload)
        except Exception as exc:
            raise ProviderConfigurationError(f"cannot load provider config: {exc}") from exc

    def require_environment(self) -> None:
        name = self.dashscope.api_key_env
        if not os.getenv(name, "").strip():
            raise ProviderConfigurationError(
                f"required provider credential is missing: environment variable {name}"
            )

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"

    @property
    def execution_profile(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"live:dashscope:sha256:{hashlib.sha256(canonical).hexdigest()}"

    @property
    def provider_manifest(self) -> dict[str, str]:
        provider = self.dashscope
        return {
            "mode": self.mode,
            "provider": provider.provider,
            "image_model": provider.image_model,
            "video_model": provider.video_model,
            "native_av_video_model": provider.native_av_video_model,
            "tts_model": provider.tts_model,
            "default_voice": provider.default_voice,
        }