"""Render E02-G04 through the proven HappyHorse selected-reference R2V route.

This keeps the successful E02-G03 path and changes only the target shot,
selected references, and provider-facing spoken reading. The authoritative
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


class E02G04SelectedR2VError(RuntimeError):
    """E02-G04 cannot safely use the reviewed selected-reference route."""


_EXPECTED_SUBJECTS = ("C01", "C03", "S03")
_EXPECTED_DIALOGUE = "先生(spoken): よく分かったなら、それでいいの。もう泣かなくていい"
_PROVIDER_SPOKEN_HIRAGANA = "よくわかったなら、それでいいの。もうなかなくていい"


def build_e02_g04_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E02-G04":
        raise E02G04SelectedR2VError("selected-reference manifest is pinned to E02-G04")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E02G04SelectedR2VError(
            "E02-G04 master order changed; refusing unreviewed payload: "
            + ",".join(subjects)
        )
    return manifest


def rewrite_e02_g04_dialogue(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E02G04SelectedR2VError(
            f"E02-G04 dialogue changed or is missing: {dialogue_prompt!r}"
        )
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise E02G04SelectedR2VError(
            "could not locate generated E02-G04 spoken-dialogue instruction"
        )
    new_line = (
        "Dialogue delivery for E02-G04: 0.8-6.8s C03 says exactly "
        f"{_PROVIDER_SPOKEN_HIRAGANA}. Use this hiragana reading literally for the "
        "provider-facing Japanese speech. Only C03 moves the mouth for this line; C01 "
        "keeps the mouth closed and does not speak. After the line both mouths remain "
        "closed. Do not add any other words or vocalizations."
    )
    rewritten = prompt.replace(old_line, new_line, 1)
    if "よく分かったなら、それでいいの。もう泣かなくていい" in rewritten:
        raise E02G04SelectedR2VError(
            "kanji dialogue leaked into the final provider-facing prompt"
        )
    return rewritten


def append_e02_g04_continuity_prompt(prompt: str, *, existing_reference_count: int) -> str:
    if existing_reference_count != 3:
        raise E02G04SelectedR2VError(
            f"E02-G04 requires three masters before continuity; got {existing_reference_count}"
        )
    return (
        prompt
        + "\n"
        + "[Image 4] is the exact final frame of E02-G03 and is the primary same-scene "
        + "continuity reference. Preserve the S03 room layout, C01 and C03 identities, "
        + "their relative positions, lighting, camera axis, and C01's just-admitted, "
        + "tearful but calmer state. Continue without a scene cut. The two paint cakes "
        + "visible at the desk edge in the prior frame are background continuity only: "
        + "leave them untouched and do not create additional paint objects."
    )


def e02_g04_direction() -> str:
    return (
        "Directed shot progression for E02-G04: "
        "0.0-0.8s continue directly from E02-G03 in the same S03 teacher room with only "
        "C01 and C03 present. Preserve Image 1 as C01, Image 2 as C03, Image 3 as S03, "
        "and Image 4 as the exact prior-frame continuity state. C01 has just admitted "
        "wrongdoing and remains emotional but quieter. C03 is calm and accepting, not "
        "overly smiling. 0.8-6.8s C03 says exactly よくわかったなら、それでいいの。もうなかなくていい "
        "in natural Japanese. Only C03 speaks and moves the mouth. While speaking, C03 "
        "uses an open-palm gesture toward the nearby settee; do not grab C01's shoulder, "
        "arm, face, or clothing. C01 understands the gesture and sits down by himself on "
        "the settee, remaining silent. 6.8-8.0s C01 stays seated, breathes out, and the "
        "crying visibly eases; both mouths closed. 8.0-10.0s C03 turns slightly away and "
        "takes a small step toward the window, stopping near it while C01 remains seated. "
        "Final state: C01 is seated on the settee, C03 is near and oriented toward the "
        "window, the room is still S03, and no other people enter. If the two paint cakes "
        "from the prior frame remain visible, keep exactly those two stationary in the "
        "background and do not feature them. Do not duplicate people, do not add physical "
        "contact, do not add dialogue, and do not add text, subtitles, logos, or watermarks."
    )


@contextmanager
def install_e02_g04_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e02_g04_manifest(prepared, plan, bundle, segment_id=segment_id)

    def segment_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id == "E02-G04":
            rewritten += "\n" + e02_g04_direction()
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_directed_continuity_prompt = append_e02_g04_continuity_prompt
    directed.append_segment_direction = segment_direction
    directed.rewrite_dialogue_delivery = rewrite_e02_g04_dialogue
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
        raise E02G04SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E02-G04":
            raise E02G04SelectedR2VError("this wrapper is pinned to E02-G04")
        if _option_value(arguments, "--input-mode") != "references":
            raise E02G04SelectedR2VError(
                "E02-G04 selected-reference route requires --input-mode references"
            )
        with install_e02_g04_policy():
            return directed.main(arguments)
    except E02G04SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
