"""Wan 2.7 compatibility adapters built on the imported LumenX providers.

The adapters preserve the imported public interfaces while correcting Wan 2.7
payloads, using the exact target model for temporary uploads, and exposing
restart-safe async task hooks to the canary ledger.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, Mapping, Optional

import requests

from src.models.image import WanxImageModel
from src.models.wanx import WanxModel
from src.utils import get_logger
from src.utils.endpoints import get_provider_base_url


logger = get_logger(__name__)

TaskSubmittedCallback = Callable[[str, str | None], None]


class _AsyncTaskHooks:
    def _init_task_hooks(self) -> None:
        self._resume_task_id: str | None = None
        self._task_submitted_callback: TaskSubmittedCallback | None = None

    def configure_operation(
        self,
        *,
        resume_task_id: str | None = None,
        on_task_submitted: TaskSubmittedCallback | None = None,
    ) -> None:
        self._resume_task_id = resume_task_id
        self._task_submitted_callback = on_task_submitted

    def clear_operation(self) -> None:
        self._resume_task_id = None
        self._task_submitted_callback = None

    def _notify_task_submitted(self, task_id: str, response: requests.Response) -> None:
        if self._task_submitted_callback is None:
            return
        payload = response.json() if response.text else {}
        request_id = (
            payload.get("request_id")
            or payload.get("output", {}).get("request_id")
            or getattr(response, "headers", {}).get("X-DashScope-Request-Id")
        )
        self._task_submitted_callback(task_id, request_id)


class Wan27ImageModel(_AsyncTaskHooks, WanxImageModel):
    """Send Wan 2.7 image requests without unsupported legacy parameters."""

    def __init__(self, config):
        super().__init__(config)
        self._init_task_hooks()

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

        task_id = self._resume_task_id
        if task_id:
            logger.info("Resuming existing %s image task %s", model_name, task_id)
        else:
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
            self._notify_task_submitted(task_id, response)
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


class Wan27VideoModel(_AsyncTaskHooks, WanxModel):
    """Use the Wan 2.7 unified media-array protocol with exact-model uploads."""

    def __init__(self, config):
        super().__init__(config)
        self._init_task_hooks()

    def _resolver_model_for_media(self, model_name: str) -> str:
        normalized = (model_name or "").strip().lower()
        if normalized.startswith("wan2.7-i2v") or normalized.startswith("wan2.7-r2v"):
            return model_name
        return super()._resolver_model_for_media(model_name)

    def _create_dashscope_temp_url(self, local_path: str, model_name: str) -> str:
        if not os.path.exists(local_path):
            raise FileNotFoundError(f"Local media file not found: {local_path}")

        upload_base = os.getenv("DASHSCOPE_UPLOAD_BASE_URL", "").strip().rstrip("/")
        if not upload_base:
            upload_base = get_provider_base_url("DASHSCOPE")
        policy_url = f"{upload_base}/api/v1/uploads"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        policy_resp = requests.get(
            policy_url,
            params={"action": "getPolicy", "model": model_name},
            headers=headers,
            timeout=30,
        )
        if policy_resp.status_code != 200:
            raise RuntimeError(
                f"Failed to get DashScope upload policy (HTTP {policy_resp.status_code}): "
                f"{policy_resp.text}"
            )

        policy_body = policy_resp.json()
        policy_data = policy_body.get("output") or policy_body.get("data") or policy_body
        upload_host = policy_data.get("upload_host") or policy_data.get("host")
        if not upload_host:
            raise RuntimeError(f"DashScope upload policy missing upload_host: {policy_body}")

        upload_dir = policy_data.get("upload_dir") or policy_data.get("dir") or ""
        object_key = (
            policy_data.get("upload_file_path")
            or policy_data.get("object_key")
            or policy_data.get("key")
            or policy_data.get("file_path")
        )
        if not object_key:
            filename = os.path.basename(local_path)
            object_key = f"{upload_dir.rstrip('/')}/{filename}" if upload_dir else filename

        form_data: Dict[str, str] = {"key": object_key}
        field_map = {
            "policy": "policy",
            "signature": "signature",
            "oss_access_key_id": "OSSAccessKeyId",
            "x_oss_security_token": "x-oss-security-token",
            "x_oss_signature_version": "x-oss-signature-version",
            "x_oss_credential": "x-oss-credential",
            "x_oss_date": "x-oss-date",
            "x_oss_signature": "x-oss-signature",
            "x_oss_object_acl": "x-oss-object-acl",
            "x_oss_forbid_overwrite": "x-oss-forbid-overwrite",
            "success_action_status": "success_action_status",
            "callback": "callback",
        }
        for source_key, target_key in field_map.items():
            value = policy_data.get(source_key)
            if value:
                form_data[target_key] = str(value)
        form_data.setdefault("x-oss-object-acl", "private")
        form_data.setdefault("x-oss-forbid-overwrite", "true")
        for key, value in policy_data.items():
            if key.startswith("x-oss-") and value and key not in form_data:
                form_data[key] = str(value)

        with open(local_path, "rb") as file_handle:
            files = {"file": (os.path.basename(local_path), file_handle)}
            upload_resp = requests.post(
                upload_host,
                data=form_data,
                files=files,
                timeout=120,
            )
        if upload_resp.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Failed to upload temp media to DashScope (HTTP {upload_resp.status_code}): "
                f"{upload_resp.text}"
            )
        return f"oss://{object_key}"

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
        if any(
            isinstance(item.get("url"), str) and item["url"].startswith("oss://")
            for item in media
        ):
            headers["X-DashScope-OssResourceResolve"] = "enable"

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

        task_id = self._resume_task_id
        if task_id:
            logger.info("Resuming existing %s video task %s", model_name, task_id)
        else:
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
            self._notify_task_submitted(task_id, response)
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
