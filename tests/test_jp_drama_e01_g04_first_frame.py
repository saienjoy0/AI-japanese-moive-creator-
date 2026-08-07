from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.apps.jp_drama.workflows import render_happyhorse_e01_g04_first_frame_canary as g4
from src.apps.jp_drama.workflows import render_happyhorse_segment_auto_route_canary as auto
from src.apps.jp_drama.workflows import render_happyhorse_segment_canary as base
from src.apps.jp_drama.workflows import (
    render_happyhorse_segment_directed_continuity_canary as directed,
)


def _plan():
    return SimpleNamespace(
        segments=[
            SimpleNamespace(
                segment_id="E01-G01",
                order=1,
                continuity_group_id="classroom",
            ),
            SimpleNamespace(
                segment_id="E01-G02",
                order=2,
                continuity_group_id="classroom",
            ),
            SimpleNamespace(
                segment_id="E01-G03",
                order=3,
                continuity_group_id="classroom",
            ),
            SimpleNamespace(
                segment_id="E01-G04",
                order=4,
                continuity_group_id="corridor",
            ),
            SimpleNamespace(
                segment_id="E01-G05",
                order=5,
                continuity_group_id="corridor",
            ),
        ]
    )


def test_route_is_first_frame_at_episode_start_and_group_boundary() -> None:
    g1 = auto.decide_happyhorse_auto_route(_plan(), segment_id="E01-G01")
    g4_decision = auto.decide_happyhorse_auto_route(
        _plan(),
        segment_id="E01-G04",
    )

    assert g1.input_mode == "first_frame"
    assert g1.reason == "episode_start"
    assert g1.previous_segment_id is None
    assert g4_decision.input_mode == "first_frame"
    assert g4_decision.reason == "continuity_group_boundary"
    assert g4_decision.previous_segment_id == "E01-G03"
    assert g4_decision.previous_continuity_group_id == "classroom"
    assert g4_decision.target_continuity_group_id == "corridor"


def test_route_is_references_inside_each_continuity_group() -> None:
    for segment_id, previous in (
        ("E01-G02", "E01-G01"),
        ("E01-G03", "E01-G02"),
        ("E01-G05", "E01-G04"),
    ):
        decision = auto.decide_happyhorse_auto_route(
            _plan(),
            segment_id=segment_id,
        )
        assert decision.input_mode == "references"
        assert decision.reason == "same_continuity_group"
        assert decision.previous_segment_id == previous
        assert decision.continuity_required is True


def test_group_boundary_rejects_previous_end_frame() -> None:
    decision = auto.decide_happyhorse_auto_route(_plan(), segment_id="E01-G04")
    with pytest.raises(
        auto.HappyHorseAutoRouteError,
        match="starts a new continuity group",
    ):
        auto.materialize_auto_route_arguments(
            [
                "--segment-id",
                "E01-G04",
                "--input-mode",
                "first_frame",
                "--continuity-frame",
                "E01-G03_end.png",
                "--continuity-frame-metadata",
                "E01-G03_end.json",
            ],
            decision,
        )


def test_same_group_auto_attaches_exact_previous_pair(tmp_path: Path) -> None:
    root = tmp_path / "continuity"
    prior = root / "E01-G04"
    prior.mkdir(parents=True)
    frame = prior / "E01-G04_end.png"
    metadata = prior / "E01-G04_end.json"
    frame.write_bytes(b"frame")
    metadata.write_text("{}", encoding="utf-8")

    decision = auto.decide_happyhorse_auto_route(_plan(), segment_id="E01-G05")
    arguments = auto.materialize_auto_route_arguments(
        [
            "--segment-id",
            "E01-G05",
            "--output",
            str(tmp_path / "E01-G05.mp4"),
            "--continuity-dir",
            str(root),
        ],
        decision,
    )

    assert arguments[arguments.index("--input-mode") + 1] == "references"
    assert arguments[arguments.index("--continuity-frame") + 1] == str(frame)
    assert arguments[arguments.index("--continuity-frame-metadata") + 1] == str(metadata)


def test_same_group_fails_closed_when_previous_pair_is_missing(
    tmp_path: Path,
) -> None:
    decision = auto.decide_happyhorse_auto_route(_plan(), segment_id="E01-G02")
    with pytest.raises(
        auto.HappyHorseAutoRouteError,
        match="requires the verified end frame",
    ):
        auto.materialize_auto_route_arguments(
            [
                "--segment-id",
                "E01-G02",
                "--output",
                str(tmp_path / "E01-G02.mp4"),
            ],
            decision,
        )


def test_explicit_mode_cannot_override_the_plan() -> None:
    decision = auto.decide_happyhorse_auto_route(_plan(), segment_id="E01-G04")
    with pytest.raises(
        auto.HappyHorseAutoRouteError,
        match="plan selects --input-mode first_frame",
    ):
        auto.materialize_auto_route_arguments(
            [
                "--segment-id",
                "E01-G04",
                "--input-mode",
                "references",
            ],
            decision,
        )


def test_g4_direction_is_shared_by_the_generic_directed_runner() -> None:
    prompt = directed.append_segment_direction("base", segment_id="E01-G04")

    assert "hard scene cut" in prompt
    assert "approved E01-G04 first frame" in prompt
    assert "both paint cakes remain fully inside C01's anatomical right coat pocket" in prompt
    assert "Only C02 moves the mouth" in prompt
    assert "Only C01 moves the mouth" in prompt
    assert "Do not return to the classroom" in prompt


def test_route_report_records_why_i2v_or_r2v_was_selected(monkeypatch) -> None:
    decision = auto.decide_happyhorse_auto_route(_plan(), segment_id="E01-G04")
    original = base._base_report
    monkeypatch.setattr(base, "_base_report", lambda **kwargs: {"valid": True})

    with auto.install_auto_route_report(decision):
        report = base._base_report()

    assert report["auto_route"]["reason"] == "continuity_group_boundary"
    assert report["auto_route"]["input_mode"] == "first_frame"
    assert report["auto_route"]["continuity_required"] is False
    assert base._base_report is not original


def test_g4_compatibility_entrypoint_delegates_to_auto_router(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_main(arguments):
        captured["arguments"] = arguments
        return base.EXIT_OK

    monkeypatch.setattr(g4.auto_route, "main", fake_main)
    arguments = [
        "--generation-plan",
        "plan.json",
        "--segment-id",
        "E01-G04",
        "--input-mode",
        "first_frame",
    ]

    result = g4.main(arguments)

    assert result == base.EXIT_OK
    assert captured["arguments"] == arguments

    with pytest.raises(g4.G4FirstFrameError, match="sealed"):
        g4.validate_g4_arguments(
            ["--generation-plan", "plan.json", "--segment-id", "E01-G05"]
        )
