"""Render E03-G02 through the proven HappyHorse selected-reference R2V route.

This keeps the successful E03-G01 path and changes only the target shot,
selected references, continuity frame, and provider-facing inner-monologue reading.
The authoritative storyboard text stays unchanged.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class E03G02SelectedR2VError(RuntimeError):
    """E03-G02 cannot safely use the reviewed selected-reference route."""


_EXPECTED_SUBJECTS = ("C01", "S04", "P07")
_EXPECTED_DIALOGUE = "僕(inner_monologue): 先生に、もう一度会いたい"
_PROVIDER_HIRAGANA = "せんせいに、もういちどあいたい"


def build_e03_g02_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E03-G02":
        raise E03G02SelectedR2VError("selected-reference manifest is pinned to E03-G02")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E03G02SelectedR2VError(
            "E03-G02 master order changed; refusing unreviewed payload: "
            + ",".join(subjects)
        )
    return manifest


def rewrite_e03_g02_dialogue(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E03G02SelectedR2VError(
            f"E03-G02 dialogue changed or is missing: {dialogue_prompt!r}"
        )
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        old_line = (
            "Inner voice-over: Use a natural Japanese internal monologue with these "
            "exact words: 先生に、もう一度会いたい The visible character does not speak "
            "aloud. Keep the mouth closed and relaxed, with no lip synchronization, "
            "mouthing, or visible articulation."
        )
    if old_line not in prompt:
        raise E03G02SelectedR2VError(
            "could not locate generated E03-G02 inner-monologue instruction"
        )
    new_line = (
        "Provider-facing inner voice for E03-G02: 1.2-5.2s C01 thinks exactly "
        f"{_PROVIDER_HIRAGANA}. Use this hiragana reading literally for Japanese speech. "
        "This is internal voice-over only: C01 keeps the mouth closed and relaxed for the "
        "entire line, with no lip synchronization or visible articulation. Do not add any "
        "other words or vocalizations."
    )
    rewritten = prompt.replace(old_line, new_line, 1)
    if "先生に、もう一度会いたい" in rewritten:
        raise E03G02SelectedR2VError(
            "kanji dialogue leaked into the final provider-facing prompt"
        )
    return rewritten


def append_e03_g02_continuity_prompt(prompt: str, *, existing_reference_count: int) -> str:
    if existing_reference_count != 3:
        raise E03G02SelectedR2VError(
            f"E03-G02 requires three masters before continuity; got {existing_reference_count}"
        )
    return (
        prompt
        + "\n"
        + "[Image 4] is the exact final frame of E03-G01 and is the primary same-scene "
        + "continuity reference. Preserve C01 outside the same S04 school gate, the P07 "
        + "brown leather satchel, autumn morning lighting, camera axis, screen direction, "
        + "and C01's hesitant posture. Continue directly without a scene cut. Image 4 is "
        + "the opening state only; the action must progress until C01 crosses the gate."
    )


def e03_g02_direction() -> str:
    return (
        "Directed shot progression for E03-G02: 0.0-1.2s continue exactly from E03-G01 "
        "with C01 still outside S04, facing inward, carrying exactly one P07 brown leather "
        "satchel. No other people are present. 1.2-5.2s the internal voice says exactly "
        "せんせいに、もういちどあいたい while C01 keeps the mouth closed. He takes one "
        "quiet breath, grips the satchel strap slightly, and decides to move forward; keep "
        "the acting restrained, not heroic. 5.2-8.2s C01 walks through the same gate at a "
        "normal child pace in one continuous action. Do not run, jump, pose, or look triumphant. "
        "8.2-10.0s C01 stops just inside the gate and turns his gaze toward the school building. "
        "Final state: C01 is clearly inside S04 facing the school, P07 remains one unchanged "
        "brown leather satchel, and the emotion is small courage with some lingering anxiety. "
        "Preserve the exact period gate architecture and costume. No C02 or C03, no other "
        "students, no cars, modern roads, power lines, modern signs, readable text, subtitles, "
        "logos, watermarks, extra dialogue, or duplicated props."
    )


@contextmanager
def install_e03_g02_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e03_g02_manifest(prepared, plan, bundle, segment_id=segment_id)

    def segment_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id == "E03-G02":
            rewritten += "\n" + e03_g02_direction()
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_directed_continuity_prompt = append_e03_g02_continuity_prompt
    directed.append_segment_direction = segment_direction
    directed.rewrite_dialogue_delivery = rewrite_e03_g02_dialogue
    try:
        yield
    finally:
        base.build_wan_master_reference_manifest = original_builder
        directed.append_directed_continuity_prompt = original_continuity
        directed.append_segment_direction = original_direction
        directed.rewrite_dialogue_delivery = original_dialogue


def _option_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise E03G02SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E03-G02":
            raise E03G02SelectedR2VError("this wrapper is pinned to E03-G02")
        if _option_value(arguments, "--input-mode") != "references":
            raise E03G02SelectedR2VError(
                "E03-G02 selected-reference route requires --input-mode references"
            )
        with install_e03_g02_policy():
            return directed.main(arguments)
    except E03G02SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
