"""Strict configuration for live Japanese-drama generation providers."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


LIVE_PROVIDER_SCHEMA_VERSION = "1.2.0"


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
    base_url_env: Literal["DASHSCOPE_BASE_URL"] = "DASHSCOPE_BASE_URL"
    upload_base_url_env: Literal["DASHSCOPE_UPLOAD_BASE_URL"] = "DASHSCOPE_UPLOAD_BASE_URL"
    workspace_id_env: Literal["DASHSCOPE_WORKSPACE_ID"] = "DASHSCOPE_WORKSPACE_ID"
    region: Literal["beijing", "singapore"] = "singapore"
    image_model: str = Field(default="wan2.7-image-pro", min_length=1)
    video_model: str = Field(default="wan2.7-i2v", min_length=1)
    native_av_video_model: str = Field(default="wan2.7-i2v", min_length=1)
    tts_model: str = Field(default="qwen3-tts-flash", min_length=1)
    tts_family: Literal["qwen3", "cosyvoice"] = "qwen3"
    default_voice: str = Field(default="Ono Anna", min_length=1)
    voice_by_character: dict[str, str] = Field(default_factory=dict)
    tts_instructions_enabled: bool = False
    image_size: str = Field(default="960*1696", pattern=r"^[1-9][0-9]*\*[1-9][0-9]*$")
    image_thinking_mode: bool = True
    video_resolution: Literal["720P", "1080P"] = "720P"
    provider_clip_seconds: int = Field(default=5, ge=2, le=15)
    prompt_extend: bool = True
    watermark: bool = False
    seed_base: int = Field(default=240700, ge=0, le=2_147_483_647)
    price_snapshot_date: str = Field(default="2026-08-04", pattern=r"^\d{4}-\d{2}-\d{2}$")
    image_cost_cny: Decimal = Field(default=Decimal("0.562065"), ge=0)
    video_cost_cny_per_second: Decimal = Field(default=Decimal("0.74942"), ge=0)
    tts_cost_cny_per_10k_chars: Decimal = Field(default=Decimal("0.733924"), ge=0)

    @model_validator(mode="after")
    def validate_voice_mapping_and_size(self) -> "DashScopeProviderConfig":
        if any(not key or not value for key, value in self.voice_by_character.items()):
            raise ValueError("voice_by_character keys and values must be non-empty")
        width_text, height_text = self.image_size.split("*", 1)
        width, height = int(width_text), int(height_text)
        if width < 768 or height < 768:
            raise ValueError("Wan 2.7 image dimensions must each be at least 768 pixels")
        if width > 4096 or height > 4096:
            raise ValueError("Wan 2.7 image dimensions must not exceed 4096 pixels")
        ratio = width / height
        if not (1 / 8 <= ratio <= 8):
            raise ValueError("Wan 2.7 image aspect ratio must be between 1:8 and 8:1")
        return self

    def endpoint_base_url(self) -> str | None:
        explicit = os.getenv(self.base_url_env, "").strip()
        if explicit:
            return explicit.rstrip("/")
        workspace_id = os.getenv(self.workspace_id_env, "").strip()
        if not workspace_id:
            return None
        region_domain = {
            "beijing": "cn-beijing",
            "singapore": "ap-southeast-1",
        }[self.region]
        return f"https://{workspace_id}.{region_domain}.maas.aliyuncs.com"

    def upload_endpoint_base_url(self) -> str:
        explicit = os.getenv(self.upload_base_url_env, "").strip()
        if explicit:
            return explicit.rstrip("/")
        return {
            "beijing": "https://dashscope.aliyuncs.com",
            "singapore": "https://dashscope-intl.aliyuncs.com",
        }[self.region]

    def required_environment(self) -> list[str]:
        required = [self.api_key_env]
        if not os.getenv(self.base_url_env, "").strip():
            required.append(self.workspace_id_env)
        return required

    def missing_environment(self) -> list[str]:
        return [name for name in self.required_environment() if not os.getenv(name, "").strip()]

    def configure_runtime(self) -> str:
        endpoint = self.endpoint_base_url()
        if endpoint is None:
            raise ProviderConfigurationError(
                f"configure {self.base_url_env} or {self.workspace_id_env} for {self.region}"
            )
        os.environ[self.base_url_env] = endpoint
        os.environ[self.upload_base_url_env] = self.upload_endpoint_base_url()
        try:
            import dashscope

            dashscope.base_http_api_url = f"{endpoint}/api/v1"
        except Exception:
            pass
        return endpoint

    def estimate_image_cost_cny(self) -> Decimal:
        return self.image_cost_cny

    def estimate_video_cost_cny(self, duration_seconds: int | float) -> Decimal:
        return self.video_cost_cny_per_second * Decimal(str(duration_seconds))

    def estimate_tts_cost_cny(self, characters: int) -> Decimal:
        return self.tts_cost_cny_per_10k_chars * Decimal(characters) / Decimal("10000")


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
        missing = self.dashscope.missing_environment()
        if missing:
            raise ProviderConfigurationError(
                "required provider environment is missing: " + ", ".join(missing)
            )
        self.dashscope.configure_runtime()

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
            "region": provider.region,
            "base_url_env": provider.base_url_env,
            "upload_base_url_env": provider.upload_base_url_env,
            "workspace_id_env": provider.workspace_id_env,
            "image_model": provider.image_model,
            "image_size": provider.image_size,
            "video_model": provider.video_model,
            "video_resolution": provider.video_resolution,
            "native_av_video_model": provider.native_av_video_model,
            "tts_model": provider.tts_model,
            "default_voice": provider.default_voice,
            "tts_instructions_enabled": str(provider.tts_instructions_enabled).lower(),
            "price_snapshot_date": provider.price_snapshot_date,
        }
