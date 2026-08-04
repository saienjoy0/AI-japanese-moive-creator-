from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.preparation import PreparedEpisode, compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.preparation.models import RenderTaskNode
from src.apps.jp_drama.rendering import (
    MockTaskExecutor,
    RenderGraphRunner,
    RenderStateConflictError,
    RenderTaskFailedError,
    TaskContext,
)
from src.apps.jp_drama.rendering.ffmpeg import file_sha256


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"


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


def test_topological_order_finishes_each_cut_before_the_next(
    prepared: PreparedEpisode,
    tmp_path: Path,
) -> None:
    runner = RenderGraphRunner(
        prepared,
        output_file=tmp_path / "episode.mp4",
        work_dir=tmp_path / "work",
    )
    order = runner.task_order

    shot_1_final = next(
        task_id
        for task_id in order
        if runner.nodes_by_id[task_id].shot_id == "shot_01"
        and runner.nodes_by_id[task_id].task_type == "finalize_shot"
    )
    shot_2_first = next(
        task_id
        for task_id in order
        if runner.nodes_by_id[task_id].shot_id == "shot_02"
    )
    assert order.index(shot_1_final) < order.index(shot_2_first)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_failed_cut_resumes_without_repeating_completed_cut(
    prepared: PreparedEpisode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shortened = _short_episode(prepared)
    runner = RenderGraphRunner(
        shortened,
        output_file=tmp_path / "episode.mp4",
        work_dir=tmp_path / "work",
        persistence_status="created",
    )
    failure_task = next(
        node.task_id
        for node in shortened.render_graph.nodes
        if node.shot_id == "shot_02" and node.task_type == "generate_native_av"
    )
    monkeypatch.setenv("JP_DRAMA_FAIL_TASK_ID", failure_task)
    with pytest.raises(RenderTaskFailedError):
        runner.run(reset=True)

    failed_state = json.loads(runner.state_file.read_text(encoding="utf-8"))
    shot_1_final = next(
        node.task_id
        for node in shortened.render_graph.nodes
        if node.shot_id == "shot_01" and node.task_type == "finalize_shot"
    )
    assert failed_state["task_states"][shot_1_final]["attempts"] == 1
    assert failed_state["task_states"][failure_task]["status"] == "failed"

    monkeypatch.delenv("JP_DRAMA_FAIL_TASK_ID")
    report = runner.run()
    resumed_state = json.loads(runner.state_file.read_text(encoding="utf-8"))

    assert report.valid is True
    assert resumed_state["task_states"][shot_1_final]["attempts"] == 1
    assert resumed_state["task_states"][failure_task]["attempts"] == 2
    assert resumed_state["external_api_calls"] == 0


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_same_input_reuses_state_and_keeps_output_identical(
    prepared: PreparedEpisode,
    tmp_path: Path,
) -> None:
    shortened = _short_episode(prepared)
    runner = RenderGraphRunner(
        shortened,
        output_file=tmp_path / "episode.mp4",
        work_dir=tmp_path / "work",
        persistence_status="created",
    )
    first = runner.run(reset=True)
    state_before = runner.state_file.read_bytes()
    hash_before = file_sha256(tmp_path / "episode.mp4")

    second = runner.run()

    assert first.graph_fingerprint == second.graph_fingerprint
    assert runner.state_file.read_bytes() == state_before
    assert file_sha256(tmp_path / "episode.mp4") == hash_before


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_generate_image_and_still_motion_mock_tasks(
    prepared: PreparedEpisode,
    tmp_path: Path,
) -> None:
    shortened = _short_episode(prepared)
    frame = shortened.storyboard_frame_drafts[0]
    executor = MockTaskExecutor()
    image_node = RenderTaskNode(
        task_id="test_generate_image",
        shot_id=frame.source_shot_id,
        task_type="generate_image",
        depends_on=[],
        external_api_required=True,
        provider_required=True,
    )
    image_outputs = executor.execute(
        TaskContext(
            prepared=shortened,
            frame=frame,
            node=image_node,
            work_dir=tmp_path,
            dependency_outputs=[],
        )
    )
    motion_node = RenderTaskNode(
        task_id="test_apply_still_motion",
        shot_id=frame.source_shot_id,
        task_type="apply_still_motion",
        depends_on=[image_node.task_id],
        external_api_required=False,
        provider_required=False,
    )
    motion_outputs = executor.execute(
        TaskContext(
            prepared=shortened,
            frame=frame,
            node=motion_node,
            work_dir=tmp_path,
            dependency_outputs=image_outputs,
        )
    )

    assert image_outputs[0].suffix == ".ppm"
    assert motion_outputs[0].suffix == ".mp4"
    assert all(path.stat().st_size > 0 for path in [*image_outputs, *motion_outputs])


def test_existing_state_rejects_different_source(
    prepared: PreparedEpisode,
    tmp_path: Path,
) -> None:
    first = RenderGraphRunner(
        prepared,
        output_file=tmp_path / "episode.mp4",
        work_dir=tmp_path / "work",
    )
    first.work_dir.mkdir(parents=True)
    first._write_state(first._load_or_create_state())

    changed = prepared.model_copy(deep=True)
    changed.source_digest = "sha256:" + "f" * 64
    second = RenderGraphRunner(
        changed,
        output_file=tmp_path / "episode.mp4",
        work_dir=tmp_path / "work",
    )
    with pytest.raises(RenderStateConflictError):
        second._load_or_create_state()
