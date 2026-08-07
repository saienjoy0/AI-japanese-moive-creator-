"""Render E02-G05 through the proven HappyHorse selected-reference R2V route.

This keeps the successful E02-G04 path and changes only the target shot,
selected references, and provider-facing spoken readings. The authoritative
storyboard keeps its normal Japanese text, while HappyHorse receives hiragana
speech for stable pronunciation.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class E02G05SelectedR2VError(RuntimeError):
    """E02-G05 cannot safely use the reviewed selected-reference route."""


_EXPECTED_SUBJECTS = ("C01", "C03", "S03", "P05")
_EXPECTED_DIALOGUE = (
    "先生(spoken): そして明日は、どんなことがあっても学校へ来てください | "
    "先生(voice_over): あなたの顔を見ないと、私は悲しいわ"
)
_PROVIDER_LINE_1 = "そしてあしたは、どんなことがあってもがっこうへきてください"
_PROVIDER_LINE_2 = "あなたのかおをみないと、わたしはかなしいわ"


def build_e02_g05_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E02-G05":
        raise E02G05SelectedR2VError("selected-reference manifest is pinned to E02-G05")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E02G05SelectedR2VError(
            "E02-G05 master order changed; refusing unreviewed payload: "
            + ",".join(subjects)
        )
    return manifest


def rewrite_e02_g05_dialogue(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E02G05SelectedR2VError(
            f"E02-G05 dialogue changed or is missing: {dialogue_prompt!r}"
        )
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise E02G05SelectedR2VError(
            "could not locate generated E02-G05 spoken-dialogue instruction"
        )
    new_line = (
        "Dialogue delivery for E02-G05: 0.5-5.95s C03 says exactly "
        f"{_PROVIDER_LINE_1}. Use this hiragana reading literally for provider-facing "
        "Japanese speech. Only C03 moves the mouth for this spoken line; C01 keeps the "
        "mouth closed. 5.95-9.5s use C03 voice-over exactly "
        f"{_PROVIDER_LINE_2}. During the voice-over do not visibly lip-sync C03; C03 "
        "turns away and exits while the voice continues, and C01 stays silent with the "
        "mouth closed. Do not add any other words or vocalizations."
    )
    rewritten = prompt.replace(old_line, new_line, 1)
    forbidden = (
        "そして明日は、どんなことがあっても学校へ来てください",
        "あなたの顔を見ないと、私は悲しいわ",
    )
    for text in forbidden:
        if text in rewritten:
            raise E02G05SelectedR2VError(
                f"kanji dialogue leaked into final provider-facing prompt: {text}"
            )
    return rewritten


def append_e02_g05_continuity_prompt(prompt: str, *, existing_reference_count: int) -> str:
    if existing_reference_count != 4:
        raise E02G05SelectedR2VError(
            f"E02-G05 requires four masters before continuity; got {existing_reference_count}"
        )
    return (
        prompt
        + "\n"
        + "[Image 5] is the exact final frame of E02-G04 and is the primary same-scene "
        + "continuity reference. Preserve the S03 room layout, C01 identity seated on the "
        + "settee, C03 identity near the window, their clothing, lighting, and camera axis. "
        + "Continue without a scene cut. [Image 4] is only the approved species/style master "
        + "for P05: introduce exactly one in-story bunch of grapes for this E02 scene, with "
        + "no duplicate bunches, no giant advertising-style cluster, and no modern packaging."
    )


def e02_g05_direction() -> str:
    return (
        "Directed shot progression for E02-G05: "
        "0.0-0.5s continue directly from E02-G04 in the same S03 teacher room. C01 remains "
        "seated on the settee and C03 remains near the window. Preserve Image 1 as C01, "
        "Image 2 as C03, Image 3 as S03, Image 4 as the exact visual master for one P05 "
        "grape bunch, and Image 5 as the exact prior-frame continuity state. "
        "0.5-2.8s C03 says exactly そしてあしたは、どんなことがあってもがっこうへきてください "
        "while turning from the window, lifting exactly one P05 bunch from the small side "
        "surface beside the window, walking to C01, and gently placing that one bunch on "
        "C01's lap. Only C03 speaks; C01 remains silent. Do not make the grapes appear in "
        "C01's hands before C03 brings them into frame. 2.8-5.95s C03 stays beside C01 and "
        "finishes the same spoken line with a calm, compassionate, serious expression. "
        "This is reassurance and an invitation to return, not a celebratory reward for "
        "wrongdoing. C01 looks surprised, then holds the single bunch carefully on the lap. "
        "5.95-9.5s C03 turns toward the door and exits the room while C03 voice-over says "
        "exactly あなたのかおをみないと、わたしはかなしいわ. Do not visibly lip-sync the "
        "voice-over; C01 does not speak. 9.5-10.0s hold on C01 alone, seated on the settee "
        "with exactly one P05 bunch resting on the lap, absorbing the words. Final state: "
        "P05 is on C01's lap and C03 has exited the frame. No other people enter. Do not "
        "duplicate grapes, do not turn the bunch into a bowl or multiple clusters, do not "
        "make C01 eat the grapes yet, do not add dialogue, and do not add text, subtitles, "
        "logos, or watermarks."
    )


@contextmanager
def install_e02_g05_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e02_g05_manifest(prepared, plan, bundle, segment_id=segment_id)

    def segment_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id == "E02-G05":
            rewritten += "\n" + e02_g05_direction()
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_directed_continuity_prompt = append_e02_g05_continuity_prompt
    directed.append_segment_direction = segment_direction
    directed.rewrite_dialogue_delivery = rewrite_e02_g05_dialogue
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
        raise E02G05SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E02-G05":
            raise E02G05SelectedR2VError("this wrapper is pinned to E02-G05")
        if _option_value(arguments, "--input-mode") != "references":
            raise E02G05SelectedR2VError(
                "E02-G05 selected-reference route requires --input-mode references"
            )
        with install_e02_g05_policy():
            return directed.main(arguments)
    except E02G05SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
