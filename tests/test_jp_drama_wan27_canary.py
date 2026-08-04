from __future__ import annotations

import json
import shutil
import struct
import zlib
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.preparation import compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering import (
    CanaryProviderLedgerStore,
    LiveProviderConfig,
    LiveTaskExecutor,
    ProviderCallLimitError,
    ProviderLedgerError,
    create_approval_manifest,
    load_and_verify_approval,
    select_canary_shot,
)
from src.apps.jp_drama.rendering.approval import ApprovalError
from src.apps.jp_drama.rendering.ffmpeg import ffmpeg
from src.apps.jp_drama.rendering.mock_tasks import TaskContext
from src.apps.jp_drama.rendering.wan27_adapters import Wan27ImageModel, Wan27VideoModel


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
PROVIDER_PATH = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"


class FakeResponse:
    def __init__(
        self,
        payload: dict,
        status_code: int = 200,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)
        self.headers = headers or {}

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


def _png_chunk(name: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + name
        + payload
        + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
    )


def _write_png(path: Path, width: int = 960, height: int = 1696) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + (b"\x00\x00\x00" * width)
    compressed = zlib.compress(row * height, level=9)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )
    return path


def test_provider_config_uses_official_wan27_dimensions_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = LiveProviderConfig.load(PROVIDER_PATH)
    assert config.schema_version == "1.2.0"
    assert config.dashscope.image_size == "960*1696"
    assert config.dashscope.video_resolution == "720P"
    assert config.dashscope.region == "singapore"
    assert config.dashscope.tts_instructions_enabled is False

    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_UPLOAD_BASE_URL", raising=False)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "ws_example")
    assert (
        config.dashscope.endpoint_base_url()
        == "https://ws_example.ap-southeast-1.maas.aliyuncs.com"
    )
    assert (
        config.dashscope.upload_endpoint_base_url()
        == "https://dashscope-intl.aliyuncs.com"
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


def test_wan27_i2v_payload_uses_media_array_resolution_and_oss_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apps.jp_drama.rendering.wan27_adapters as module

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.test")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    captured: dict[str, object] = {}

    def post(url: str, **kwargs: object) -> FakeResponse:
        captured["payload"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
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
    assert captured["headers"]["X-DashScope-OssResourceResolve"] == "enable"


def test_wan27_local_png_upload_uses_exact_target_model_and_shared_upload_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apps.jp_drama.rendering.wan27_adapters as module
    import src.models.wanx as parent_module

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://workspace.example")
    monkeypatch.setenv("DASHSCOPE_UPLOAD_BASE_URL", "https://dashscope-intl.example")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    class FakeUploader:
        is_configured = False

        def __init__(self) -> None:
            pass

    monkeypatch.setattr(parent_module, "OSSImageUploader", FakeUploader)
    captured: dict[str, object] = {}

    def get(url: str, **kwargs: object) -> FakeResponse:
        if url.endswith("/api/v1/uploads"):
            captured["policy_url"] = url
            captured["policy_params"] = kwargs["params"]
            return FakeResponse(
                {
                    "output": {
                        "upload_host": "https://upload.example",
                        "upload_dir": "dashscope-temp/session",
                        "policy": "policy",
                        "signature": "signature",
                        "oss_access_key_id": "access",
                    }
                }
            )
        if "/api/v1/tasks/" in url:
            return FakeResponse(
                {
                    "output": {
                        "task_status": "SUCCEEDED",
                        "video_url": "https://cdn.example/video.mp4",
                    }
                }
            )
        raise AssertionError(f"unexpected GET {url}")

    def post(url: str, **kwargs: object) -> FakeResponse:
        if url == "https://upload.example":
            captured["upload_data"] = kwargs["data"]
            captured["upload_name"] = kwargs["files"]["file"][0]
            return FakeResponse({}, status_code=204)
        if "video-synthesis" in url:
            captured["create_payload"] = kwargs["json"]
            captured["create_headers"] = kwargs["headers"]
            return FakeResponse({"output": {"task_id": "video-task"}})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(module.requests, "get", get)
    monkeypatch.setattr(module.requests, "post", post)
    image = _write_png(tmp_path / "keyframe.png")
    output = tmp_path / "out.mp4"
    model = Wan27VideoModel({"params": {"resolution": "720P"}})
    monkeypatch.setattr(
        model,
        "_download_video",
        lambda _url, output_path: Path(output_path).write_bytes(b"video"),
    )

    result, _ = model.generate(
        prompt="demo",
        output_path=str(output),
        img_path=str(image),
        model_name="wan2.7-i2v",
        duration=5,
    )

    assert result == str(output)
    assert captured["policy_url"] == "https://dashscope-intl.example/api/v1/uploads"
    assert captured["policy_params"] == {
        "action": "getPolicy",
        "model": "wan2.7-i2v",
    }
    assert captured["create_payload"]["model"] == "wan2.7-i2v"
    assert captured["create_payload"]["input"]["media"][0]["url"].startswith("oss://")
    assert captured["create_headers"]["X-DashScope-OssResourceResolve"] == "enable"


def test_wan27_async_task_can_resume_without_new_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apps.jp_drama.rendering.wan27_adapters as module

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.test")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    posts: list[str] = []

    monkeypatch.setattr(
        module.requests,
        "post",
        lambda url, **kwargs: posts.append(url) or FakeResponse({}, status_code=500),
    )
    monkeypatch.setattr(
        module.requests,
        "get",
        lambda url, **kwargs: FakeResponse(
            {"output": {"task_status": "SUCCEEDED", "video_url": "https://example.test/v.mp4"}}
        ),
    )

    model = Wan27VideoModel({"params": {"resolution": "720P"}})
    model.configure_operation(resume_task_id="existing-task")
    result = model._generate_wan_i2v_http(
        prompt="resume",
        img_url="oss://existing/image.png",
        model_name="wan2.7-i2v",
        duration=5,
    )
    assert result.endswith("v.mp4")
    assert posts == []


def test_canary_selection_isolates_one_shot_and_native_duration() -> None:
    prepared = _prepared()
    selected = select_canary_shot(
        prepared,
        "shot_01",
        target_duration_seconds=5,
    )
    assert selected.source_digest != prepared.source_digest
    assert selected.project_draft.project_id.endswith("canary_shot_01")
    assert selected.project_draft.target_duration_seconds == 5
    assert selected.storyboard_frame_drafts[0].duration_seconds == 5
    assert all(cue.end_seconds <= 5 for cue in selected.storyboard_frame_drafts[0].dialogue_cues)
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


def test_persistent_ledger_blocks_second_process_and_cost_overrun(tmp_path: Path) -> None:
    store = CanaryProviderLedgerStore(tmp_path / "ledger.json")
    ledger = store.load_or_create(
        source_digest="sha256:" + ("a" * 64),
        shot_id="shot_01",
        max_api_calls=1,
        max_cost_cny=Decimal("1.0"),
    )
    store.begin(
        ledger,
        operation_id="op-1",
        stage="keyframe",
        operation_type="image",
        provider="dashscope",
        model="wan2.7-image",
        estimated_cost_cny=Decimal("0.5"),
    )

    reloaded = store.load_or_create(
        source_digest="sha256:" + ("a" * 64),
        shot_id="shot_01",
        max_api_calls=1,
        max_cost_cny=Decimal("1.0"),
    )
    assert reloaded.committed_api_calls == 1
    assert reloaded.committed_cost_cny == Decimal("0.5")
    with pytest.raises(ProviderLedgerError, match="call ceiling"):
        store.begin(
            reloaded,
            operation_id="op-2",
            stage="render",
            operation_type="video",
            provider="dashscope",
            model="wan2.7-i2v",
            estimated_cost_cny=Decimal("0.4"),
        )


def test_approval_manifest_detects_keyframe_tampering(tmp_path: Path) -> None:
    image = _write_png(tmp_path / "approved.png")
    manifest_path = tmp_path / "approval.json"
    manifest = create_approval_manifest(
        shot_id="shot_01",
        asset_path=image,
        generated_by="dashscope/wan2.7-image-pro",
        operation_id="keyframe-op",
        output_path=manifest_path,
    )
    loaded, verified = load_and_verify_approval(
        manifest_path,
        expected_shot_id="shot_01",
        expected_generated_by="dashscope/wan2.7-image-pro",
    )
    assert loaded.asset_sha256 == manifest.asset_sha256
    assert verified == image.resolve()

    image.write_bytes(image.read_bytes() + b"tampered")
    with pytest.raises(ApprovalError, match="hash changed"):
        load_and_verify_approval(
            manifest_path,
            expected_shot_id="shot_01",
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


def test_ledger_persists_task_id_and_refuses_duplicate_success(tmp_path: Path) -> None:
    config = LiveProviderConfig.load(PROVIDER_PATH)
    selected = select_canary_shot(_prepared(), "shot_01", target_duration_seconds=5)
    store = CanaryProviderLedgerStore(tmp_path / "ledger.json")
    ledger = store.load_or_create(
        source_digest=selected.source_digest,
        shot_id="shot_01",
        max_api_calls=1,
        max_cost_cny=Decimal("1.0"),
    )

    class FakeAsyncAdapter:
        def __init__(self) -> None:
            self.callback = None
            self.resume_task_id = None
            self.calls = 0

        def configure_operation(self, *, resume_task_id=None, on_task_submitted=None) -> None:
            self.resume_task_id = resume_task_id
            self.callback = on_task_submitted

        def clear_operation(self) -> None:
            self.callback = None

        def generate(self, prompt: str, output_path: str) -> str:
            self.calls += 1
            assert self.callback is not None
            self.callback("task-123", "request-456")
            Path(output_path).write_bytes(b"artifact")
            return output_path

    adapter = FakeAsyncAdapter()
    executor = LiveTaskExecutor(
        config,
        image_model=adapter,
        video_model=SimpleNamespace(),
        tts_processor=SimpleNamespace(),
        require_credentials=False,
        api_call_limit=1,
        ledger_store=store,
        ledger=ledger,
    )
    output = tmp_path / "image.png"
    executor._provider_call(
        adapter.generate,
        "prompt",
        str(output),
        _operation_id="image-op",
        _stage="keyframe",
        _operation_type="image",
        _model="wan2.7-image-pro",
        _estimated_cost_cny=Decimal("0.5"),
    )

    reloaded = store.load_or_create(
        source_digest=selected.source_digest,
        shot_id="shot_01",
        max_api_calls=1,
        max_cost_cny=Decimal("1.0"),
    )
    assert reloaded.operations["image-op"].provider_task_id == "task-123"
    assert reloaded.operations["image-op"].status == "succeeded"

    executor2 = LiveTaskExecutor(
        config,
        image_model=adapter,
        video_model=SimpleNamespace(),
        tts_processor=SimpleNamespace(),
        require_credentials=False,
        api_call_limit=1,
        ledger_store=store,
        ledger=reloaded,
    )
    with pytest.raises(Exception, match="already succeeded"):
        executor2._provider_call(
            adapter.generate,
            "prompt",
            str(output),
            _operation_id="image-op",
            _stage="keyframe",
            _operation_type="image",
            _model="wan2.7-image-pro",
            _estimated_cost_cny=Decimal("0.5"),
        )
    assert adapter.calls == 1


def test_japanese_tts_instructions_are_disabled_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.apps.jp_drama.rendering.canary_tasks as module

    selected = select_canary_shot(_prepared(), "shot_01", target_duration_seconds=5)
    frame = selected.storyboard_frame_drafts[0]
    node = next(item for item in selected.render_graph.nodes if item.task_type == "generate_tts")
    captured: list[object] = []

    class FakeTTS:
        def synthesize(self, text: str, output_path: str, **kwargs: object):
            captured.append(kwargs.get("instructions"))
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"mp3")
            return output_path, 0.0, "fake"

    def fake_ffmpeg(*args: object) -> None:
        destination = Path(str(args[-1]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"wav")

    monkeypatch.setattr(module, "ffmpeg", fake_ffmpeg)
    executor = LiveTaskExecutor(
        LiveProviderConfig.load(PROVIDER_PATH),
        image_model=SimpleNamespace(),
        video_model=SimpleNamespace(),
        tts_processor=FakeTTS(),
        require_credentials=False,
        api_call_limit=3,
    )
    executor._generate_tts_track(
        TaskContext(
            prepared=selected,
            frame=frame,
            node=node,
            work_dir=tmp_path / "work",
            dependency_outputs=[],
        ),
        tmp_path / "voice.wav",
        tmp_path / "raw",
    )
    assert captured
    assert set(captured) == {None}


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
