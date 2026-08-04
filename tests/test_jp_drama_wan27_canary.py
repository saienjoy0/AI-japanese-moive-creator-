from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.preparation import compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering import (
    LiveProviderConfig,
    LiveTaskExecutor,
    ProviderCallLimitError,
    select_canary_shot,
)
from src.apps.jp_drama.rendering.ffmpeg import ffmpeg
from src.apps.jp_drama.rendering.mock_tasks import TaskContext
from src.apps.jp_drama.rendering.wan27_adapters import Wan27ImageModel, Wan27VideoModel


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
PROVIDER_PATH = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def _prepared():
    payload = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    package = EpisodePackage.model_validate(payload)
    return compile_episode(
        package,
        catalog=load_model_catalog(CATALOG_PATH),
        strict=True,
        source_payload=payload,
    )


def test_provider_config_uses_official_wan27_dimensions_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = LiveProviderConfig.load(PROVIDER_PATH)
    assert config.schema_version == "1.1.0"
    assert config.dashscope.image_size == "960*1696"
    assert config.dashscope.video_resolution == "720P"
    assert config.dashscope.region == "singapore"

    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "ws_example")
    assert (
        config.dashscope.endpoint_base_url()
        == "https://ws_example.ap-southeast-1.maas.aliyuncs.com"
    )


def test_provider_config_rejects_undersized_wan27_image() -> None:
    payload = json.loads(PROVIDER_PATH.read_text(encoding="utf-8"))
    payload["dashscope"]["image_size"] = "576*1024"
    with pytest.raises(ValueError, match="at least 768"):
        LiveProviderConfig.model_validate(payload)


def test_wan27_image_payload_omits_unsupported_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apps.jp_drama.rendering.wan27_adapters as module

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.test")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    captured: dict[str, object] = {}

    def post(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        return FakeResponse({"output": {"task_id": "image-task"}})

    def get(url: str, **kwargs: object) -> FakeResponse:
        return FakeResponse(
            {
                "output": {
                    "task_status": "SUCCEEDED",
                    "choices": [
                        {"message": {"content": [{"image": "https://example.test/image.png"}]}}
                    ],
                }
            }
        )

    monkeypatch.setattr(module.requests, "post", post)
    monkeypatch.setattr(module.requests, "get", get)
    model = Wan27ImageModel({"params": {"thinking_mode": True}})
    result = model._generate_dashscope_image_http(
        prompt="Japanese vertical drama",
        model_name="wan2.7-image-pro",
        size="960*1696",
        negative_prompt="must not be sent",
        prompt_extend=True,
        seed=7,
    )

    payload = captured["payload"]
    parameters = payload["parameters"]
    assert result.endswith("image.png")
    assert "negative_prompt" not in parameters
    assert "prompt_extend" not in parameters
    assert parameters["thinking_mode"] is True
    assert parameters["size"] == "960*1696"


def test_wan27_i2v_payload_uses_media_array_and_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apps.jp_drama.rendering.wan27_adapters as module

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.test")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    captured: dict[str, object] = {}

    def post(url: str, **kwargs: object) -> FakeResponse:
        captured["payload"] = kwargs["json"]
        return FakeResponse({"output": {"task_id": "video-task"}})

    def get(url: str, **kwargs: object) -> FakeResponse:
        return FakeResponse(
            {"output": {"task_status": "SUCCEEDED", "video_url": "https://example.test/v.mp4"}}
        )

    monkeypatch.setattr(module.requests, "post", post)
    monkeypatch.setattr(module.requests, "get", get)
    model = Wan27VideoModel({"params": {"resolution": "720P"}})
    result = model._generate_wan_i2v_http(
        prompt="A woman turns toward the camera",
        img_url="oss://dashscope-instant/keyframe.png",
        model_name="wan2.7-i2v",
        resolution=None,
        ratio="9:16",
        duration=5,
        shot_type="single",
    )

    payload = captured["payload"]
    assert result.endswith("v.mp4")
    assert payload["input"]["media"] == [
        {"type": "first_frame", "url": "oss://dashscope-instant/keyframe.png"}
    ]
    assert "img_url" not in payload["input"]
    assert payload["parameters"]["resolution"] == "720P"
    assert "ratio" not in payload["parameters"]
    assert "shot_type" not in payload["parameters"]


def test_canary_selection_isolates_one_shot_and_cost() -> None:
    prepared = _prepared()
    selected = select_canary_shot(prepared, "shot_01")
    assert selected.source_digest != prepared.source_digest
    assert selected.project_draft.project_id.endswith("canary_shot_01")
    assert selected.project_draft.target_duration_seconds == 15
    assert [frame.source_shot_id for frame in selected.storyboard_frame_drafts] == ["shot_01"]
    assert {node.shot_id for node in selected.render_graph.nodes} == {"shot_01"}
    assert selected.readiness_report.shot_count == 1
    assert len(selected.budget_snapshot.shot_items) == 1


def test_approved_keyframe_reduces_canary_calls_to_video_and_tts() -> None:
    selected = select_canary_shot(_prepared(), "shot_01")
    assert LiveTaskExecutor.estimate_api_calls(selected) == 3
    assert (
        LiveTaskExecutor.estimate_api_calls(
            selected,
            approved_keyframe_shots={"shot_01"},
        )
        == 2
    )


def test_hard_api_call_limit_blocks_before_submission(tmp_path: Path) -> None:
    config = LiveProviderConfig.load(PROVIDER_PATH)
    executor = LiveTaskExecutor(
        config,
        image_model=SimpleNamespace(),
        video_model=SimpleNamespace(),
        tts_processor=SimpleNamespace(),
        require_credentials=False,
        api_call_limit=1,
    )
    calls: list[str] = []

    def operation(value: str) -> str:
        calls.append(value)
        return value

    assert executor._provider_call(operation, "first") == "first"
    with pytest.raises(ProviderCallLimitError, match="limit reached"):
        executor._provider_call(operation, "second")
    assert calls == ["first"]
    assert executor.external_api_calls == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_approved_keyframe_is_reused_without_an_image_submission(tmp_path: Path) -> None:
    selected = select_canary_shot(_prepared(), "shot_01")
    selected.storyboard_frame_drafts[0].duration_seconds = 0.5
    selected.project_draft.target_duration_seconds = 0.5
    approved = tmp_path / "approved.png"
    approved.write_bytes(b"human-approved-keyframe")

    class FailImage:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, *args: object, **kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("approved keyframe must not be regenerated")

    class FakeVideo:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, prompt: str, output_path: str, **kwargs: object) -> tuple[str, float]:
            self.calls += 1
            ffmpeg(
                "-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=0.25",
                "-t", "0.25", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", output_path,
            )
            return output_path, 0.0

    image = FailImage()
    video = FakeVideo()
    executor = LiveTaskExecutor(
        LiveProviderConfig.load(PROVIDER_PATH),
        image_model=image,
        video_model=video,
        tts_processor=SimpleNamespace(),
        require_credentials=False,
        api_call_limit=1,
        approved_keyframes={"shot_01": approved},
    )
    node = next(
        item for item in selected.render_graph.nodes if item.task_type == "generate_video"
    )
    outputs = executor._run_generate_video(
        TaskContext(
            prepared=selected,
            frame=selected.storyboard_frame_drafts[0],
            node=node,
            work_dir=tmp_path / "work",
            dependency_outputs=[],
        )
    )

    assert image.calls == 0
    assert video.calls == 1
    assert executor.external_api_calls == 1
    assert outputs[0].read_bytes() == approved.read_bytes()
