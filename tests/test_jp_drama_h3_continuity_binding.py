import json
from pathlib import Path

import pytest

from src.apps.jp_drama.workflows import bind_h3_continuity_reference as subject
from src.apps.jp_drama.workflows.render_happyhorse_segment_continuity_canary import (
    ContinuityFrame,
)


SHA_VIDEO = "sha256:" + "a" * 64
SHA_FRAME = "sha256:" + "b" * 64
SHA_METADATA = "sha256:" + "c" * 64


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    handoff = tmp_path / "minimax_h3_cn_continuation_handoff.json"
    segment = tmp_path / "E01-G01.segment_artifact.json"
    frame = tmp_path / "E01-G01_end.png"
    metadata = tmp_path / "E01-G01_end.json"
    _write_json(
        handoff,
        {
            "reused_segment_id": "E01-G01",
            "remaining_segment_ids": ["E01-G02", "E01-G03"],
            "target_provider_route_id": "minimax/h3-reference-av",
            "external_api_calls": 0,
        },
    )
    _write_json(
        segment,
        {
            "segment_id": "E01-G01",
            "output_sha256": SHA_VIDEO,
        },
    )
    frame.write_bytes(b"png")
    metadata.write_text("{}", encoding="utf-8")
    return handoff, segment, frame, metadata


def _continuity(
    frame: Path,
    metadata: Path,
    *,
    video_sha: str = SHA_VIDEO,
) -> ContinuityFrame:
    return ContinuityFrame(
        path=frame.resolve(),
        metadata_path=metadata.resolve(),
        source_segment_id="E01-G01",
        frame_sha256=SHA_FRAME,
        metadata_sha256=SHA_METADATA,
        source_video_sha256=video_sha,
        width=720,
        height=1280,
        offset_from_end_seconds=0.13,
    )


def test_binding_reuses_pr45_artifact_without_provider_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    handoff, segment, frame, metadata = _inputs(tmp_path)
    monkeypatch.setattr(
        subject,
        "load_continuity_frame",
        lambda *args, **kwargs: _continuity(frame, metadata),
    )

    result = subject.bind(
        handoff_path=handoff,
        segment_artifact_path=segment,
        frame_path=frame,
        metadata_path=metadata,
        output_dir=tmp_path / "output",
    )

    binding_path = Path(result["binding"])
    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    assert payload["source_workflow_run_id"] == "31091499445"
    assert payload["source_artifact"] == "jp-drama-E01-G01-continuity-frame"
    assert payload["source_segment_id"] == "E01-G01"
    assert payload["target_segment_id"] == "E01-G02"
    assert payload["target_provider_route_id"] == "minimax/h3-reference-av"
    assert payload["reference_role"] == "storyboard"
    assert payload["send_full_previous_video"] is False
    assert payload["requires_publication_before_h3_submit"] is True
    assert payload["external_api_calls"] == 0
    assert not Path(payload["frame_path"]).is_absolute()
    assert not Path(payload["metadata_path"]).is_absolute()
    assert (binding_path.parent / payload["frame_path"]).read_bytes() == b"png"
    assert (binding_path.parent / payload["metadata_path"]).is_file()
    assert payload["content_digest"].startswith("sha256:")


def test_binding_rejects_frame_from_a_different_video(
    tmp_path: Path,
    monkeypatch,
) -> None:
    handoff, segment, frame, metadata = _inputs(tmp_path)
    monkeypatch.setattr(
        subject,
        "load_continuity_frame",
        lambda *args, **kwargs: _continuity(
            frame,
            metadata,
            video_sha="sha256:" + "d" * 64,
        ),
    )

    with pytest.raises(
        subject.H3ContinuityBindingError,
        match="different E01-G01 video",
    ):
        subject.bind(
            handoff_path=handoff,
            segment_artifact_path=segment,
            frame_path=frame,
            metadata_path=metadata,
            output_dir=tmp_path / "output",
        )


def test_binding_rejects_non_reference_h3_route(
    tmp_path: Path,
    monkeypatch,
) -> None:
    handoff, segment, frame, metadata = _inputs(tmp_path)
    payload = json.loads(handoff.read_text(encoding="utf-8"))
    payload["target_provider_route_id"] = "minimax/h3-first-frame"
    _write_json(handoff, payload)
    monkeypatch.setattr(
        subject,
        "load_continuity_frame",
        lambda *args, **kwargs: _continuity(frame, metadata),
    )

    with pytest.raises(subject.H3ContinuityBindingError, match="reference mode"):
        subject.bind(
            handoff_path=handoff,
            segment_artifact_path=segment,
            frame_path=frame,
            metadata_path=metadata,
            output_dir=tmp_path / "output",
        )
