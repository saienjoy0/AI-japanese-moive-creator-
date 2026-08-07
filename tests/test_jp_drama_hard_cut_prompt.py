from __future__ import annotations

import pytest

from src.apps.jp_drama.workflows import render_happyhorse_segment_hard_cut_canary as hard_cut
from src.apps.jp_drama.workflows import render_happyhorse_segment_canary as base


def test_g4_hard_cut_prompt_pins_opening_and_state_roles() -> None:
    prompt = hard_cut.append_hard_cut_prompt(
        "base prompt",
        existing_reference_count=7,
        segment_id="E01-G04",
    )

    assert "[Image 8] is the exact approved opening composition" in prompt
    assert "clean hard cut" in prompt
    assert "Do not morph from the classroom" in prompt
    assert "[Image 9] is the exact final frame of E01-G03" in prompt
    assert "state-only continuity reference" in prompt
    assert "Never copy its desk, classroom, hand pose, or framing" in prompt


def test_g4_prompt_preserves_exact_story_and_end_state() -> None:
    prompt = hard_cut.append_hard_cut_prompt(
        "base prompt",
        existing_reference_count=7,
        segment_id="E01-G04",
    )

    assert "藍と洋紅がない" in prompt
    assert "休みに教室にいたのは、君だけだ" in prompt
    assert "僕じゃない" in prompt
    assert "Exactly two paint wells are empty" in prompt
    assert "All other ten paint cakes remain" in prompt
    assert "P02 stays hidden" in prompt
    assert "does not fall" in prompt
    assert "closed teacher-room door" in prompt
    assert "do not show a teacher" in prompt
    assert "do not open the door" in prompt
    assert "C90 and C91 stay silent and secondary" in prompt


def test_g4_hard_cut_uses_exactly_nine_references() -> None:
    prompt = hard_cut.append_hard_cut_prompt(
        "base prompt",
        existing_reference_count=7,
        segment_id="E01-G04",
    )
    assert "[Image 8]" in prompt
    assert "[Image 9]" in prompt


def test_hard_cut_rejects_too_many_references() -> None:
    with pytest.raises(base.HappyHorseCanaryError):
        hard_cut.append_hard_cut_prompt(
            "base prompt",
            existing_reference_count=8,
            segment_id="E01-G04",
        )


def test_hard_cut_rejects_other_segments() -> None:
    with pytest.raises(hard_cut.HardCutCanaryError):
        hard_cut.append_hard_cut_prompt(
            "base prompt",
            existing_reference_count=7,
            segment_id="E01-G05",
        )


def test_g4_master_reference_set_is_frozen() -> None:
    assert list(hard_cut.EXPECTED_MASTER_ASSET_IDS) == [
        "ref_char_C01_aaf13357f6",
        "ref_char_C02_aaf13357f6",
        "ref_char_C90_aaf13357f6",
        "ref_char_C91_aaf13357f6",
        "ref_loc_S02_aaf13357f6",
        "ref_prop_P01_aaf13357f6",
        "ref_prop_P02_aaf13357f6",
    ]
    assert len(hard_cut.EXPECTED_MASTER_ASSET_HASHES) == 7
    assert all(item.startswith("sha256:") for item in hard_cut.EXPECTED_MASTER_ASSET_HASHES)
