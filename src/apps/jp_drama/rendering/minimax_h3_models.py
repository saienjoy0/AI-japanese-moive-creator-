"""MiniMax H3 V2 request, response, and reference-preflight contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


H3Status = Literal["queued", "running", "succeeded", "failed", "cancelled"]
H3Role = Literal[
    "first_frame",
    "last_frame",
    "reference_image",
    "reference_video",
    "reference_audio",
]


class H3Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class H3URL(H3Model):
    url: str = Field(min_length=1)


class H3ContentItem(H3Model):
    type: Literal["text", "image_url", "video_url", "audio_url"]
    text: str | None = None
    image_url: H3URL | None = None
    video_url: H3URL | None = None
    audio_url: H3URL | None = None
    role: H3Role | None = None

    @model_validator(mode="after")
    def validate_item(self) -> "H3ContentItem":
        fields = {
            "text": self.text,
            "image_url": self.image_url,
            "video_url": self.video_url,
            "audio_url": self.audio_url,
        }
        expected = {
            "text": "text",
            "image_url": "image_url",
            "video_url": "video_url",
            "audio_url": "audio_url",
        }[self.type]
        present = [name for name, value in fields.items() if value is not None]
        if present != [expected]:
            raise ValueError(f"{self.type} content must contain only {expected}")
        if self.type == "text":
            if not self.text or not self.text.strip():
                raise ValueError("text content must not be empty")
            if self.role is not None:
                raise ValueError("text content must not have a role")
        else:
            if self.role is None:
                raise ValueError(f"{self.type} content requires a role")
        allowed = {
            "image_url": {"first_frame", "last_frame", "reference_image"},
            "video_url": {"reference_video"},
            "audio_url": {"reference_audio"},
        }
        if self.type != "text" and self.role not in allowed[self.type]:
            raise ValueError(f"role {self.role} is invalid for {self.type}")
        return self

    @classmethod
    def text_item(cls, text: str) -> "H3ContentItem":
        return cls(type="text", text=text)

    @classmethod
    def media_item(cls, media_type: str, url: str, role: H3Role) -> "H3ContentItem":
        field = {
            "image_url": "image_url",
            "video_url": "video_url",
            "audio_url": "audio_url",
        }[media_type]
        return cls(type=media_type, role=role, **{field: H3URL(url=url)})


class H3VideoGenerationRequest(H3Model):
    model: Literal["MiniMax-H3"] = "MiniMax-H3"
    content: list[H3ContentItem] = Field(min_length=1)
    resolution: Literal["768P", "2K"]
    duration: int = Field(ge=4, le=15)
    ratio: Literal["9:16", "adaptive"]

    @model_validator(mode="after")
    def validate_mode(self) -> "H3VideoGenerationRequest":
        text_items = [item for item in self.content if item.type == "text"]
        if len(text_items) != 1:
            raise ValueError("H3 requires exactly one non-empty text content item")
        roles = [item.role for item in self.content if item.role is not None]
        frame_roles = {role for role in roles if role in {"first_frame", "last_frame"}}
        reference_roles = {
            role
            for role in roles
            if role in {"reference_image", "reference_video", "reference_audio"}
        }
        if frame_roles and reference_roles:
            raise ValueError("first/last-frame mode cannot be mixed with reference mode")
        if "last_frame" in frame_roles and "first_frame" not in frame_roles:
            raise ValueError("last_frame requires first_frame")
        if len([role for role in roles if role == "first_frame"]) > 1:
            raise ValueError("only one first_frame is allowed")
        if len([role for role in roles if role == "last_frame"]) > 1:
            raise ValueError("only one last_frame is allowed")
        if sum(role == "reference_image" for role in roles) > 9:
            raise ValueError("reference images exceed 9")
        if sum(role == "reference_video" for role in roles) > 3:
            raise ValueError("reference videos exceed 3")
        if sum(role == "reference_audio" for role in roles) > 3:
            raise ValueError("reference audios exceed 3")
        if frame_roles and self.ratio != "adaptive":
            raise ValueError("first/last-frame mode must use ratio=adaptive")
        if not frame_roles and self.ratio == "adaptive":
            raise ValueError("text/reference mode must use an explicit ratio")
        return self

    @property
    def mode(self) -> Literal["text", "first_frame", "reference"]:
        roles = {item.role for item in self.content if item.role is not None}
        if roles & {"first_frame", "last_frame"}:
            return "first_frame"
        if roles:
            return "reference"
        return "text"


class H3ReferenceAsset(H3Model):
    asset_id: str = Field(min_length=1)
    kind: Literal["image", "video", "audio"]
    url: str = Field(min_length=1)
    role: H3Role
    priority: int = Field(ge=1)
    size_bytes: int = Field(ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    fps: float | None = Field(default=None, ge=0)
    aspect_ratio: float | None = Field(default=None, gt=0)
    sha256: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_asset(self) -> "H3ReferenceAsset":
        expected_kind = {
            "first_frame": "image",
            "last_frame": "image",
            "reference_image": "image",
            "reference_video": "video",
            "reference_audio": "audio",
        }[self.role]
        if self.kind != expected_kind:
            raise ValueError(f"{self.role} requires kind={expected_kind}")
        if self.kind in {"video", "audio"} and self.duration_seconds is None:
            raise ValueError(f"{self.kind} reference requires duration_seconds")
        return self


class H3ReferenceBundle(H3Model):
    segment_id: str = Field(min_length=1)
    assets: list[H3ReferenceAsset] = Field(default_factory=list)

    def preflight_errors(self, *, max_request_bytes: int = 64 * 1024 * 1024) -> list[str]:
        errors: list[str] = []
        if sum(item.size_bytes for item in self.assets) > max_request_bytes:
            errors.append("request exceeds 64MB")
        images = [item for item in self.assets if item.role == "reference_image"]
        videos = [item for item in self.assets if item.role == "reference_video"]
        audios = [item for item in self.assets if item.role == "reference_audio"]
        if len(images) > 9:
            errors.append("reference images exceed 9")
        if len(videos) > 3:
            errors.append("reference videos exceed 3")
        if len(audios) > 3:
            errors.append("reference audios exceed 3")
        if sum(item.duration_seconds or 0 for item in videos) > 15:
            errors.append("reference video duration exceeds 15 seconds")
        if sum(item.duration_seconds or 0 for item in audios) > 15:
            errors.append("reference audio duration exceeds 15 seconds")
        for item in self.assets:
            if item.aspect_ratio is not None and not 0.4 <= item.aspect_ratio <= 2.5:
                errors.append(f"{item.asset_id} aspect ratio is outside 0.4-2.5")
            if item.kind == "video":
                if not 2 <= (item.duration_seconds or 0) <= 15:
                    errors.append(f"{item.asset_id} video duration is outside 2-15 seconds")
                if item.fps is None or not 23.976 <= item.fps <= 60:
                    errors.append(f"{item.asset_id} FPS is outside 23.976-60")
            if item.kind == "audio" and not 2 <= (item.duration_seconds or 0) <= 15:
                errors.append(f"{item.asset_id} audio duration is outside 2-15 seconds")
        return errors

    def require_valid(self, *, max_request_bytes: int = 64 * 1024 * 1024) -> None:
        errors = self.preflight_errors(max_request_bytes=max_request_bytes)
        if errors:
            raise ValueError("; ".join(errors))


class H3SubmitResult(H3Model):
    task_id: str = Field(min_length=1)
    request_id: str | None = None


class H3Usage(H3Model):
    data: dict[str, Any] = Field(default_factory=dict)


class H3QueryResult(H3Model):
    task_id: str = Field(min_length=1)
    status: H3Status
    output_url: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_terminal_result(self) -> "H3QueryResult":
        if self.status == "succeeded" and not self.output_url:
            raise ValueError("succeeded H3 task requires output_url")
        return self
