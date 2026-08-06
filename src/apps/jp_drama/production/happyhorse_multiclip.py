"""Contracts and local composition helpers for HappyHorse multi-clip production.

A source storyboard segment may contain several visual cuts even when the
provider accepts only one first frame per request. This module models that
production split without changing the existing one-segment Canary contract.
"""

from __future__ import annotations

import base64
import hashlib
import re
import xml.etree.ElementTree as ET
import json
import math
import os
import struct
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "1.0.0"
MODEL_NAME = "happyhorse-1.1-i2v"
MIN_PROVIDER_DURATION = 3
MAX_PROVIDER_DURATION = 15
MAX_CLIPS = 20
PORTRAIT_RATIO = 9 / 16
PORTRAIT_RATIO_TOLERANCE = 0.01


class HappyHorseMultiClipError(RuntimeError):
    """A multi-clip production contract or local composition is invalid."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class OutputSpec(FrozenModel):
    duration_seconds: float = Field(gt=0)
    fps: int = Field(gt=0, le=120)
    aspect_ratio: Literal["9:16"] = "9:16"
    resolution: Literal["720P", "1080P"] = "720P"
    estimated_output_frames: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_frame_count(self) -> "OutputSpec":
        expected = round(self.duration_seconds * self.fps)
        if self.estimated_output_frames != expected:
            raise ValueError(
                "estimated_output_frames must equal duration_seconds * fps"
            )
        return self


class SourceStoryboard(FrozenModel):
    dialogue: str | None = None
    audio: list[str] = Field(default_factory=list)


class ProductionClip(FrozenModel):
    clip_id: str = Field(min_length=1)
    timeline_start_seconds: float = Field(ge=0)
    timeline_end_seconds: float = Field(gt=0)
    final_duration_seconds: float = Field(gt=0)
    provider_request_duration_seconds: int = Field(
        ge=MIN_PROVIDER_DURATION,
        le=MAX_PROVIDER_DURATION,
    )
    trim_after_generation: bool = True

    first_frame_path: str = Field(min_length=1)
    first_frame_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    mime_type: Literal["image/png", "image/jpeg"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    reference_asset_ids: list[str] = Field(min_length=1, max_length=9)
    visual_prompt: str = Field(min_length=1)
    motion_prompt: str = Field(min_length=1)
    camera_prompt: str = Field(min_length=1)
    audio_prompt: str = Field(min_length=1)
    dialogue_prompt: str | None = None
    negative_constraints: list[str] = Field(default_factory=list)
    requires_audio_stream: bool = True

    approval_status: Literal["approved"] = "approved"
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    generated_by: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_clip(self) -> "ProductionClip":
        duration = self.timeline_end_seconds - self.timeline_start_seconds
        if not math.isclose(
            duration,
            self.final_duration_seconds,
            abs_tol=0.001,
        ):
            raise ValueError(
                f"{self.clip_id} timeline duration does not match final duration"
            )
        if self.final_duration_seconds > self.provider_request_duration_seconds:
            raise ValueError(
                f"{self.clip_id} final duration exceeds provider request duration"
            )
        if len(self.reference_asset_ids) != len(set(self.reference_asset_ids)):
            raise ValueError(f"{self.clip_id} reference asset IDs must be unique")
        return self


class AssemblySpec(FrozenModel):
    clip_order: list[str] = Field(min_length=1)
    transition_style: list[Literal["hard_cut", "memory_flash_cut"]]
    video_codec: Literal["libx264"] = "libx264"
    audio_codec: Literal["aac"] = "aac"
    audio_sample_rate: int = 48000
    audio_channels: int = 2


class HappyHorseMultiClipPlan(FrozenModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    project_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    source_segment_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    production_mode: Literal["happyhorse_multi_clip_i2v"]
    master_approval_manifest_path: str = Field(min_length=1)
    master_approval_id: str = Field(min_length=1)
    output: OutputSpec
    source_storyboard: SourceStoryboard
    clips: list[ProductionClip] = Field(min_length=1, max_length=MAX_CLIPS)
    assembly: AssemblySpec
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> "HappyHorseMultiClipPlan":
        clip_ids = [clip.clip_id for clip in self.clips]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError("clip IDs must be unique")
        if self.assembly.clip_order != clip_ids:
            raise ValueError("assembly clip_order must exactly match plan clip order")
        if len(self.assembly.transition_style) != max(0, len(self.clips) - 1):
            raise ValueError("transition_style count must be clip count minus one")

        cursor = 0.0
        for clip in self.clips:
            if not math.isclose(clip.timeline_start_seconds, cursor, abs_tol=0.001):
                raise ValueError(
                    f"timeline is not contiguous before {clip.clip_id}: "
                    f"expected {cursor}, got {clip.timeline_start_seconds}"
                )
            cursor = clip.timeline_end_seconds
        if not math.isclose(cursor, self.output.duration_seconds, abs_tol=0.001):
            raise ValueError("clip timeline does not equal output duration")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("content_digest does not match canonical plan content")
        return self

    @classmethod
    def build_with_digest(cls, **data: Any) -> "HappyHorseMultiClipPlan":
        normalized = dict(data)
        normalized["output"] = OutputSpec.model_validate(data["output"])
        normalized["source_storyboard"] = SourceStoryboard.model_validate(
            data["source_storyboard"]
        )
        normalized["clips"] = [
            ProductionClip.model_validate(item) for item in data["clips"]
        ]
        normalized["assembly"] = AssemblySpec.model_validate(data["assembly"])
        provisional = cls.model_construct(
            **normalized,
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


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def embedded_image_bytes(path: str | Path) -> tuple[bytes, str]:
    """Return provider-ready image bytes from a direct image or SVG data wrapper."""

    source = Path(path)
    if source.suffix.lower() != ".svg":
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(source.suffix.lower())
        if mime is None:
            raise HappyHorseMultiClipError(
                f"unsupported first-frame image source: {source}"
            )
        return source.read_bytes(), mime

    text = source.read_text(encoding="utf-8")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise HappyHorseMultiClipError(
            f"first-frame source/hash changed; SVG wrapper is invalid XML: {source}"
        ) from exc
    image = next(
        (node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "image"),
        None,
    )
    href = image.attrib.get("href", "") if image is not None else ""
    match = re.fullmatch(
        r"data:(image/(?:jpeg|png));base64,([A-Za-z0-9+/=]+)",
        href,
    )
    if match is None:
        raise HappyHorseMultiClipError(
            f"SVG first-frame wrapper has no embedded JPEG or PNG: {source}"
        )
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise HappyHorseMultiClipError(
            f"SVG first-frame wrapper has invalid base64: {source}"
        ) from exc
    return payload, match.group(1)


def _dimensions_from_bytes(payload: bytes, mime_type: str) -> tuple[int, int]:
    if mime_type == "image/png":
        if payload[:8] != b"\x89PNG\r\n\x1a\n" or len(payload) < 24:
            raise HappyHorseMultiClipError("embedded image is not a valid PNG")
        return struct.unpack(">II", payload[16:24])
    if mime_type == "image/jpeg":
        from io import BytesIO

        handle = BytesIO(payload)
        if handle.read(2) != b"\xff\xd8":
            raise HappyHorseMultiClipError("embedded image is not a valid JPEG")
        while True:
            marker_start = handle.read(1)
            if not marker_start:
                break
            if marker_start != b"\xff":
                continue
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if marker in {b"\xd8", b"\xd9"}:
                continue
            length_bytes = handle.read(2)
            if len(length_bytes) != 2:
                break
            length = struct.unpack(">H", length_bytes)[0]
            if length < 2:
                break
            if marker and marker[0] in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            }:
                data = handle.read(length - 2)
                if len(data) < 5:
                    break
                height, width = struct.unpack(">HH", data[1:5])
                return width, height
            handle.seek(length - 2, 1)
        raise HappyHorseMultiClipError("JPEG dimensions not found")
    raise HappyHorseMultiClipError(f"unsupported embedded image MIME type: {mime_type}")


def first_frame_sha256(path: str | Path) -> str:
    payload, _ = embedded_image_bytes(path)
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def image_dimensions(path: str | Path) -> tuple[int, int]:
    payload, mime_type = embedded_image_bytes(path)
    return _dimensions_from_bytes(payload, mime_type)


def materialize_first_frame(
    path: str | Path,
    destination_dir: str | Path,
    *,
    expected_sha256: str,
) -> Path:
    """Decode an SVG data wrapper or copy a direct image into a provider-ready file."""

    payload, mime_type = embedded_image_bytes(path)
    actual = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if actual != expected_sha256:
        raise HappyHorseMultiClipError("first-frame payload hash changed")
    suffix = ".jpg" if mime_type == "image/jpeg" else ".png"
    source = Path(path)
    destination = Path(destination_dir).resolve() / f"{source.stem}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_bytes(payload)
    os.replace(temporary, destination)
    return destination


def png_dimensions(path: str | Path) -> tuple[int, int]:
    """Backward-compatible alias retained for focused tests and callers."""
    return image_dimensions(path)


def _resolve_repo_path(repo_root: Path, raw_path: str, *, label: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute():
        raise HappyHorseMultiClipError(f"{label} must be repository-relative")
    root = repo_root.resolve()
    resolved = (root / relative).resolve()
    if root != resolved and root not in resolved.parents:
        raise HappyHorseMultiClipError(f"{label} escapes repository root")
    return resolved


def load_plan(path: str | Path) -> HappyHorseMultiClipPlan:
    source = Path(path)
    try:
        return HappyHorseMultiClipPlan.model_validate_json(
            source.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise HappyHorseMultiClipError(f"cannot load production plan: {exc}") from exc


def verify_plan_files(
    plan: HappyHorseMultiClipPlan,
    *,
    repository_root: str | Path,
) -> dict[str, Any]:
    root = Path(repository_root).resolve()
    approval_path = _resolve_repo_path(
        root,
        plan.master_approval_manifest_path,
        label="master approval manifest path",
    )
    try:
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HappyHorseMultiClipError(
            f"cannot load master approval manifest: {exc}"
        ) from exc
    if approval.get("decision") != "approved":
        raise HappyHorseMultiClipError("master reference decision is not approved")
    if approval.get("approval_id") != plan.master_approval_id:
        raise HappyHorseMultiClipError("master approval ID does not match plan")
    raw_assets = approval.get("assets")
    if not isinstance(raw_assets, list):
        raise HappyHorseMultiClipError("master approval assets are invalid")
    approved_assets = {
        str(item.get("source_asset_id")): item
        for item in raw_assets
        if isinstance(item, dict)
    }

    verified_clips: list[dict[str, Any]] = []
    for clip in plan.clips:
        frame = _resolve_repo_path(
            root,
            clip.first_frame_path,
            label=f"{clip.clip_id} first-frame path",
        )
        if not frame.is_file() or frame.stat().st_size == 0:
            raise HappyHorseMultiClipError(
                f"{clip.clip_id} first frame is missing or empty"
            )
        _, embedded_mime = embedded_image_bytes(frame)
        if embedded_mime != clip.mime_type:
            raise HappyHorseMultiClipError(
                f"{clip.clip_id} embedded image MIME type does not match plan"
            )
        actual_hash = first_frame_sha256(frame)
        if actual_hash != clip.first_frame_sha256:
            raise HappyHorseMultiClipError(
                f"{clip.clip_id} first-frame hash changed"
            )
        width, height = image_dimensions(frame)
        if (width, height) != (clip.width, clip.height):
            raise HappyHorseMultiClipError(
                f"{clip.clip_id} first-frame dimensions changed"
            )
        actual_ratio = width / height
        if abs(actual_ratio - PORTRAIT_RATIO) / PORTRAIT_RATIO > PORTRAIT_RATIO_TOLERANCE:
            raise HappyHorseMultiClipError(
                f"{clip.clip_id} first frame must be 9:16 portrait"
            )
        for asset_id in clip.reference_asset_ids:
            asset = approved_assets.get(asset_id)
            if asset is None:
                raise HappyHorseMultiClipError(
                    f"{clip.clip_id} references unapproved master asset {asset_id}"
                )
            master_path = _resolve_repo_path(
                root,
                str(asset.get("path", "")),
                label=f"master asset {asset_id} path",
            )
            if not master_path.is_file() or master_path.stat().st_size == 0:
                raise HappyHorseMultiClipError(
                    f"approved master asset is missing: {asset_id}"
                )
            expected = "sha256:" + str(asset.get("sha256", "")).removeprefix("sha256:")
            if file_sha256(master_path) != expected:
                raise HappyHorseMultiClipError(
                    f"approved master asset hash changed: {asset_id}"
                )
        verified_clips.append(
            {
                "clip_id": clip.clip_id,
                "first_frame": str(frame),
                "first_frame_sha256": actual_hash,
                "width": width,
                "height": height,
                "reference_asset_ids": clip.reference_asset_ids,
            }
        )
    return {
        "valid": True,
        "plan_digest": plan.content_digest,
        "master_approval_id": plan.master_approval_id,
        "clip_count": len(plan.clips),
        "clips": verified_clips,
        "external_api_calls": 0,
    }


def build_clip_prompt(clip: ProductionClip) -> str:
    parts = [
        "Japanese live-action vertical short drama. Preserve the approved first "
        "frame exactly: identity, age, costume, props, period setting, lighting, "
        "screen direction, and composition.",
        f"Visual: {clip.visual_prompt}",
        f"Motion: {clip.motion_prompt}",
        f"Camera: {clip.camera_prompt}",
        f"Audio: {clip.audio_prompt}",
    ]
    if clip.dialogue_prompt:
        parts.append(f"Dialogue: {clip.dialogue_prompt}")
    if clip.negative_constraints:
        parts.append("Constraints: " + "; ".join(clip.negative_constraints))
    parts.append(
        "Do not add subtitles, captions, logos, watermarks, title cards, or text."
    )
    return "\n".join(parts)


def deterministic_seed(plan: HappyHorseMultiClipPlan, clip: ProductionClip, base: int) -> int:
    material = (
        f"{plan.content_digest}|{clip.clip_id}|{MODEL_NAME}|production"
    ).encode("utf-8")
    offset = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
    return (base + offset) % 2_147_483_648


def request_fingerprint(
    plan: HappyHorseMultiClipPlan,
    clip: ProductionClip,
    *,
    resolution: str,
    seed: int,
) -> str:
    payload = {
        "model": MODEL_NAME,
        "plan_digest": plan.content_digest,
        "clip_id": clip.clip_id,
        "prompt": build_clip_prompt(clip),
        "first_frame_sha256": clip.first_frame_sha256,
        "resolution": resolution,
        "duration": clip.provider_request_duration_seconds,
        "seed": seed,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def build_preflight_report(
    plan: HappyHorseMultiClipPlan,
    *,
    repository_root: str | Path,
    seed_base: int,
    resolution: str,
    max_api_calls: int,
    max_cost_cny: str,
    cost_reserve_cny_per_clip: str,
    missing_environment: list[str] | None = None,
) -> dict[str, Any]:
    verified = verify_plan_files(plan, repository_root=repository_root)
    reserve_each = float(cost_reserve_cny_per_clip)
    reserve_total = reserve_each * len(plan.clips)
    max_cost = float(max_cost_cny)
    requests = []
    for clip in plan.clips:
        seed = deterministic_seed(plan, clip, seed_base)
        requests.append(
            {
                "clip_id": clip.clip_id,
                "operation_id": (
                    f"{plan.source_segment_id}:{clip.clip_id}:happyhorse-1.1-i2v:"
                    f"{request_fingerprint(plan, clip, resolution=resolution, seed=seed).split(':', 1)[1][:16]}"
                ),
                "first_frame_path": clip.first_frame_path,
                "first_frame_sha256": clip.first_frame_sha256,
                "resolution": resolution,
                "provider_duration_seconds": clip.provider_request_duration_seconds,
                "final_duration_seconds": clip.final_duration_seconds,
                "seed": seed,
                "prompt": build_clip_prompt(clip),
                "request_fingerprint": request_fingerprint(
                    plan,
                    clip,
                    resolution=resolution,
                    seed=seed,
                ),
            }
        )
    stable = {
        "protocol": "happyhorse-1.1-i2v-multiclip-production-v1",
        "model": MODEL_NAME,
        "plan_digest": plan.content_digest,
        "source_segment_id": plan.source_segment_id,
        "clip_count": len(plan.clips),
        "resolution": resolution,
        "requests": requests,
        "max_api_calls": max_api_calls,
        "max_cost_cny": str(max_cost_cny),
        "cost_reserve_cny_per_clip": str(cost_reserve_cny_per_clip),
        "total_cost_reserve_cny": f"{reserve_total:g}",
    }
    canonical = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    valid = max_api_calls == len(plan.clips) and reserve_total <= max_cost
    return {
        **stable,
        "valid": valid,
        "approval_digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        "verified_assets": verified,
        "missing_environment": list(missing_environment or []),
        "credentials_present": not missing_environment,
        "external_api_calls": 0,
        "status": (
            "ready_for_four_paid_submissions" if valid else "blocked_by_limits"
        ),
    }


def build_ffmpeg_concat_command(
    plan: HappyHorseMultiClipPlan,
    *,
    raw_outputs: list[str | Path],
    output_path: str | Path,
) -> list[str]:
    if len(raw_outputs) != len(plan.clips):
        raise HappyHorseMultiClipError("raw output count does not match clip count")
    command = ["ffmpeg", "-y"]
    for source in raw_outputs:
        command.extend(["-i", str(Path(source))])

    filters: list[str] = []
    concat_inputs: list[str] = []
    width, height = (720, 1280) if plan.output.resolution == "720P" else (1080, 1920)
    for index, clip in enumerate(plan.clips):
        duration = clip.final_duration_seconds
        filters.append(
            f"[{index}:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={plan.output.fps},format=yuv420p[v{index}]"
        )
        filters.append(
            f"[{index}:a]atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
            f"aresample={plan.assembly.audio_sample_rate},"
            f"aformat=channel_layouts=stereo[a{index}]"
        )
        concat_inputs.extend([f"[v{index}]", f"[a{index}]"])
    filters.append(
        f"{''.join(concat_inputs)}concat=n={len(plan.clips)}:v=1:a=1[outv][outa]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-t",
            f"{plan.output.duration_seconds:.3f}",
            "-c:v",
            plan.assembly.video_codec,
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            plan.assembly.audio_codec,
            "-b:a",
            "192k",
            "-ar",
            str(plan.assembly.audio_sample_rate),
            "-ac",
            str(plan.assembly.audio_channels),
            "-movflags",
            "+faststart",
            str(Path(output_path)),
        ]
    )
    return command


def assemble_clips(
    plan: HappyHorseMultiClipPlan,
    *,
    raw_outputs: list[str | Path],
    output_path: str | Path,
) -> Path:
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    for source in raw_outputs:
        path = Path(source).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise HappyHorseMultiClipError(f"raw clip is missing or empty: {path}")
    command = build_ffmpeg_concat_command(
        plan,
        raw_outputs=raw_outputs,
        output_path=output,
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise HappyHorseMultiClipError(
            "ffmpeg multi-clip assembly failed: " + completed.stderr[-2000:]
        )
    if not output.is_file() or output.stat().st_size == 0:
        raise HappyHorseMultiClipError("ffmpeg produced no usable final MP4")
    return output


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> Path:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, destination)
    return destination
