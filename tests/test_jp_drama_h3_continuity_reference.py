from pathlib import Path

import pytest

from src.apps.jp_drama.workflows.build_h3_continuity_reference import (
    ContinuityError,
    _build_entries,
)


def _segment(
    segment_id: str,
    order: int,
    group: str,
    *,
    first_asset: str | None = None,
) -> dict[str, object]:
    references: list[str] = []
    if first_asset is not None:
        references.append(first_asset)
    return {
        "segment_id": segment_id,
        "order": order,
        "continuity_group_id": group,
        "reference_asset_ids": references,
    }


def test_reused_first_video_seeds_only_the_immediate_same_group_segment(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "E01-G01.last-stable-frame.png"
    segments = [
        _segment("E01-G01", 1, "classroom"),
        _segment("E01-G02", 2, "classroom", first_asset="ref_first_E01-G02"),
        _segment("E01-G03", 3, "classroom", first_asset="ref_first_E01-G03"),
        _segment("E01-G04", 4, "hallway", first_asset="ref_first_E01-G04"),
        _segment("E01-G05", 5, "hallway", first_asset="ref_first_E01-G05"),
    ]

    entries = _build_entries(
        segments,
        extracted_frame_path=frame,
        extracted_frame_sha256="sha256:" + "a" * 64,
        extracted_at_seconds=9.622667,
    )

    assert entries[0] == {
        "segment_id": "E01-G02",
        "mode": "extracted_previous_segment_end_frame",
        "status": "ready",
        "source_segment_id": "E01-G01",
        "reference_role": "first_frame",
        "reference_path": str(frame.resolve()),
        "reference_sha256": "sha256:" + "a" * 64,
        "extracted_at_seconds": 9.622667,
        "send_full_previous_video": False,
    }
    assert entries[1]["segment_id"] == "E01-G03"
    assert entries[1]["mode"] == "previous_segment_end_frame"
    assert entries[1]["source_segment_id"] == "E01-G02"
    assert entries[1]["status"] == "pending_previous_segment_generation"

    assert entries[2] == {
        "segment_id": "E01-G04",
        "mode": "planned_first_frame_asset",
        "status": "ready_from_asset_bundle",
        "source_asset_id": "ref_first_E01-G04",
        "reference_role": "first_frame",
        "send_full_previous_video": False,
    }
    assert entries[3]["segment_id"] == "E01-G05"
    assert entries[3]["mode"] == "previous_segment_end_frame"
    assert entries[3]["source_segment_id"] == "E01-G04"


def test_e01_g02_must_continue_from_reused_video_in_same_group(tmp_path: Path) -> None:
    segments = [
        _segment("E01-G01", 1, "classroom"),
        _segment("E01-G02", 2, "another-place", first_asset="ref_first_E01-G02"),
    ]

    with pytest.raises(ContinuityError, match="same continuity group"):
        _build_entries(
            segments,
            extracted_frame_path=tmp_path / "frame.png",
            extracted_frame_sha256="sha256:" + "b" * 64,
            extracted_at_seconds=9.5,
        )
