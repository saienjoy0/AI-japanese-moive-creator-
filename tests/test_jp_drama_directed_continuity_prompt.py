from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.apps.jp_drama.generation.models import PromptBundle
from src.apps.jp_drama.workflows import render_happyhorse_segment_canary as base
from src.apps.jp_drama.workflows import (
    render_happyhorse_segment_continuity_canary as continuity,
)
from src.apps.jp_drama.workflows.render_happyhorse_segment_directed_continuity_canary import (
    DirectedPromptError,
    append_directed_continuity_prompt,
    append_segment_direction,
    inner_monologue_text,
    install_directed_prompt,
    rewrite_dialogue_delivery,
)


def _bundle(dialogue_prompt: str) -> PromptBundle:
    return PromptBundle(
        narrative_summary="ジムの二色",
        visual_prompt="明治期の教室、木製の絵具箱、藍色と洋紅色",
        motion_prompt="もどかしさから執着へ変化する",
        camera_prompt="close_up, eye_level, static, slow",
        timed_shot_prompt="0-10秒で絵具箱が開き、僕が二色へ惹かれる",
        dialogue_prompt=dialogue_prompt,
        audio_prompt="自然な日本語と静かな教室の環境音",
        negative_constraints=["字幕", "現代ロゴ"],
    )


def test_extracts_exact_inner_monologue_words() -> None:
    assert inner_monologue_text("僕(inner_monologue): あの二色さえあれば……") == (
        "あの二色さえあれば……"
    )
    assert inner_monologue_text("僕: 声に出す台詞") is None


def test_inner_monologue_replaces_visible_lip_sync_instruction() -> None:
    bundle = _bundle("僕(inner_monologue): あの二色さえあれば……")
    original = base.build_happyhorse_prompt(bundle)

    rewritten = rewrite_dialogue_delivery(original, bundle.dialogue_prompt)

    assert "Inner voice-over" in rewritten
    assert "あの二色さえあれば……" in rewritten
    assert "does not speak aloud" in rewritten
    assert "Keep the mouth closed" in rewritten
    assert "synchronize the visible speaker's mouth" not in rewritten


def test_inner_monologue_rewrite_fails_closed_when_template_changed() -> None:
    with pytest.raises(DirectedPromptError, match="could not locate"):
        rewrite_dialogue_delivery(
            "a different prompt template",
            "僕(inner_monologue): あの二色さえあれば……",
        )


def test_normal_spoken_dialogue_keeps_existing_lip_sync_instruction() -> None:
    bundle = _bundle("僕: 見つけた")
    original = base.build_happyhorse_prompt(bundle)

    assert rewrite_dialogue_delivery(original, bundle.dialogue_prompt) == original
    assert "synchronize the visible speaker's mouth" in original


def test_g2_direction_prevents_early_theft_and_pins_two_paints() -> None:
    prompt = append_segment_direction("base", segment_id="E01-G02")

    assert "looking screen-right" in prompt
    assert "from screen-right" in prompt
    assert "reversing screen direction" in prompt
    assert "exactly two P02 solid paint cakes" in prompt
    assert "one indigo and one magenta" in prompt
    assert "P02 remains inside P01" in prompt
    assert "never touches, removes, or steals" in prompt
    assert "Do not return to the harbor memory" in prompt


def test_other_segments_do_not_receive_g2_specific_direction() -> None:
    assert append_segment_direction("base", segment_id="E01-G03") == "base"


def test_continuity_frame_is_opening_anchor_not_frozen_composition(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        base,
        "HappyHorse11R2VModel",
        SimpleNamespace(MAX_REFERENCE_IMAGES=9),
    )

    prompt = append_directed_continuity_prompt(
        "[Image 1] C01",
        existing_reference_count=5,
    )

    assert "[Image 6] is the exact final frame" in prompt
    assert "opening frame and continuity anchor only" in prompt
    assert "let the shot evolve naturally" in prompt
    assert "Do not freeze, loop, or repeat" in prompt


def test_continuity_frame_still_respects_nine_image_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        base,
        "HappyHorse11R2VModel",
        SimpleNamespace(MAX_REFERENCE_IMAGES=9),
    )
    with pytest.raises(RuntimeError, match="exceed 9 images"):
        append_directed_continuity_prompt("base", existing_reference_count=9)


def test_context_installs_and_restores_both_prompt_policies() -> None:
    bundle = _bundle("僕(inner_monologue): あの二色さえあれば……")
    original_build = base.build_happyhorse_prompt
    original_continuity = continuity.append_continuity_prompt

    with install_directed_prompt("E01-G02"):
        prompt = base.build_happyhorse_prompt(bundle)
        assert "Inner voice-over" in prompt
        assert "Directed shot progression for E01-G02" in prompt
        assert continuity.append_continuity_prompt is not original_continuity

    assert base.build_happyhorse_prompt is original_build
    assert continuity.append_continuity_prompt is original_continuity
