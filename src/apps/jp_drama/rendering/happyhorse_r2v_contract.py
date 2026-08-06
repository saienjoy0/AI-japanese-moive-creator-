"""Hash-bound exact request contract for HappyHorse 1.1 R2V."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


HAPPYHORSE_R2V_APPROVAL_SCHEMA_VERSION = "1.0.0"


class ApprovalContractError(RuntimeError):
    """An exact paid request no longer matches its reviewed contract."""


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class HappyHorseR2VApprovalManifest(ContractModel):
    schema_version: Literal[HAPPYHORSE_R2V_APPROVAL_SCHEMA_VERSION] = (
        HAPPYHORSE_R2V_APPROVAL_SCHEMA_VERSION
    )
    protocol: Literal["happyhorse-1.1-r2v-official-canary-v1"] = (
        "happyhorse-1.1-r2v-official-canary-v1"
    )
    provider: Literal["dashscope"] = "dashscope"
    model: Literal["happyhorse-1.1-r2v"] = "happyhorse-1.1-r2v"
    provider_route_id: Literal["dashscope/happyhorse-1.1-r2v"] = (
        "dashscope/happyhorse-1.1-r2v"
    )
    segment_id: str = Field(min_length=1)
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    asset_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    reference_selection_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    prompt_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    prompt_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ordered_asset_ids: list[str] = Field(min_length=1, max_length=9)
    ordered_asset_sha256: list[str] = Field(min_length=1, max_length=9)
    deployment_region: str = Field(min_length=1)
    endpoint_origin_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    workspace_id_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    resolution: Literal["720P", "1080P"]
    ratio: Literal["9:16"] = "9:16"
    duration: int = Field(ge=3, le=15)
    watermark: bool = False
    seed: int = Field(ge=0, le=2_147_483_647)
    audio_strategy: Literal["native_audio", "external_tts", "silent"]
    price_snapshot_id: str = Field(min_length=1)
    quoted_cost_cny: Decimal = Field(ge=0)
    max_api_calls: int = Field(ge=1, le=1)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> "HappyHorseR2VApprovalManifest":
        if len(self.ordered_asset_ids) != len(self.ordered_asset_sha256):
            raise ValueError("ordered reference IDs and hashes must have equal length")
        if len(self.ordered_asset_ids) != len(set(self.ordered_asset_ids)):
            raise ValueError("duplicate_reference_asset_id")
        if len(self.ordered_asset_sha256) != len(set(self.ordered_asset_sha256)):
            raise ValueError("duplicate_reference_asset_sha")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("approval manifest digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "HappyHorseR2VApprovalManifest":
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
            default=str,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    def assert_approval_digest(self, supplied: str) -> None:
        if supplied.strip() != self.content_digest:
            raise ApprovalContractError(
                "approval digest does not match the exact HappyHorse R2V request"
            )

    def to_canonical_json(self) -> str:
        return (
            json.dumps(
                self.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                default=str,
            )
            + "\n"
        )


def write_approval_manifest(
    path: str | Path,
    manifest: HappyHorseR2VApprovalManifest,
) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(manifest.to_canonical_json(), encoding="utf-8")


def load_approval_manifest(
    path: str | Path,
) -> HappyHorseR2VApprovalManifest:
    return HappyHorseR2VApprovalManifest.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def hash_binding(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ApprovalContractError("binding value must not be empty")
    return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
