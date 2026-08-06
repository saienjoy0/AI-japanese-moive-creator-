from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.apps.jp_drama.rendering.ffmpeg import file_sha256
from src.apps.jp_drama.workflows import render_happyhorse_segment_continuity_canary as continuity
from src.apps.jp_drama.workflows.extract_segment_end_frame import (
    SCHEMA_VERSION,
    extract_segment_end_frame,
)
from src.apps.jp_drama.workflows.render_happyhorse_segment_continuity_canary import (
    ContinuityFrameError,
    append_continuity_prompt,
    load_continuity_frame,
    resolve_previous_continuity,
)


def _make_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:s=180x320:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def test_extracts_sha_bound_end_frame_for_next_segment(tmp_path: Path) -> None:
    video = tmp_path / "E01-G01-r2v.mp4"
    _make_video(video)

    report = extract_segment_end_frame(
        segment_id="E01-G01",
        video=video,
        output_dir=tmp_path / "continuity",
    )

    frame = Path(report["frame_path"])
    metadata = Path(report["metadata_path"])
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert frame.is_file() and frame.stat().st_size > 0
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["source_segment_id"] == "E01-G01"
    assert payload["derived_for_next_segment_id"] == "E01-G02"
    assert payload["source_video_sha256"] == file_sha256(video)
    assert payload["frame_sha256"] == file_sha256(frame)
    assert payload["frame_file"] == "E01-G01_end.png"
    assert payload["width"] == 180
    assert payload["height"] == 320
    assert payload["requested_offset_from_end_seconds"] == 0.1
    assert payload["offset_from_end_seconds"] >= 0.1


def test_retries_slightly_earlier_when_tail_boundary_has_no_frame(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video = tmp_path / "E01-G01-r2v.mp4"
    _make_video(video)
    reference = tmp_path / "reference.png"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "0.5",
            "-i",
            str(video),
            "-frames:v",
            "1",
            str(reference),
        ],
        check=True,
    )

    calls: list[list[str]] = []

    def fake_run(command, *, check, capture_output, text):
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        shutil.copyfile(reference, Path(command[-1]))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(
        "src.apps.jp_drama.workflows.extract_segment_end_frame.subprocess.run",
        fake_run,
    )

    report = extract_segment_end_frame(
        segment_id="E01-G01",
        video=video,
        output_dir=tmp_path / "continuity",
        offset_seconds=0.10,
    )

    assert len(calls) == 2
    assert report["requested_offset_from_end_seconds"] == 0.10
    assert report["offset_from_end_seconds"] == 0.13


def test_resolves_immediately_previous_nested_continuity_pair(tmp_path: Path) -> None:
    video = tmp_path / "E01-G01-r2v.mp4"
    _make_video(video)
    root = tmp_path / "continuity"
    report = extract_segment_end_frame(
        segment_id="E01-G01",
        video=video,
        output_dir=root / "E01-G01",
    )

    resolved = resolve_previous_continuity(root, target_segment_id="E01-G02")

    assert resolved == (
        Path(report["frame_path"]),
        Path(report["metadata_path"]),
    )
    assert resolve_previous_continuity(root, target_segment_id="E01-G01") is None


def test_rejects_frame_changed_after_extraction(tmp_path: Path) -> None:
    video = tmp_path / "E01-G01-r2v.mp4"
    _make_video(video)
    report = extract_segment_end_frame(
        segment_id="E01-G01",
        video=video,
        output_dir=tmp_path / "continuity",
    )
    frame = Path(report["frame_path"])
    frame.write_bytes(frame.read_bytes() + b"changed")

    with pytest.raises(ContinuityFrameError, match="SHA changed"):
        load_continuity_frame(
            frame,
            report["metadata_path"],
            target_segment_id="E01-G02",
        )


def test_rejects_non_immediate_source_segment(tmp_path: Path) -> None:
    video = tmp_path / "E01-G01-r2v.mp4"
    _make_video(video)
    report = extract_segment_end_frame(
        segment_id="E01-G01",
        video=video,
        output_dir=tmp_path / "continuity",
    )

    with pytest.raises(ContinuityFrameError, match="must come from E01-G02"):
        load_continuity_frame(
            report["frame_path"],
            report["metadata_path"],
            target_segment_id="E01-G03",
        )


def test_rejects_same_source_and_target_segment(tmp_path: Path) -> None:
    frame = tmp_path / "E01-G01_end.png"
    frame.write_bytes(b"not-used-before-sha-check")
    metadata = tmp_path / "E01-G01_end.json"
    metadata.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "source_segment_id": "E01-G01",
                "source_video_sha256": "sha256:" + "1" * 64,
                "frame_file": frame.name,
                "frame_sha256": file_sha256(frame),
                "width": 180,
                "height": 320,
                "offset_from_end_seconds": 0.1,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ContinuityFrameError, match="prior segment"):
        load_continuity_frame(
            frame,
            metadata,
            target_segment_id="E01-G01",
        )


def test_wrapper_auto_loads_prior_frame_without_manual_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prior_video = tmp_path / "E01-G01-r2v.mp4"
    _make_video(prior_video)
    root = tmp_path / "continuity"
    extract_segment_end_frame(
        segment_id="E01-G01",
        video=prior_video,
        output_dir=root / "E01-G01",
    )
    captured = {}

    def fake_run(arguments, frame):
        captured["arguments"] = arguments
        captured["frame"] = frame
        return continuity.base.EXIT_OK

    monkeypatch.setattr(continuity, "_run_base", fake_run)

    result = continuity.main(
        [
            "--segment-id",
            "E01-G02",
            "--output",
            str(tmp_path / "E01-G02-r2v.mp4"),
            "--input-mode",
            "references",
            "--stage",
            "preflight",
            "--continuity-dir",
            str(root),
        ]
    )

    assert result == continuity.base.EXIT_OK
    assert captured["frame"].source_segment_id == "E01-G01"


def test_wrapper_auto_extracts_rendered_segment_end_frame(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output = tmp_path / "E01-G02-r2v.mp4"
    _make_video(output)
    root = tmp_path / "continuity"

    monkeypatch.setattr(
        continuity,
        "_run_base",
        lambda arguments, frame: continuity.base.EXIT_OK,
    )

    result = continuity.main(
        [
            "--segment-id",
            "E01-G02",
            "--output",
            str(output),
            "--input-mode",
            "references",
            "--stage",
            "render",
            "--continuity-dir",
            str(root),
        ]
    )

    assert result == continuity.base.EXIT_OK
    metadata = root / "E01-G02" / "E01-G02_end.json"
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert (root / "E01-G02" / "E01-G02_end.png").is_file()
    assert payload["derived_for_next_segment_id"] == "E01-G03"


def test_prompt_adds_the_next_image_binding(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.apps.jp_drama.workflows.render_happyhorse_segment_continuity_canary.base.HappyHorse11R2VModel",
        SimpleNamespace(MAX_REFERENCE_IMAGES=9),
    )
    prompt = append_continuity_prompt("[Image 1] character", existing_reference_count=5)
    assert "[Image 6] is the derived final frame" in prompt


def test_prompt_rejects_tenth_reference(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.apps.jp_drama.workflows.render_happyhorse_segment_continuity_canary.base.HappyHorse11R2VModel",
        SimpleNamespace(MAX_REFERENCE_IMAGES=9),
    )
    with pytest.raises(RuntimeError, match="exceed 9 images"):
        append_continuity_prompt("test", existing_reference_count=9)
