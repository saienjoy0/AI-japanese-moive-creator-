from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.apps.jp_drama.workflows import render_happyhorse_segment_canary as base
from src.apps.jp_drama.workflows import (
    render_happyhorse_e01_g04_first_frame_canary as g4,
)


def _arguments(*extra: str) -> list[str]:
    return [
        "--segment-id",
        "E01-G04",
        "--input-mode",
        "first_frame",
        "--output",
        "E01-G04.mp4",
        *extra,
    ]


def test_accepts_only_g4_first_frame_route() -> None:
    g4.validate_g4_arguments(_arguments())

    with pytest.raises(g4.G4FirstFrameError, match="sealed"):
        g4.validate_g4_arguments(
            [
                "--segment-id",
                "E01-G05",
                "--input-mode",
                "first_frame",
            ]
        )

    with pytest.raises(g4.G4FirstFrameError, match="must use --input-mode first_frame"):
        g4.validate_g4_arguments(
            [
                "--segment-id",
                "E01-G04",
                "--input-mode",
                "references",
            ]
        )


def test_rejects_g3_continuity_inputs() -> None:
    with pytest.raises(g4.G4FirstFrameError, match="must not consume"):
        g4.validate_g4_arguments(
            _arguments(
                "--continuity-frame",
                "E01-G03_end.png",
                "--continuity-frame-metadata",
                "E01-G03_end.json",
            )
        )


def test_g4_direction_pins_hard_cut_dialogue_and_hidden_paints() -> None:
    prompt = g4.append_g4_direction("base")

    assert "hard scene cut" in prompt
    assert "Do not begin from" in prompt
    assert "approved E01-G04 first frame" in prompt
    assert "C02 holds the open wooden P01" in prompt
    assert "both paint cakes remain fully inside C01's anatomical right coat pocket" in prompt
    assert "Only C02 moves the mouth" in prompt
    assert "Only C01 moves the mouth" in prompt
    assert "僕じゃない" in prompt
    assert "Do not return to the classroom" in prompt
    assert "show P02 in P01" in prompt


def test_prompt_context_is_temporary(monkeypatch) -> None:
    original = base.build_happyhorse_prompt
    bundle = SimpleNamespace(
        narrative_summary="疑いと嘘",
        visual_prompt="学校の廊下",
        motion_prompt="恐怖から追い詰められる",
        camera_prompt="medium, eye_level, static, slow",
        timed_shot_prompt="C02が紛失を告げ、C01が否定する",
        dialogue_prompt="ジム(spoken): 藍と洋紅がない | 僕(spoken): 僕じゃない",
        audio_prompt="日本語の台詞と控えめな環境音",
        negative_constraints=["字幕"],
    )

    with g4.install_g4_prompt():
        prompt = base.build_happyhorse_prompt(bundle)
        assert "Directed shot progression for E01-G04" in prompt
        assert "approved E01-G04 first frame" in prompt

    assert base.build_happyhorse_prompt is original


def test_main_passes_first_frame_request_to_existing_continuity_runner(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_main(arguments):
        captured["arguments"] = arguments
        captured["prompt_builder"] = base.build_happyhorse_prompt
        return base.EXIT_OK

    monkeypatch.setattr(g4.continuity, "main", fake_main)

    result = g4.main(_arguments("--stage", "preflight"))

    assert result == base.EXIT_OK
    assert captured["arguments"] == _arguments("--stage", "preflight")
    assert captured["prompt_builder"] is not base.build_happyhorse_prompt
