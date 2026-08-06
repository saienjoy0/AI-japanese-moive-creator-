from __future__ import annotations

from typing import Any

import pytest

from src.apps.jp_drama.rendering.happyhorse11 import (
    HappyHorse11I2VModel,
    HappyHorse11R2VModel,
)


def test_r2v_payload_is_vertical_and_keeps_ordered_references() -> None:
    payload = HappyHorse11R2VModel.build_request_payload(
        prompt="[Image 1] boy; [Image 2] classroom",
        reference_image_urls=[
            "oss://bucket/boy.png",
            "oss://bucket/classroom.png",
        ],
        resolution="720P",
        ratio="9:16",
        duration=10,
        watermark=False,
        seed=7,
    )
    assert payload == {
        "model": "happyhorse-1.1-r2v",
        "input": {
            "prompt": "[Image 1] boy; [Image 2] classroom",
            "media": [
                {"type": "reference_image", "url": "oss://bucket/boy.png"},
                {"type": "reference_image", "url": "oss://bucket/classroom.png"},
            ],
        },
        "parameters": {
            "resolution": "720P",
            "ratio": "9:16",
            "duration": 10,
            "watermark": False,
            "seed": 7,
        },
    }


@pytest.mark.parametrize(
    "urls,error",
    [
        ([], "between 1 and 9"),
        (["https://x.test/a.png"] * 2, "must be unique"),
        ([f"https://x.test/{index}.png" for index in range(10)], "between 1 and 9"),
    ],
)
def test_r2v_payload_rejects_unsafe_reference_lists(urls, error) -> None:
    with pytest.raises(ValueError, match=error):
        HappyHorse11R2VModel.build_request_payload(
            prompt="test",
            reference_image_urls=urls,
            resolution="720P",
            ratio="9:16",
            duration=10,
        )


def test_r2v_task_id_is_persisted_before_poll(monkeypatch) -> None:
    posted: dict[str, Any] = {}
    submitted: list[tuple[str, str | None]] = []

    class FakeResponse:
        status_code = 200
        text = '{"output":{"task_id":"task-r2v"},"request_id":"request-r2v"}'
        headers: dict[str, str] = {}

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "output": {"task_id": "task-r2v"},
                "request_id": "request-r2v",
            }

    def fake_post(url, *, headers, json, timeout):
        posted.update({"url": url, "headers": headers, "json": json})
        return FakeResponse()

    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.get_provider_base_url",
        lambda _: "https://workspace.example.test",
    )
    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.requests.post",
        fake_post,
    )
    model = HappyHorse11R2VModel({"params": {}})
    model.configure_operation(
        on_task_submitted=lambda task_id, request_id: submitted.append(
            (task_id, request_id)
        )
    )
    monkeypatch.setattr(
        model,
        "_poll_video_task",
        lambda base, task_id, model_name: "https://example.test/result.mp4",
    )

    result = model._generate_happyhorse_r2v_http(
        prompt="[Image 1] boy",
        reference_image_urls=["oss://bucket/boy.png"],
        resolution="720P",
        ratio="9:16",
        duration=10,
        watermark=False,
        seed=1,
    )

    assert result == "https://example.test/result.mp4"
    assert submitted == [("task-r2v", "request-r2v")]
    assert posted["headers"]["X-DashScope-OssResourceResolve"] == "enable"
    assert posted["json"]["parameters"]["ratio"] == "9:16"


def test_r2v_resume_never_posts_again(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.get_provider_base_url",
        lambda _: "https://workspace.example.test",
    )

    def reject_post(*args, **kwargs):
        raise AssertionError("resume must not create another paid task")

    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.requests.post",
        reject_post,
    )
    model = HappyHorse11R2VModel({"params": {}})
    model.configure_operation(resume_task_id="task-existing")
    monkeypatch.setattr(
        model,
        "_poll_video_task",
        lambda base, task_id, model_name: "https://example.test/resumed.mp4",
    )

    result = model._generate_happyhorse_r2v_http(
        prompt="ignored during resume",
        reference_image_urls=[],
        resolution="720P",
        ratio="9:16",
        duration=10,
        watermark=False,
        seed=1,
    )
    assert result == "https://example.test/resumed.mp4"


def test_i2v_payload_regression_remains_first_frame_only() -> None:
    payload = HappyHorse11I2VModel.build_request_payload(
        prompt="test",
        first_frame_url="oss://bucket/frame.png",
        resolution="720P",
        duration=5,
    )
    assert payload["model"] == "happyhorse-1.1-i2v"
    assert payload["input"]["media"] == [
        {"type": "first_frame", "url": "oss://bucket/frame.png"}
    ]
    assert "ratio" not in payload["parameters"]
