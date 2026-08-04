"""Wan 2.7 compatibility adapters built on the imported LumenX providers.

LumenX's generic adapters predate the Wan 2.7 request contracts. These thin
subclasses retain LumenX media resolution, authentication, downloads, and
public interfaces while correcting only the Wan 2.7 HTTP payloads.
"""

from __future__ import annotations

import time
from typing import Mapping, Optional

import requests

from src.models.image import WanxImageModel
from src.models.wanx import WanxModel
from src.utils import get_logger
from src.utils.endpoints import get_provider_base_url


logger = get_logger(__name__)


class Wan27ImageModel(WanxImageModel):
    """Send Wan 2.7 image requests without unsupported legacy parameters."""

    def _generate_dashscope_image_http(
        self,
        prompt: str,
        model_name: str,
        size: str = "960*1696",
        n: int = 1,
        negative_prompt: str | None = None,
        ref_image_paths: list | None = None,
        seed: int | None = None,
        prompt_extend: bool = True,
        watermark: bool = False,
    ) -> str:
        if not model_name.startswith("wan2.7-image"):
            return super()._generate_dashscope_image_http(
                prompt=prompt,
                model_name=model_name,
                size=size,
                n=n,
                negative_prompt=negative_prompt,
                ref_image_paths=ref_image_paths,
                seed=seed,
                prompt_extend=prompt_extend,
                watermark=watermark,
            )

        # Wan 2.7 does not accept negative_prompt or prompt_extend. Exclusions
        # must be expressed in the positive prompt and thinking_mode is the
        # supported prompt-reasoning control.
        _ = negative_prompt, prompt_extend
        base = get_provider_base_url("DASHSCOPE")
        create_url = f"{base}/api/v1/services/aigc/image-generation/generation"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-DashScope-Async": "enable",
        }

        content: list[dict[str, str]] = []
        for path in (ref_image_paths or [])[:9]:
            image_input = self._resolve_wan26_reference_image(path, model_name=model_name)
            if image_input:
                content.append({"image": image_input})
        content.append({"text": prompt})

        parameters: dict[str, object] = {
            "n": n,
            "size": size,
            "watermark": watermark,
            "thinking_mode": bool(self.params.get("thinking_mode", True)),
        }
        if seed is not None:
            parameters["seed"] = seed

        payload = {
            "model": model_name,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": parameters,
        }
        logger.info("Calling %s with Wan 2.7 image payload", model_name)
        response = requests.post(create_url, headers=headers, json=payload, timeout=120)
        if response.status_code != 200:
            data = response.json() if response.text else {}
            raise RuntimeError(
                f"{model_name} task creation failed: {data.get('message', response.text)}"
            )
        task_id = response.json().get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"No task_id in response: {response.json()}")
        return self._poll_image_task(base, task_id, model_name)

    def _poll_image_task(self, base: str, task_id: str, model_name: str) -> str:
        poll_url = f"{base}/api/v1/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        elapsed = 0
        while elapsed < 600:
            time.sleep(10)
            elapsed += 10
            response = requests.get(poll_url, headers=headers, timeout=30)
            if response.status_code != 200:
                continue
            result = response.json()
            output = result.get("output", {})
            status = output.get("task_status")
            if status == "SUCCEEDED":
                choices = output.get("choices", [])
                content = choices[0].get("message", {}).get("content", []) if choices else []
                image_url = content[0].get("image") if content else None
                if not image_url:
                    raise RuntimeError(f"No image URL in completed task: {result}")
                return image_url
            if status == "FAILED":
                raise RuntimeError(
                    f"{model_name} task failed: {output.get('code', '')} - "
                    f"{output.get('message', 'Unknown error')}"
                )
            if status in {"CANCELED", "UNKNOWN"}:
                raise RuntimeError(f"{model_name} task {status}: {result}")
        raise RuntimeError(f"{model_name} task timed out after 600s")


class Wan27VideoModel(WanxModel):
    """Use the Wan 2.7 unified media-array I2V protocol."""

    def _generate_wan_i2v_http(
        self,
        prompt: str,
        img_url: str,
        model_name: str = "wan2.7-i2v",
        resolution: str | None = "720P",
        ratio: Optional[str] = None,
        duration: int = 5,
        prompt_extend: bool = True,
        negative_prompt: str | None = None,
        audio_url: str | None = None,
        watermark: bool = False,
        seed: int | None = None,
        shot_type: str = "single",
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        if not model_name.startswith("wan2.7-i2v"):
            return super()._generate_wan_i2v_http(
                prompt=prompt,
                img_url=img_url,
                model_name=model_name,
                resolution=resolution or "720P",
                ratio=ratio,
                duration=duration,
                prompt_extend=prompt_extend,
                negative_prompt=negative_prompt,
                audio_url=audio_url,
                watermark=watermark,
                seed=seed,
                shot_type=shot_type,
                extra_headers=extra_headers,
            )

        # Wan 2.7 derives aspect ratio from the first frame. ratio and
        # shot_type are legacy parameters and must not be sent.
        _ = ratio, shot_type
        final_resolution = (resolution or self.params.get("resolution") or "720P").upper()
        if final_resolution not in {"720P", "1080P"}:
            raise ValueError("Wan 2.7 I2V resolution must be 720P or 1080P")

        base = get_provider_base_url("DASHSCOPE")
        create_url = f"{base}/api/v1/services/aigc/video-generation/video-synthesis"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-DashScope-Async": "enable",
        }
        if extra_headers:
            headers.update(dict(extra_headers))

        media = [{"type": "first_frame", "url": img_url}]
        if audio_url:
            media.append({"type": "driving_audio", "url": audio_url})
        input_payload: dict[str, object] = {"prompt": prompt, "media": media}
        if negative_prompt:
            input_payload["negative_prompt"] = negative_prompt
        parameters: dict[str, object] = {
            "resolution": final_resolution,
            "duration": duration,
            "prompt_extend": prompt_extend,
            "watermark": watermark,
        }
        if seed is not None:
            parameters["seed"] = seed

        payload = {
            "model": model_name,
            "input": input_payload,
            "parameters": parameters,
        }
        logger.info("Calling %s with Wan 2.7 media-array payload", model_name)
        response = requests.post(create_url, headers=headers, json=payload, timeout=120)
        if response.status_code != 200:
            data = response.json() if response.text else {}
            raise RuntimeError(
                f"{model_name} task creation failed: {data.get('message', response.text)}"
            )
        task_id = response.json().get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"No task_id in response: {response.json()}")
        return self._poll_video_task(base, task_id, model_name)

    def _poll_video_task(self, base: str, task_id: str, model_name: str) -> str:
        poll_url = f"{base}/api/v1/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        elapsed = 0
        while elapsed < 900:
            time.sleep(15)
            elapsed += 15
            response = requests.get(poll_url, headers=headers, timeout=30)
            if response.status_code != 200:
                continue
            result = response.json()
            output = result.get("output", {})
            status = output.get("task_status")
            if status == "SUCCEEDED":
                video_url = output.get("video_url")
                if not video_url:
                    raise RuntimeError(f"No video_url in completed task: {result}")
                return video_url
            if status == "FAILED":
                raise RuntimeError(
                    f"{model_name} task failed: {output.get('code', '')} - "
                    f"{output.get('message', 'Unknown error')}"
                )
            if status in {"CANCELED", "UNKNOWN"}:
                raise RuntimeError(f"{model_name} task {status}: {result}")
        raise RuntimeError(f"{model_name} task timed out after 900s")
