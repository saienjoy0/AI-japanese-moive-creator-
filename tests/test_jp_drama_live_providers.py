from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.preparation import PreparedEpisode, compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.rendering import (
    LiveProviderConfig,
    LiveTaskExecutor,
    MockTaskExecutor,
    ProviderConfigurationError,
    RenderGraphRunner,
    RenderStateConflictError,
)
from src.apps.jp_drama.rendering.ffmpeg import ffmpeg
from src.apps.jp_drama.workflows.render_episode import main as render_main


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
PROVIDER_PATH = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"


@pytest.fixture()
def prepared() -> PreparedEpisode:
    payload = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
    package = EpisodePackage.model_validate(payload)
    return compile_episode(
        package,
        catalog=load_model_catalog(CATALOG_PATH),
        strict=True,
        source_payload=payload,
    )


def _short_episode(prepared: PreparedEpisode) -> PreparedEpisode:
    shortened = prepared.model_copy(deep=True)
    for frame in shortened.storyboard_frame_drafts:
        frame.duration_seconds = 0.8
        for cue in frame.dialogue_cues:
            cue.start_seconds = 0.10
            cue.end_seconds = 0.55
    shortened.project_draft.target_duration_seconds = 2.4
    return shortened


class FakeImageModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(self, prompt: str, output_path: str, **kwargs: object) -> tuple[str, float]:
        self.calls.append({"prompt": prompt, **kwargs})
        ffmpeg(
            "-f", "lavfi", "-i", "color=c=0x35506f:s=540x960:d=0.04",
            "-frames:v", "1", "-update", "1", "-y", output_path,
        )
        return output_path, 0.0


class FakeVideoModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(self, prompt: str, output_path: str, **kwargs: object) -> tuple[str, float]:
        self.calls.append({"prompt": prompt, **kwargs})
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=540x960:rate=30:duration=0.35",
            "-t", "0.35", "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", output_path,
        )
        return output_path, 0.0


class FakeTTSProcessor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def synthesize(self, text: str, output_path: str, **kwargs: object) -> tuple[str, float, str]:
        self.calls.append({"text": text, **kwargs})
        ffmpeg(
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.30",
            "-c:a", "libmp3lame", "-b:a", "96k", output_path,
        )
        return output_path, 0.0, f"fake-{len(self.calls)}"


def test_provider_config_requires_named_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = LiveProviderConfig.load(PROVIDER_PATH)
    monkeypatch.delenv(config.dashscope.api_key_env, raising=False)
    with pytest.raises(ProviderConfigurationError, match="DASHSCOPE_API_KEY"):
        config.require_environment()


def test_execution_profile_never_contains_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "test-secret-that-must-not-be-serialized"
    config = LiveProviderConfig.load(PROVIDER_PATH)
    monkeypatch.setenv(config.dashscope.api_key_env, secret)

    assert secret not in config.to_canonical_json()
    assert secret not in config.execution_profile
    assert config.provider_manifest["provider"] == "dashscope"
    assert config.provider_manifest["default_voice"] == "Ono Anna"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_live_executor_runs_full_graph_with_injected_provider_clients(
    prepared: PreparedEpisode,
    tmp_path: Path,
) -> None:
    shortened = _short_episode(prepared)
    config = LiveProviderConfig.load(PROVIDER_PATH)
    images = FakeImageModel()
    videos = FakeVideoModel()
    voices = FakeTTSProcessor()
    executor = LiveTaskExecutor(
        config,
        image_model=images,
        video_model=videos,
        tts_processor=voices,
        require_credentials=False,
    )
    runner = RenderGraphRunner(
        shortened,
        output_file=tmp_path / "episode.mp4",
        work_dir=tmp_path / "work",
        executor=executor,
        persistence_status="created",
    )

    report = runner.run(reset=True)
    state_before = runner.state_file.read_bytes()
    second = runner.run()

    assert report.valid is True
    assert report.execution_profile == executor.execution_profile
    assert report.provider_manifest == executor.provider_manifest
    assert report.external_api_calls == 9
    assert second.external_api_calls == 9
    assert runner.state_file.read_bytes() == state_before
    assert len(images.calls) == 3
    assert len(videos.calls) == 3
    assert len(voices.calls) == 3
    assert all(call["model_name"] == config.dashscope.image_model for call in images.calls)
    assert all(call["resolution"] == "720P" for call in videos.calls)
    assert {call["voice"] for call in voices.calls} == {"Ono Anna"}

    state = json.loads(runner.state_file.read_text(encoding="utf-8"))
    assert state["external_api_calls"] == 9
    assert sum(task["external_api_calls"] for task in state["task_states"].values()) == 9


def test_mock_state_cannot_be_reused_by_live_provider(
    prepared: PreparedEpisode,
    tmp_path: Path,
) -> None:
    output = tmp_path / "episode.mp4"
    work = tmp_path / "work"
    mock_runner = RenderGraphRunner(
        prepared,
        output_file=output,
        work_dir=work,
        executor=MockTaskExecutor(),
    )
    work.mkdir(parents=True)
    mock_runner._write_state(mock_runner._load_or_create_state())

    live_runner = RenderGraphRunner(
        prepared,
        output_file=output,
        work_dir=work,
        executor=LiveTaskExecutor(
            LiveProviderConfig.load(PROVIDER_PATH),
            image_model=object(),
            video_model=object(),
            tts_processor=object(),
            require_credentials=False,
        ),
    )
    with pytest.raises(RenderStateConflictError, match="provider profile"):
        live_runner._load_or_create_state()


def test_live_preflight_makes_no_provider_calls(
    prepared: PreparedEpisode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_path = tmp_path / "prepared.json"
    prepared_path.write_text(prepared.to_canonical_json(), encoding="utf-8")
    output = tmp_path / "episode.mp4"
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    exit_code = render_main(
        [
            "--input", str(prepared_path),
            "--output", str(output),
            "--providers", str(PROVIDER_PATH),
            "--preflight",
        ]
    )

    assert exit_code == 0
    assert output.exists() is False
    assert (tmp_path / ".episode_work").exists() is False


def test_full_live_render_is_blocked_without_explicit_call_budget(
    prepared: PreparedEpisode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_path = tmp_path / "prepared.json"
    prepared_path.write_text(prepared.to_canonical_json(), encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "not-used")
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "not-used")

    exit_code = render_main(
        [
            "--input", str(prepared_path),
            "--output", str(tmp_path / "episode.mp4"),
            "--providers", str(PROVIDER_PATH),
        ]
    )

    assert exit_code == 6
    assert (tmp_path / "episode.mp4").exists() is False
