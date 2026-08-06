from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.apps.jp_drama.production.happyhorse_multiclip import (
    HappyHorseMultiClipError,
    HappyHorseMultiClipPlan,
    build_clip_prompt,
    build_ffmpeg_concat_command,
    build_preflight_report,
    file_sha256,
    load_plan,
    materialize_first_frame,
    png_dimensions,
    verify_plan_files,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_PLAN = (
    REPO_ROOT
    / "assets/jp_drama/one_bunch_of_grapes/production_keyframes/E01/G01"
    / "E01-G01.happyhorse_multiclip_plan.json"
)


def test_committed_e01_g01_plan_is_four_contiguous_approved_clips() -> None:
    plan = load_plan(COMMITTED_PLAN)
    verified = verify_plan_files(plan, repository_root=REPO_ROOT)

    assert plan.source_segment_id == "E01-G01"
    assert [clip.clip_id for clip in plan.clips] == [
        "E01-G01-A",
        "E01-G01-B",
        "E01-G01-C",
        "E01-G01-D",
    ]
    assert [clip.final_duration_seconds for clip in plan.clips] == [
        2.0,
        2.0,
        2.5,
        3.5,
    ]
    assert [clip.provider_request_duration_seconds for clip in plan.clips] == [
        3,
        3,
        3,
        4,
    ]
    assert verified["clip_count"] == 4
    assert verified["external_api_calls"] == 0
    assert all(clip.approval_status == "approved" for clip in plan.clips)


def test_preflight_builds_exactly_four_paid_requests_and_is_zero_call() -> None:
    plan = load_plan(COMMITTED_PLAN)
    report = build_preflight_report(
        plan,
        repository_root=REPO_ROOT,
        seed_base=240700,
        resolution="720P",
        max_api_calls=4,
        max_cost_cny="20",
        cost_reserve_cny_per_clip="4",
        missing_environment=["DASHSCOPE_API_KEY", "DASHSCOPE_WORKSPACE_ID"],
    )

    assert report["valid"] is True
    assert report["clip_count"] == 4
    assert len(report["requests"]) == 4
    assert report["external_api_calls"] == 0
    assert report["total_cost_reserve_cny"] == "16"
    assert report["approval_digest"].startswith("sha256:")
    assert [item["provider_duration_seconds"] for item in report["requests"]] == [
        3,
        3,
        3,
        4,
    ]
    assert len({item["operation_id"] for item in report["requests"]}) == 4


def test_preflight_refuses_wrong_api_limit() -> None:
    plan = load_plan(COMMITTED_PLAN)
    report = build_preflight_report(
        plan,
        repository_root=REPO_ROOT,
        seed_base=240700,
        resolution="720P",
        max_api_calls=3,
        max_cost_cny="20",
        cost_reserve_cny_per_clip="4",
    )
    assert report["valid"] is False
    assert report["status"] == "blocked_by_limits"


def test_dialogue_is_only_on_final_clip_and_keeps_lips_closed() -> None:
    plan = load_plan(COMMITTED_PLAN)
    prompts = {clip.clip_id: build_clip_prompt(clip) for clip in plan.clips}

    assert "海は、こんな色じゃない" not in prompts["E01-G01-A"]
    assert "海は、こんな色じゃない" not in prompts["E01-G01-B"]
    assert "海は、こんな色じゃない" not in prompts["E01-G01-C"]
    assert "海は、こんな色じゃない" in prompts["E01-G01-D"]
    assert "No visible speaking" in prompts["E01-G01-D"]
    assert "Do not add subtitles" in prompts["E01-G01-D"]


def test_ffmpeg_command_trims_provider_minimums_to_exact_ten_seconds() -> None:
    plan = load_plan(COMMITTED_PLAN)
    command = build_ffmpeg_concat_command(
        plan,
        raw_outputs=["a.mp4", "b.mp4", "c.mp4", "d.mp4"],
        output_path="final.mp4",
    )
    joined = " ".join(command)

    assert "trim=duration=2.000" in joined
    assert "trim=duration=2.500" in joined
    assert "trim=duration=3.500" in joined
    assert "concat=n=4:v=1:a=1" in joined
    assert "-t 10.000" in joined
    assert command[-1] == "final.mp4"


def test_svg_wrapper_materializes_provider_ready_jpeg(tmp_path: Path) -> None:
    plan = load_plan(COMMITTED_PLAN)
    clip = plan.clips[0]
    materialized = materialize_first_frame(
        REPO_ROOT / clip.first_frame_path,
        tmp_path,
        expected_sha256=clip.first_frame_sha256,
    )

    assert materialized.suffix == ".jpg"
    assert file_sha256(materialized) == clip.first_frame_sha256
    assert png_dimensions(materialized) == (clip.width, clip.height)


def test_changed_keyframe_hash_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "assets", project / "assets")
    plan_path = (
        project
        / "assets/jp_drama/one_bunch_of_grapes/production_keyframes/E01/G01"
        / "E01-G01.happyhorse_multiclip_plan.json"
    )
    plan = load_plan(plan_path)
    first = project / plan.clips[0].first_frame_path
    first.write_bytes(first.read_bytes() + b"changed")

    with pytest.raises(HappyHorseMultiClipError, match="hash changed"):
        verify_plan_files(plan, repository_root=project)


def test_path_traversal_is_rejected() -> None:
    raw = json.loads(COMMITTED_PLAN.read_text(encoding="utf-8"))
    raw["clips"][0]["first_frame_path"] = "../../outside.png"
    raw["content_digest"] = "sha256:" + "0" * 64

    # Build a fresh digest using the public constructor, then verify the path gate.
    raw.pop("content_digest")
    plan = HappyHorseMultiClipPlan.build_with_digest(**raw)
    with pytest.raises(HappyHorseMultiClipError, match="escapes repository root"):
        verify_plan_files(plan, repository_root=REPO_ROOT)
