"""Official Alibaba Cloud HappyHorse 1.1 video transports.

The I2V and R2V adapters share the same asynchronous task boundary:
provider task IDs are persisted before polling, saved tasks can be resumed
without another paid POST, and returned MP4 files are downloaded atomically by
the inherited video model.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests

from src.utils import get_logger
from src.utils.endpoints import get_provider_base_url
from src.utils.oss_utils import OSSImageUploader
from src.utils.provider_media import resolve_media_input, resolve_media_inputs

from .wan27_adapters import Wan27VideoModel


logger = get_logger(__name__)


def _submit_or_resume_happyhorse(
    model: Wan27VideoModel,
    *,
    model_name: str,
    payload: dict[str, Any] | None,
    media_urls: Sequence[str],
    extra_headers: Mapping[str, str] | None = None,
) -> str:
    """Submit one official task or resume a previously persisted task ID."""

    base = get_provider_base_url("DASHSCOPE")
    task_id = model._resume_task_id
    if task_id:
        logger.info("Resuming existing %s task %s", model_name, task_id)
        return model._poll_video_task(base, task_id, model_name)

    if payload is None:
        raise ValueError("new HappyHorse submissions require an exact payload")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {model.api_key}",
        "X-DashScope-Async": "enable",
    }
    if extra_headers:
        headers.update({key: value for key, value in extra_headers.items() if value})
    if any(url.startswith("oss://") for url in media_urls):
        headers["X-DashScope-OssResourceResolve"] = "enable"

    response = requests.post(
        f"{base}/api/v1/services/aigc/video-generation/video-synthesis",
        headers=headers,
        json=payload,
        timeout=120,
    )
    if response.status_code != 200:
        data = response.json() if response.text else {}
        message = data.get("message", response.text)
        raise RuntimeError(f"{model_name} task creation failed: {message}")

    result = response.json()
    task_id = result.get("output", {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"No task_id in response: {result}")
    model._notify_task_submitted(task_id, response)
    return model._poll_video_task(base, task_id, model_name)


class HappyHorse11I2VModel(Wan27VideoModel):
    """HappyHorse 1.1 I2V client using the official first-frame payload."""

    MODEL_NAME = "happyhorse-1.1-i2v"
    MIN_DURATION_SECONDS = 3
    MAX_DURATION_SECONDS = 15
    SUPPORTED_RESOLUTIONS = frozenset({"720P", "1080P"})

    @classmethod
    def build_request_payload(
        cls,
        *,
        prompt: str,
        first_frame_url: str,
        resolution: str,
        duration: int,
        watermark: bool = False,
        seed: int | None = None,
    ) -> dict[str, Any]:
        normalized_prompt = prompt.strip()
        normalized_url = first_frame_url.strip()
        normalized_resolution = resolution.upper()
        if not normalized_prompt:
            raise ValueError("HappyHorse prompt must not be empty")
        if not normalized_url:
            raise ValueError("HappyHorse I2V requires exactly one first-frame URL")
        if normalized_resolution not in cls.SUPPORTED_RESOLUTIONS:
            raise ValueError("HappyHorse resolution must be 720P or 1080P")
        if not cls.MIN_DURATION_SECONDS <= duration <= cls.MAX_DURATION_SECONDS:
            raise ValueError("HappyHorse duration must be between 3 and 15 seconds")
        if seed is not None and not 0 <= seed <= 2_147_483_647:
            raise ValueError("HappyHorse seed must be between 0 and 2147483647")

        parameters: dict[str, Any] = {
            "resolution": normalized_resolution,
            "duration": duration,
            "watermark": bool(watermark),
        }
        if seed is not None:
            parameters["seed"] = seed

        return {
            "model": cls.MODEL_NAME,
            "input": {
                "prompt": normalized_prompt,
                "media": [{"type": "first_frame", "url": normalized_url}],
            },
            "parameters": parameters,
        }

    def generate(
        self,
        prompt: str,
        output_path: str,
        img_path: str | None = None,
        model_name: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, float]:
        final_model_name = (model_name or self.MODEL_NAME).strip()
        if final_model_name != self.MODEL_NAME:
            raise ValueError(
                f"HappyHorse11I2VModel only supports {self.MODEL_NAME}, "
                f"not {final_model_name}"
            )

        image_ref = img_path or kwargs.get("img_url")
        if not image_ref:
            raise ValueError("HappyHorse I2V requires one first-frame image")

        resolution = str(
            kwargs.get("resolution")
            or self.params.get("resolution")
            or "720P"
        ).upper()
        duration = int(kwargs.get("duration", self.params.get("duration", 5)))
        watermark = bool(kwargs.get("watermark", self.params.get("watermark", False)))
        seed = kwargs.get("seed", self.params.get("seed"))
        if seed is not None:
            seed = int(seed)

        resolved_url = str(image_ref)
        headers: Mapping[str, str] | None = None
        if not self._resume_task_id:
            uploader = OSSImageUploader()
            backend = self._resolve_provider_backend_for_model(final_model_name)
            resolved = resolve_media_input(
                str(image_ref),
                model_name=final_model_name,
                modality="image",
                backend=backend,
                uploader=uploader,
                dashscope_temp_url_resolver=lambda local_path: self._create_dashscope_temp_url(
                    local_path,
                    final_model_name,
                ),
            )
            resolved_url = resolved.value
            headers = resolved.headers

        started = time.time()
        video_url = self._generate_happyhorse_i2v_http(
            prompt=prompt,
            first_frame_url=resolved_url,
            resolution=resolution,
            duration=duration,
            watermark=watermark,
            seed=seed,
            extra_headers=headers,
        )
        self._download_video(video_url, output_path)
        return output_path, time.time() - started

    def _generate_happyhorse_i2v_http(
        self,
        *,
        prompt: str,
        first_frame_url: str,
        resolution: str,
        duration: int,
        watermark: bool,
        seed: int | None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> str:
        payload = None
        if not self._resume_task_id:
            payload = self.build_request_payload(
                prompt=prompt,
                first_frame_url=first_frame_url,
                resolution=resolution,
                duration=duration,
                watermark=watermark,
                seed=seed,
            )
        return _submit_or_resume_happyhorse(
            self,
            model_name=self.MODEL_NAME,
            payload=payload,
            media_urls=[first_frame_url],
            extra_headers=extra_headers,
        )


class HappyHorse11R2VModel(Wan27VideoModel):
    """HappyHorse 1.1 R2V client for one to nine ordered reference images."""

    MODEL_NAME = "happyhorse-1.1-r2v"
    MIN_DURATION_SECONDS = 3
    MAX_DURATION_SECONDS = 15
    MIN_REFERENCE_IMAGES = 1
    MAX_REFERENCE_IMAGES = 9
    SUPPORTED_RESOLUTIONS = frozenset({"720P", "1080P"})
    SUPPORTED_RATIOS = frozenset({"9:16"})

    @classmethod
    def build_request_payload(
        cls,
        *,
        prompt: str,
        reference_image_urls: Sequence[str],
        resolution: str,
        ratio: str,
        duration: int,
        watermark: bool = False,
        seed: int | None = None,
    ) -> dict[str, Any]:
        normalized_prompt = prompt.strip()
        urls = [str(url).strip() for url in reference_image_urls]
        normalized_resolution = resolution.upper()
        normalized_ratio = ratio.strip()

        if not normalized_prompt:
            raise ValueError("HappyHorse prompt must not be empty")
        if not cls.MIN_REFERENCE_IMAGES <= len(urls) <= cls.MAX_REFERENCE_IMAGES:
            raise ValueError("HappyHorse R2V requires between 1 and 9 reference images")
        if any(not url for url in urls):
            raise ValueError("HappyHorse R2V reference image URLs must not be empty")
        if len(urls) != len(set(urls)):
            raise ValueError("HappyHorse R2V reference image URLs must be unique")
        if normalized_resolution not in cls.SUPPORTED_RESOLUTIONS:
            raise ValueError("HappyHorse resolution must be 720P or 1080P")
        if normalized_ratio not in cls.SUPPORTED_RATIOS:
            raise ValueError("HappyHorse R2V Canary requires ratio 9:16")
        if not cls.MIN_DURATION_SECONDS <= duration <= cls.MAX_DURATION_SECONDS:
            raise ValueError("HappyHorse duration must be between 3 and 15 seconds")
        if seed is not None and not 0 <= seed <= 2_147_483_647:
            raise ValueError("HappyHorse seed must be between 0 and 2147483647")

        parameters: dict[str, Any] = {
            "resolution": normalized_resolution,
            "ratio": normalized_ratio,
            "duration": duration,
            "watermark": bool(watermark),
        }
        if seed is not None:
            parameters["seed"] = seed

        return {
            "model": cls.MODEL_NAME,
            "input": {
                "prompt": normalized_prompt,
                "media": [
                    {"type": "reference_image", "url": url}
                    for url in urls
                ],
            },
            "parameters": parameters,
        }

    def generate(
        self,
        prompt: str,
        output_path: str,
        img_path: str | None = None,
        model_name: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, float]:
        final_model_name = (model_name or self.MODEL_NAME).strip()
        if final_model_name != self.MODEL_NAME:
            raise ValueError(
                f"HappyHorse11R2VModel only supports {self.MODEL_NAME}, "
                f"not {final_model_name}"
            )

        references = (
            kwargs.get("reference_image_paths")
            or kwargs.get("reference_image_urls")
            or kwargs.get("ref_image_urls")
            or []
        )
        if not self._resume_task_id and not references:
            raise ValueError("HappyHorse R2V requires reference images")

        resolution = str(
            kwargs.get("resolution")
            or self.params.get("resolution")
            or "720P"
        ).upper()
        ratio = str(kwargs.get("ratio") or self.params.get("ratio") or "9:16")
        duration = int(kwargs.get("duration", self.params.get("duration", 5)))
        watermark = bool(kwargs.get("watermark", self.params.get("watermark", False)))
        seed = kwargs.get("seed", self.params.get("seed"))
        if seed is not None:
            seed = int(seed)

        urls: list[str] = []
        headers: dict[str, str] = {}
        if not self._resume_task_id:
            refs = [str(item) for item in references]
            if not self.MIN_REFERENCE_IMAGES <= len(refs) <= self.MAX_REFERENCE_IMAGES:
                raise ValueError(
                    "HappyHorse R2V requires between 1 and 9 reference images"
                )
            uploader = OSSImageUploader()
            backend = self._resolve_provider_backend_for_model(final_model_name)
            resolved = resolve_media_inputs(
                refs,
                model_name=final_model_name,
                modality="image",
                backend=backend,
                uploader=uploader,
                dashscope_temp_url_resolver=lambda local_path: self._create_dashscope_temp_url(
                    local_path,
                    final_model_name,
                ),
            )
            urls = [item.value for item in resolved]
            for item in resolved:
                headers.update(
                    {key: value for key, value in item.headers.items() if value}
                )

        started = time.time()
        video_url = self._generate_happyhorse_r2v_http(
            prompt=prompt,
            reference_image_urls=urls,
            resolution=resolution,
            ratio=ratio,
            duration=duration,
            watermark=watermark,
            seed=seed,
            extra_headers=headers,
        )
        self._download_video(video_url, output_path)
        return output_path, time.time() - started

    def _generate_happyhorse_r2v_http(
        self,
        *,
        prompt: str,
        reference_image_urls: Sequence[str],
        resolution: str,
        ratio: str,
        duration: int,
        watermark: bool,
        seed: int | None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> str:
        payload = None
        if not self._resume_task_id:
            payload = self.build_request_payload(
                prompt=prompt,
                reference_image_urls=reference_image_urls,
                resolution=resolution,
                ratio=ratio,
                duration=duration,
                watermark=watermark,
                seed=seed,
            )
        return _submit_or_resume_happyhorse(
            self,
            model_name=self.MODEL_NAME,
            payload=payload,
            media_urls=list(reference_image_urls),
            extra_headers=extra_headers,
        )


def require_local_first_frame(path: str | Path) -> Path:
    """Validate the local hand-off before upload; provider validates dimensions."""

    resolved = Path(path).resolve()
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise ValueError(f"approved first frame is missing or empty: {resolved}")
    if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError(
            "HappyHorse first frame must be PNG, JPG, JPEG, or WEBP"
        )
    if resolved.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("HappyHorse first frame exceeds the official 20 MB limit")
    return resolved
