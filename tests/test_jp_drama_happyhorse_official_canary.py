from __future__ import annotations

from typing import Any

import pytest

from src.apps.jp_drama.assets.models import (
    AssetReadinessIssue,
    AssetReadinessReport,
)
from src.apps.jp_drama.generation.models import PromptBundle
from src.apps.jp_drama.rendering.happyhorse11 import HappyHorse11I2VModel
from src.apps.jp_drama.workflows.render_happyhorse_segment_canary import (
    allow_provider_native_voice,
    build_happyhorse_prompt,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def test_official_payload_uses_one_first_frame_and_no_voice_fields() -> None:
    payload = HappyHorse11I2VModel.build_request_payload(
        prompt="少年が自然な日本語で短く話す",
        first_frame_url="oss://bucket/frame.png",
        resolution="720P",
        duration=5,
        watermark=False,
        seed=0,
    )

    assert payload == {
        "model": "happyhorse-1.1-i2v",
        "input": {
            "prompt": "少年が自然な日本語で短く話す",
            "media": [
                {
                    "type": "first_frame",
                    "url": "oss://bucket/frame.png",
                }
            ],
        },
        "parameters": {
            "resolution": "720P",
            "duration": 5,
            "watermark": False,
            "seed": 0,
        },
    }
    canonical = repr(payload)
    assert "voice_id" not in canonical
    assert "audio_url" not in canonical
    assert "ratio" not in payload["parameters"]


@pytest.mark.parametrize("duration", [2, 16])
def test_official_payload_rejects_unsupported_duration(duration: int) -> None:
    with pytest.raises(ValueError, match="between 3 and 15"):
        HappyHorse11I2VModel.build_request_payload(
            prompt="test",
            first_frame_url="https://example.test/frame.png",
            resolution="720P",
            duration=duration,
        )


def test_official_http_submission_persists_task_id_immediately(monkeypatch) -> None:
    posted: dict[str, Any] = {}
    submitted: list[tuple[str, str | None]] = []

    class FakeResponse:
        status_code = 200
        text = '{"output":{"task_id":"task-123"},"request_id":"request-456"}'
        headers: dict[str, str] = {}

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "output": {"task_id": "task-123"},
                "request_id": "request-456",
            }

    def fake_post(url, *, headers, json, timeout):
        posted.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.get_provider_base_url",
        lambda _provider: "https://workspace.example.test",
    )
    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.requests.post",
        fake_post,
    )

    model = HappyHorse11I2VModel({"params": {}})
    monkeypatch.setattr(
        model,
        "_poll_video_task",
        lambda base, task_id, model_name: "https://example.test/result.mp4",
    )
    model.configure_operation(
        on_task_submitted=lambda task_id, request_id: submitted.append(
            (task_id, request_id)
        )
    )

    result = model._generate_happyhorse_i2v_http(
        prompt="test prompt",
        first_frame_url="oss://bucket/frame.png",
        resolution="720P",
        duration=5,
        watermark=False,
        seed=42,
    )

    assert result == "https://example.test/result.mp4"
    assert submitted == [("task-123", "request-456")]
    assert posted["headers"]["X-DashScope-Async"] == "enable"
    assert posted["headers"]["X-DashScope-OssResourceResolve"] == "enable"
    assert posted["json"]["model"] == "happyhorse-1.1-i2v"
    assert posted["json"]["input"]["media"] == [
        {"type": "first_frame", "url": "oss://bucket/frame.png"}
    ]


def test_resume_uses_saved_task_without_second_paid_submission(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.get_provider_base_url",
        lambda _provider: "https://workspace.example.test",
    )

    def reject_post(*args, **kwargs):
        raise AssertionError("resume must not create a second provider task")

    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.requests.post",
        reject_post,
    )

    model = HappyHorse11I2VModel({"params": {}})
    model.configure_operation(resume_task_id="task-existing")
    monkeypatch.setattr(
        model,
        "_poll_video_task",
        lambda base, task_id, model_name: (
            "https://example.test/resumed.mp4"
            if task_id == "task-existing"
            else pytest.fail("wrong task ID")
        ),
    )

    result = model._generate_happyhorse_i2v_http(
        prompt="test prompt",
        first_frame_url="https://example.test/frame.png",
        resolution="1080P",
        duration=3,
        watermark=False,
        seed=None,
    )

    assert result == "https://example.test/resumed.mp4"


def test_native_voice_exception_relaxes_only_voice_identity_error() -> None:
    report = AssetReadinessReport(
        stage="render",
        generation_plan_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        selected_segment_ids=["seg-1"],
        required_asset_ids=["first-frame-1"],
        required_voice_character_ids=["char-1"],
        ready=False,
        errors=[
            AssetReadinessIssue(
                code="voice_profile_not_ready",
                severity="error",
                message="speaker has no approved voice identity",
                character_seed_id="char-1",
            )
        ],
    )

    relaxed = allow_provider_native_voice(report)

    assert relaxed.ready is True
    assert relaxed.errors == []
    assert relaxed.required_voice_character_ids == []
    assert [item.code for item in relaxed.warnings] == [
        "provider_native_voice_uncontrolled"
    ]


def test_native_voice_exception_never_relaxes_missing_first_frame() -> None:
    report = AssetReadinessReport(
        stage="render",
        generation_plan_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        selected_segment_ids=["seg-1"],
        required_asset_ids=["first-frame-1"],
        required_voice_character_ids=["char-1"],
        ready=False,
        errors=[
            AssetReadinessIssue(
                code="voice_profile_not_ready",
                severity="error",
                message="speaker has no approved voice identity",
                character_seed_id="char-1",
            ),
            AssetReadinessIssue(
                code="required_asset_not_ready",
                severity="error",
                message="asset is not approved",
                asset_id="first-frame-1",
            ),
        ],
    )

    relaxed = allow_provider_native_voice(report)

    assert relaxed.ready is False
    assert [item.code for item in relaxed.errors] == ["required_asset_not_ready"]


def test_prompt_includes_dialogue_audio_and_no_on_screen_text() -> None:
    prompt = build_happyhorse_prompt(
        PromptBundle(
            narrative_summary="少年が葡萄を一つ取るか迷う",
            visual_prompt="明治期の教室、少年のクローズアップ",
            motion_prompt="少年が視線を落として小さく息を吐く",
            camera_prompt="固定カメラから緩やかな寄り",
            timed_shot_prompt="0-1秒迷う、1-4秒話す、4-5秒視線を落とす",
            dialogue_prompt="『……僕も、ひとつだけなら』",
            audio_prompt="自然な少年の日本語、静かな室内環境音、BGMなし",
            negative_constraints=["identity drift", "costume change"],
        )
    )

    assert "……僕も、ひとつだけなら" in prompt
    assert "自然な少年の日本語" in prompt
    assert "Do not add subtitles" in prompt
    assert "identity drift" in prompt
