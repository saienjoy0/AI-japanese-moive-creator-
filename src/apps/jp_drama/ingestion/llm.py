"""Structured-script LLM adapters with deterministic fixture support."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import StructuredScriptDraft


class ScriptLLMError(RuntimeError):
    """The script structuring provider failed or returned invalid JSON."""


@runtime_checkable
class StructuredScriptLLM(Protocol):
    provider_id: str
    external: bool
    calls: int

    def generate(
        self,
        normalized_script: str,
        *,
        previous_payload: dict[str, Any] | None = None,
        validation_errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        ...


class FixtureStructuredScriptLLM:
    provider_id = "fixture"
    external = False

    def __init__(
        self,
        payload: dict[str, Any] | list[dict[str, Any]] | str | Path,
    ) -> None:
        if isinstance(payload, (str, Path)):
            loaded = json.loads(Path(payload).read_text(encoding="utf-8"))
        else:
            loaded = payload
        self._payloads = loaded if isinstance(loaded, list) else [loaded]
        if not self._payloads:
            raise ValueError("fixture payload list cannot be empty")
        self.calls = 0

    def generate(
        self,
        normalized_script: str,
        *,
        previous_payload: dict[str, Any] | None = None,
        validation_errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        index = min(self.calls, len(self._payloads) - 1)
        self.calls += 1
        return json.loads(json.dumps(self._payloads[index], ensure_ascii=False))


class DashScopeStructuredScriptLLM:
    provider_id = "dashscope"
    external = True

    def __init__(
        self,
        *,
        model: str = "qwen-plus",
        api_key_env: str = "DASHSCOPE_API_KEY",
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.calls = 0

    def generate(
        self,
        normalized_script: str,
        *,
        previous_payload: dict[str, Any] | None = None,
        validation_errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ScriptLLMError(
                f"missing DashScope credential environment variable: {self.api_key_env}"
            )
        try:
            from dashscope import Generation
        except Exception as exc:  # pragma: no cover - installation failure
            raise ScriptLLMError(
                "dashscope SDK is unavailable; install repository dependencies"
            ) from exc

        schema = StructuredScriptDraft.model_json_schema()
        system = (
            "あなたは日本語縦型ショートドラマの構成担当です。"
            "入力台本から人物、場所、場面、動作、台詞を抽出し、"
            "必ず指定JSON Schemaだけを返してください。"
            "事実が不明でも生成に必須でない項目はunresolved_itemsへ入れ、"
            "人物や台詞を勝手に増やさないでください。"
            "character_id、scene_id、beat_idは英数字と._-だけを使用してください。"
            "beatsは物語順に最低3件、orderは1から連番にしてください。"
        )
        user_payload: dict[str, Any] = {
            "script": normalized_script,
            "target_schema": schema,
        }
        if previous_payload is not None:
            user_payload["previous_payload"] = previous_payload
            user_payload["validation_errors"] = validation_errors or []
            user_payload["instruction"] = (
                "前回JSONの検証エラーだけを修正し、台本の内容は変更しない"
            )

        self.calls += 1
        try:
            response = Generation.call(
                model=self.model,
                api_key=api_key,
                messages=[
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": json.dumps(
                            user_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
                result_format="message",
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # pragma: no cover - network/provider failure
            raise ScriptLLMError(f"DashScope request failed: {exc}") from exc

        status_code = getattr(response, "status_code", 200)
        if status_code != 200:
            message = getattr(response, "message", "unknown provider error")
            raise ScriptLLMError(
                f"DashScope returned status {status_code}: {message}"
            )
        content = _extract_content(response)
        try:
            payload = json.loads(_strip_markdown_json(content))
        except json.JSONDecodeError as exc:
            raise ScriptLLMError("DashScope returned non-JSON content") from exc
        if not isinstance(payload, dict):
            raise ScriptLLMError("DashScope JSON root must be an object")
        return payload


def _extract_content(response: Any) -> str:
    output = getattr(response, "output", None)
    if output is None and isinstance(response, dict):
        output = response.get("output")
    choices = (
        output.get("choices")
        if isinstance(output, dict)
        else getattr(output, "choices", None)
    )
    if not choices:
        raise ScriptLLMError("DashScope response contains no choices")
    first = choices[0]
    message = (
        first.get("message")
        if isinstance(first, dict)
        else getattr(first, "message", None)
    )
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", None)
    )
    if not isinstance(content, str) or not content.strip():
        raise ScriptLLMError("DashScope response contains no message content")
    return content


def _strip_markdown_json(content: str) -> str:
    fenced = re.search(
        r"```(?:json)?\s*(.*?)\s*```",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return fenced.group(1).strip() if fenced else content.strip()
