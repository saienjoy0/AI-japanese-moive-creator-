from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.models.wanx import WanxModel


@pytest.mark.parametrize(
    "model_name",
    ["happyhorse-1.0-r2v", "happyhorse-1.1-r2v"],
)
def test_happyhorse_r2v_uses_existing_http_path_and_keeps_reference_order(
    model_name: str,
) -> None:
    model = WanxModel({"params": {}})
    captured: dict[str, object] = {}
    resolved = [
        SimpleNamespace(
            value="oss://refs/character.png",
            headers={"X-DashScope-OssResourceResolve": "enable"},
        ),
        SimpleNamespace(
            value="oss://refs/classroom.png",
            headers={"X-DashScope-OssResourceResolve": "enable"},
        ),
    ]

    def fake_generate_hh_http(**kwargs):
        captured.update(kwargs)
        return "https://example.test/video.mp4"

    with (
        patch("src.models.wanx.resolve_media_inputs", return_value=resolved) as resolver,
        patch.object(model, "_resolve_provider_backend_for_model", return_value="dashscope"),
        patch.object(
            model,
            "_build_dashscope_temp_url_resolver",
            return_value=lambda path: f"oss://temp/{path}",
        ),
        patch.object(model, "_generate_hh_http", side_effect=fake_generate_hh_http),
        patch.object(model, "_download_video") as download,
    ):
        output, _ = model.generate(
            prompt="Use [Image 1] for the boy and [Image 2] for the classroom.",
            output_path="output.mp4",
            model=model_name,
            ref_image_urls=["character.png", "classroom.png"],
            duration=10,
            resolution="720P",
            seed=17,
        )

    assert output == "output.mp4"
    assert resolver.call_args.kwargs["model_name"] == model_name
    assert resolver.call_args.kwargs["modality"] == "image"
    assert captured["model_name"] == model_name
    assert captured["ratio"] == "9:16"
    assert captured["duration"] == 10
    assert captured["media"] == [
        {"type": "reference_image", "url": "oss://refs/character.png"},
        {"type": "reference_image", "url": "oss://refs/classroom.png"},
    ]
    assert captured["extra_headers"] == {
        "X-DashScope-OssResourceResolve": "enable"
    }
    download.assert_called_once_with(
        "https://example.test/video.mp4",
        "output.mp4",
    )


@pytest.mark.parametrize(
    "references",
    [[], [f"ref-{index}.png" for index in range(10)]],
)
def test_happyhorse_11_r2v_rejects_reference_counts_outside_one_to_nine(
    references: list[str],
) -> None:
    model = WanxModel({"params": {}})

    with (
        patch.object(model, "_resolve_provider_backend_for_model", return_value="dashscope"),
        patch.object(
            model,
            "_build_dashscope_temp_url_resolver",
            return_value=lambda path: f"oss://temp/{path}",
        ),
        pytest.raises(ValueError, match="1 to 9 images"),
    ):
        model.generate(
            prompt="test",
            output_path="output.mp4",
            model="happyhorse-1.1-r2v",
            ref_image_urls=references,
        )


def test_happyhorse_11_i2v_still_uses_first_frame_media() -> None:
    model = WanxModel({"params": {}})
    captured: dict[str, object] = {}
    resolved = SimpleNamespace(
        value="oss://refs/first-frame.png",
        headers={"X-DashScope-OssResourceResolve": "enable"},
    )

    def fake_generate_hh_http(**kwargs):
        captured.update(kwargs)
        return "https://example.test/i2v.mp4"

    with (
        patch("src.models.wanx.resolve_media_input", return_value=resolved),
        patch.object(model, "_resolve_provider_backend_for_model", return_value="dashscope"),
        patch.object(
            model,
            "_build_dashscope_temp_url_resolver",
            return_value=lambda path: f"oss://temp/{path}",
        ),
        patch.object(model, "_generate_hh_http", side_effect=fake_generate_hh_http),
        patch.object(model, "_download_video"),
    ):
        model.generate(
            prompt="test",
            output_path="output.mp4",
            model="happyhorse-1.1-i2v",
            img_url="first-frame.png",
            ratio="9:16",
        )

    assert captured["media"] == [
        {"type": "first_frame", "url": "oss://refs/first-frame.png"}
    ]
    assert captured["model_name"] == "happyhorse-1.1-i2v"


def test_happyhorse_http_payload_keeps_r2v_ratio_but_omits_i2v_ratio() -> None:
    model = WanxModel({"params": {}})
    posted_payloads: list[dict] = []
    provider_ids: list[tuple[str, str | None, str | None]] = []

    class FakeResponse:
        status_code = 200
        text = "ok"

        def __init__(self, payload: dict):
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    def fake_post(url, *, headers, json, timeout):
        posted_payloads.append(json)
        return FakeResponse(
            {"output": {"task_id": "task-1"}, "request_id": "request-1"}
        )

    def fake_get(url, *, headers, timeout):
        return FakeResponse(
            {
                "output": {
                    "task_status": "SUCCEEDED",
                    "video_url": "https://example.test/video.mp4",
                }
            }
        )

    with (
        patch("src.models.wanx.get_provider_base_url", return_value="https://api.test"),
        patch("src.models.wanx.requests.post", side_effect=fake_post),
        patch("src.models.wanx.requests.get", side_effect=fake_get),
        patch("src.models.wanx.time.sleep"),
        patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-key"}),
    ):
        model._generate_hh_http(
            prompt="r2v",
            model_name="happyhorse-1.1-r2v",
            media=[{"type": "reference_image", "url": "oss://ref.png"}],
            resolution="720P",
            duration=10,
            ratio="9:16",
            seed=3,
            on_provider_ids=lambda provider, task_id, request_id: provider_ids.append(
                (provider, task_id, request_id)
            ),
        )
        model._generate_hh_http(
            prompt="i2v",
            model_name="happyhorse-1.1-i2v",
            media=[{"type": "first_frame", "url": "oss://frame.png"}],
            resolution="720P",
            duration=5,
            ratio="9:16",
        )

    assert posted_payloads[0]["parameters"]["ratio"] == "9:16"
    assert "ratio" not in posted_payloads[1]["parameters"]
    assert provider_ids == [("dashscope", "task-1", "request-1")]
