"""MiniMax H3 V2 HTTP client with injectable zero-cost transport."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .minimax_h3_config import MiniMaxH3ProviderConfig
from .minimax_h3_models import H3QueryResult, H3SubmitResult, H3VideoGenerationRequest


class MiniMaxH3ClientError(RuntimeError):
    """Structured H3 transport or API error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retryable: bool = False,
        submission_may_have_succeeded: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retryable = retryable
        self.submission_may_have_succeeded = submission_may_have_succeeded


class MiniMaxH3Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: int,
    ) -> tuple[int, dict[str, str], bytes]:
        ...


class UrllibMiniMaxH3Transport:
    """Small stdlib transport so the H3 core adds no dependency."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: int,
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read()
        except Exception as exc:
            raise MiniMaxH3ClientError(
                f"MiniMax H3 transport failed: {exc}",
                retryable=True,
                submission_may_have_succeeded=method.upper() == "POST",
            ) from exc

    def download_to(
        self,
        url: str,
        destination: Path,
        *,
        headers: dict[str, str],
        timeout_seconds: int,
        chunk_size: int = 1024 * 1024,
    ) -> Path:
        request = urllib.request.Request(url, headers=headers, method="GET")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
        )
        try:
            try:
                response = urllib.request.urlopen(request, timeout=timeout_seconds)
            except urllib.error.HTTPError as exc:
                body = exc.read()
                raise _http_error_from_payload(exc.code, body) from exc
            except Exception as exc:
                raise MiniMaxH3ClientError(
                    f"MiniMax H3 download transport failed: {exc}", retryable=True
                ) from exc
            with response, os.fdopen(fd, "wb") as handle:
                fd = -1
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if os.path.getsize(temp_name) == 0:
                raise MiniMaxH3ClientError(
                    "MiniMax H3 download returned an empty body", retryable=True
                )
            os.replace(temp_name, destination)
            return destination
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class MiniMaxH3Client:
    def __init__(
        self,
        config: MiniMaxH3ProviderConfig,
        *,
        transport: MiniMaxH3Transport | None = None,
        api_key: str | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibMiniMaxH3Transport()
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        key = self._api_key or self.config.api_key()
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def require_credentials(self) -> None:
        """Validate credentials without performing an external API call."""
        self._headers()

    def submit(self, request: H3VideoGenerationRequest) -> H3SubmitResult:
        payload = request.model_dump(mode="json", exclude_none=True)
        try:
            status, headers, body = self.transport.request(
                "POST",
                self.config.submit_url,
                headers=self._headers(),
                body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                timeout_seconds=self.config.request_timeout_seconds,
            )
            data = self._decode_response(status, body)
        except MiniMaxH3ClientError as exc:
            ambiguous = (
                exc.submission_may_have_succeeded
                or exc.status_code in {408, 429}
                or (exc.status_code is not None and exc.status_code >= 500)
                or exc.status_code is None
            )
            raise MiniMaxH3ClientError(
                str(exc),
                status_code=exc.status_code,
                error_code=exc.error_code,
                retryable=exc.retryable,
                submission_may_have_succeeded=ambiguous,
            ) from exc
        task_id = _strict_submit_task_id(data)
        if not task_id:
            raise MiniMaxH3ClientError(
                "MiniMax H3 submit response did not contain task_id at an approved path",
                status_code=status,
                submission_may_have_succeeded=True,
            )
        request_id = headers.get("x-request-id") or _direct_string(data, "request_id")
        return H3SubmitResult(task_id=task_id, request_id=request_id)

    def query(self, task_id: str) -> H3QueryResult:
        status, _, body = self.transport.request(
            "GET",
            self.config.query_url(task_id),
            headers=self._headers(),
            body=None,
            timeout_seconds=self.config.request_timeout_seconds,
        )
        data = self._decode_response(status, body)
        task = data.get("task") if isinstance(data.get("task"), dict) else data
        raw_status = str(task.get("status") or data.get("status") or "").lower()
        provider_status = {
            "preparing": "queued",
            "queueing": "queued",
            "processing": "running",
            "success": "succeeded",
            "fail": "failed",
            "canceled": "cancelled",
        }.get(raw_status, raw_status)
        if provider_status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise MiniMaxH3ClientError(
                f"MiniMax H3 returned unknown task status: {provider_status or '<empty>'}",
                status_code=status,
            )
        content = task.get("content") if isinstance(task.get("content"), dict) else {}
        nested_video_url = content.get("video_url")
        output_url = (
            content.get("url")
            or (
                nested_video_url.get("url")
                if isinstance(nested_video_url, dict)
                else nested_video_url
                if isinstance(nested_video_url, str)
                else None
            )
            or task.get("url")
            or _find_first_string(data, "video_url", "file_url", "url")
        )
        error = _extract_error_message(data) if provider_status in {"failed", "cancelled"} else None
        usage = (
            task.get("usage")
            if isinstance(task.get("usage"), dict)
            else data.get("usage")
            if isinstance(data.get("usage"), dict)
            else None
        )
        response_task_id = str(task.get("task_id") or task.get("id") or task_id)
        return H3QueryResult(
            task_id=response_task_id,
            status=provider_status,
            output_url=output_url,
            error=error,
            usage=usage,
        )

    def download(self, url: str, destination: str | Path) -> Path:
        path = Path(destination).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        streaming = getattr(self.transport, "download_to", None)
        if callable(streaming):
            return streaming(
                url,
                path,
                headers={"Accept": "video/mp4"},
                timeout_seconds=self.config.download_timeout_seconds,
            )
        status, _, body = self.transport.request(
            "GET",
            url,
            headers={"Accept": "video/mp4"},
            body=None,
            timeout_seconds=self.config.download_timeout_seconds,
        )
        if not 200 <= status < 300:
            raise self._http_error(status, body)
        if not body:
            raise MiniMaxH3ClientError("MiniMax H3 download returned an empty body", retryable=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".part", dir=path.parent
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return path

    def _decode_response(self, status: int, body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception as exc:
            raise MiniMaxH3ClientError(
                "MiniMax H3 returned invalid JSON",
                status_code=status,
                retryable=status >= 500,
            ) from exc
        if not 200 <= status < 300:
            raise self._http_error(status, body, data=data)
        if not isinstance(data, dict):
            raise MiniMaxH3ClientError(
                "MiniMax H3 response must be a JSON object", status_code=status
            )
        return data

    def _http_error(
        self,
        status: int,
        body: bytes,
        *,
        data: dict[str, Any] | None = None,
    ) -> MiniMaxH3ClientError:
        payload = data
        if payload is None:
            try:
                decoded = json.loads(body.decode("utf-8")) if body else {}
                payload = decoded if isinstance(decoded, dict) else {}
            except Exception:
                payload = {}
        return MiniMaxH3ClientError(
            _extract_error_message(payload) or f"MiniMax H3 HTTP {status}",
            status_code=status,
            error_code=_find_first_string(payload, "code", "error_code"),
            retryable=status in {408, 429} or status >= 500,
            submission_may_have_succeeded=False,
        )


def _strict_submit_task_id(data: dict[str, Any]) -> str | None:
    direct = _direct_string(data, "task_id")
    if direct:
        return direct
    task = data.get("task")
    if isinstance(task, dict):
        return _direct_string(task, "task_id") or _direct_string(task, "id")
    return None


def _direct_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value)
    return None


def _find_first_string(data: Any, *keys: str) -> str | None:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value)
        for value in data.values():
            found = _find_first_string(value, *keys)
            if found:
                return found
    if isinstance(data, list):
        for value in data:
            found = _find_first_string(value, *keys)
            if found:
                return found
    return None


def _extract_error_message(data: Any) -> str | None:
    return _find_first_string(data, "message", "error_message", "detail")


def _http_error_from_payload(status: int, body: bytes) -> MiniMaxH3ClientError:
    try:
        decoded = json.loads(body.decode("utf-8")) if body else {}
        payload = decoded if isinstance(decoded, dict) else {}
    except Exception:
        payload = {}
    return MiniMaxH3ClientError(
        _extract_error_message(payload) or f"MiniMax H3 HTTP {status}",
        status_code=status,
        error_code=_find_first_string(payload, "code", "error_code"),
        retryable=status in {408, 429} or status >= 500,
    )
