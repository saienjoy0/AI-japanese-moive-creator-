"""Official Alibaba Cloud HappyHorse 1.1 image-to-video transport.

This adapter intentionally implements the public DashScope asynchronous API
contract instead of inventing a project-specific protocol. It accepts exactly
one approved first frame, persists provider task IDs through the inherited
canary hooks, polls the official task endpoint, and downloads the returned MP4.

Voice IDs and external TTS are deliberately outside this adapter. The caller
may inspect the returned MP4 for a provider-generated audio stream and decide
whether the native-audio route is usable.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

import requests

from src.utils import get_logger
from src.utils.endpoints import get_provider_base_url
from src.utils.oss_utils import OSSImageUploader
from src.utils.provider_media import resolve_media_input

from .wan27_adapters import Wan27VideoModel


logger = get_logger(__name__)


class HappyHorse11I2VModel(Wan27VideoModel):
    """HappyHorse 1.1 I2V client using Alibaba Cloud's official HTTP payload."""

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
        """Build the exact documented HappyHorse 1.1 I2V request body."""

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
                "media": [
                    {
                        "type": "first_frame",
                        "url": normalized_url,
                    }
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
        """Submit or resume one official HappyHorse 1.1 I2V task."""

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
        raw_duration = kwargs.get("duration", self.params.get("duration", 5))
        duration = int(raw_duration)
        watermark = bool(kwargs.get("watermark", self.params.get("watermark", False)))
        seed = kwargs.get("seed", self.params.get("seed"))
        if seed is not None:
            seed = int(seed)

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

        started = time.time()
        video_url = self._generate_happyhorse_i2v_http(
            prompt=prompt,
            first_frame_url=resolved.value,
            resolution=resolution,
            duration=duration,
            watermark=watermark,
            seed=seed,
            extra_headers=resolved.headers,
        )
        self._download_video(video_url, output_path)
        elapsed = time.time() - started
        return output_path, elapsed

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
        base = get_provider_base_url("DASHSCOPE")
        create_url = (
            f"{base}/api/v1/services/aigc/video-generation/video-synthesis"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-DashScope-Async": "enable",
        }
        if extra_headers:
            headers.update({key: value for key, value in extra_headers.items() if value})
        if first_frame_url.startswith("oss://"):
            headers["X-DashScope-OssResourceResolve"] = "enable"

        payload = self.build_request_payload(
            prompt=prompt,
            first_frame_url=first_frame_url,
            resolution=resolution,
            duration=duration,
            watermark=watermark,
            seed=seed,
        )

        task_id = self._resume_task_id
        if task_id:
            logger.info("Resuming existing %s task %s", self.MODEL_NAME, task_id)
        else:
            logger.info("Calling %s with official I2V payload", self.MODEL_NAME)
            response = requests.post(
                create_url,
                headers=headers,
                json=payload,
                timeout=120,
            )
            if response.status_code != 200:
                data = response.json() if response.text else {}
                message = data.get("message", response.text)
                raise RuntimeError(
                    f"{self.MODEL_NAME} task creation failed: {message}"
                )
            result = response.json()
            task_id = result.get("output", {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"No task_id in response: {result}")
            self._notify_task_submitted(task_id, response)

        return self._poll_video_task(base, task_id, self.MODEL_NAME)


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
