"""Provider-neutral R2V prompt assembly with ordered [Image N] bindings."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..assets.reference_resolution import ReferenceSelectionManifest
from ..generation.models import GenerationSegment


R2V_PROMPT_SCHEMA_VERSION = "1.0.0"
_IMAGE_REF_RE = re.compile(r"\[Image\s+(\d+)\]")


class ReferencePromptError(RuntimeError):
    """An R2V prompt cannot be bound safely to its media array."""


class PromptModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class ImagePromptBinding(PromptModel):
    image_number: int = Field(ge=1, le=9)
    asset_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    role: str = Field(min_length=1)


class TimelineSection(PromptModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    action: str = Field(min_length=1)
    camera: str | None = None

    @model_validator(mode="after")
    def validate_window(self) -> "TimelineSection":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("timeline section end must be after start")
        return self


class CreativeOverride(PromptModel):
    segment_id: str = Field(min_length=1)
    timeline: list[TimelineSection] = Field(min_length=1)
    extra_constraints: list[str] = Field(default_factory=list)


class ReferencePromptBundle(PromptModel):
    schema_version: str = R2V_PROMPT_SCHEMA_VERSION
    segment_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=2500)
    prompt_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    image_bindings: list[ImagePromptBinding] = Field(min_length=1, max_length=9)
    timeline_sections: list[TimelineSection] = Field(min_length=1)
    negative_constraints: list[str] = Field(default_factory=list)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_bundle(self) -> "ReferencePromptBundle":
        if self.prompt_sha256 != _text_digest(self.prompt):
            raise ValueError("prompt_sha256 does not match prompt")
        expected = list(range(1, len(self.image_bindings) + 1))
        actual = [item.image_number for item in self.image_bindings]
        if actual != expected:
            raise ValueError("image prompt binding numbers must be contiguous")
        validate_prompt_media_alignment(self.prompt, len(self.image_bindings))
        if self.content_digest != self.compute_content_digest():
            raise ValueError("reference prompt bundle digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "ReferencePromptBundle":
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
        return _digest(payload)


def load_creative_override(path: str | Path | None) -> CreativeOverride | None:
    if path is None:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return CreativeOverride.model_validate(payload)


def build_reference_prompt(
    segment: GenerationSegment,
    selection: ReferenceSelectionManifest,
    *,
    audio_strategy: str,
    creative_override: CreativeOverride | None = None,
) -> ReferencePromptBundle:
    if selection.segment_id != segment.segment_id:
        raise ReferencePromptError(
            "reference selection belongs to another generation segment"
        )
    if creative_override and creative_override.segment_id != segment.segment_id:
        raise ReferencePromptError("creative override belongs to another segment")

    bindings = [
        ImagePromptBinding(
            image_number=index,
            asset_id=image.asset_id,
            subject_id=image.subject_id,
            role=image.role,
        )
        for index, image in enumerate(selection.images, start=1)
    ]
    timeline = (
        creative_override.timeline
        if creative_override is not None
        else _timeline_from_segment(segment)
    )
    constraints = list(
        dict.fromkeys(
            [
                *segment.prompt_bundle.negative_constraints,
                *(
                    creative_override.extra_constraints
                    if creative_override is not None
                    else []
                ),
                "identity drift",
                "costume change",
                "modern objects",
                "extra people",
                "changing props",
                "subtitles",
                "captions",
                "logos",
                "watermarks",
                "on-screen text",
            ]
        )
    )

    parts = [
        "Japanese live-action vertical short drama, 9:16.",
        "Use the ordered approved references exactly as follows:",
    ]
    for binding in bindings:
        role_text = {
            "character_master": "character identity, age, face, hair, body and costume",
            "location_master": "location architecture, furniture, period and lighting",
            "prop_master": "prop identity, material, color, scale and count",
        }.get(binding.role, binding.role)
        parts.append(
            f"[Image {binding.image_number}] is the approved {role_text} "
            f"reference for {binding.subject_id}."
        )

    bundle = segment.prompt_bundle
    parts.extend(
        [
            f"Narrative: {bundle.narrative_summary}",
            f"Visual: {bundle.visual_prompt}",
            f"Motion: {bundle.motion_prompt}",
            f"Camera: {bundle.camera_prompt}",
            "Timeline:",
        ]
    )
    for item in timeline:
        camera = f" Camera: {item.camera}." if item.camera else ""
        parts.append(
            f"{item.start_seconds:.1f}-{item.end_seconds:.1f}s: "
            f"{item.action}.{camera}"
        )

    if bundle.dialogue_prompt:
        if audio_strategy == "native_audio":
            parts.append(
                "Spoken dialogue: natural Japanese, exact wording and timing, "
                f"no visible speech for inner monologue: {bundle.dialogue_prompt}"
            )
        elif audio_strategy == "external_tts":
            parts.append(
                "Do not generate visible lip motion unless the editorial shot "
                "explicitly requires it; dialogue will be added by external TTS."
            )
        else:
            parts.append("Generate no spoken dialogue.")
    if bundle.audio_prompt and audio_strategy == "native_audio":
        parts.append(f"Audio: {bundle.audio_prompt}")
    elif audio_strategy == "silent":
        parts.append("Audio: no dialogue, music, ambience or sound effects.")
    parts.append("Constraints: " + "; ".join(constraints) + ".")

    prompt = "\n".join(parts)
    if len(prompt) > 2500:
        raise ReferencePromptError(
            f"HappyHorse prompt is {len(prompt)} characters; maximum is 2500"
        )
    validate_prompt_media_alignment(prompt, len(bindings))
    return ReferencePromptBundle.build_with_digest(
        segment_id=segment.segment_id,
        prompt=prompt,
        prompt_sha256=_text_digest(prompt),
        image_bindings=bindings,
        timeline_sections=timeline,
        negative_constraints=constraints,
    )


def validate_prompt_media_alignment(prompt: str, media_count: int) -> None:
    numbers = [int(value) for value in _IMAGE_REF_RE.findall(prompt)]
    expected = set(range(1, media_count + 1))
    actual = set(numbers)
    if actual != expected:
        raise ReferencePromptError(
            f"prompt image references {sorted(actual)} do not match media "
            f"positions {sorted(expected)}"
        )
    if any(number < 1 or number > media_count for number in numbers):
        raise ReferencePromptError("prompt contains an out-of-range image reference")


def _timeline_from_segment(segment: GenerationSegment) -> list[TimelineSection]:
    sections: list[TimelineSection] = []
    for shot in segment.editorial_shots:
        sections.append(
            TimelineSection(
                start_seconds=shot.start_frame / segment.timeline_fps,
                end_seconds=shot.end_frame / segment.timeline_fps,
                action=shot.visual_action,
                camera=f"{shot.framing}; {shot.camera_movement}",
            )
        )
    return sections


def _text_digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
