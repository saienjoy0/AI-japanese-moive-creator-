"""Render E02-G03 through the proven HappyHorse selected-reference R2V route.

This keeps the successful E02-G02 path and changes only the target shot,
selected references, and provider-facing spoken readings. The authoritative
storyboard keeps its normal Japanese text, while HappyHorse receives hiragana
speech instructions for more stable pronunciation.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class E02G03SelectedR2VError(RuntimeError):
    """E02-G03 cannot safely use the reviewed selected-reference route."""


_EXPECTED_SUBJECTS = ("C01", "C03", "S03", "P02")
_EXPECTED_DIALOGUE = (
    "先生(spoken): 絵具は、もう返しましたか | "
    "先生(spoken): 自分のしたことを、悪いことだったと思っていますか | "
    "僕(spoken): ……はい。悪いことでした"
)
_PROVIDER_LINE_1 = "えのぐは、もうかえしましたか"
_PROVIDER_LINE_2 = "じぶんのしたことを、わるいことだったとおもっていますか"
_PROVIDER_LINE_3 = "……はい。わるいことでした"


def build_e02_g03_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E02-G03":
        raise E02G03SelectedR2VError("selected-reference manifest is pinned to E02-G03")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E02G03SelectedR2VError(
            "E02-G03 master order changed; refusing unreviewed payload: "
            + ",".join(subjects)
        )
    return manifest


def rewrite_e02_g03_dialogue(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E02G03SelectedR2VError(
            f"E02-G03 dialogue changed or is missing: {dialogue_prompt!r}"
        )
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise E02G03SelectedR2VError(
            "could not locate generated E02-G03 spoken-dialogue instruction"
        )
    new_line = (
        "Dialogue delivery for E02-G03: 1.2-3.2s C03 says exactly "
        f"{_PROVIDER_LINE_1}. 3.8-7.4s C03 says exactly {_PROVIDER_LINE_2}. "
        f"8.3-10.0s C01 says exactly {_PROVIDER_LINE_3}. Use these hiragana readings "
        "literally for provider-facing Japanese speech. During each line only the named "
        "speaker moves the mouth; the other person keeps the mouth closed. Between lines "
        "both mouths remain closed. Do not add any other words or vocalizations."
    )
    rewritten = prompt.replace(old_line, new_line, 1)
    forbidden = (
        "絵具は、もう返しましたか",
        "自分のしたことを、悪いことだったと思っていますか",
        "……はい。悪いことでした",
    )
    for text in forbidden:
        if text in rewritten:
            raise E02G03SelectedR2VError(
                f"kanji dialogue leaked into final provider-facing prompt: {text}"
            )
    return rewritten


def append_e02_g03_continuity_prompt(prompt: str, *, existing_reference_count: int) -> str:
    if existing_reference_count != 4:
        raise E02G03SelectedR2VError(
            f"E02-G03 requires four masters before continuity; got {existing_reference_count}"
        )
    return (
        prompt
        + "\n"
        + "[Image 5] is the exact final frame of E02-G02 and is the primary same-scene "
        + "continuity reference. Preserve the S03 room layout, C01 and C03 identities, "
        + "their relative positions, lighting, camera axis, closed door, C01's tearful "
        + "state, and the quiet two-person room. Continue without a scene cut."
    )


def e02_g03_direction() -> str:
    return (
        "Directed shot progression for E02-G03: "
        "0.0-1.2s continue directly from E02-G02 in the same S03 teacher room with only "
        "C01 and C03 present. Preserve Image 1 as C01, Image 2 as C03, Image 3 as S03, "
        "Image 4 as exactly two P02 solid paints on C03's desk, one indigo and one magenta, "
        "and Image 5 as the exact prior-frame continuity state. C01 remains tearful and "
        "looks down; C03 is calm and serious, not smiling. 1.2-3.2s C03 asks exactly "
        "えのぐは、もうかえしましたか. Only C03 speaks. C01 gives one small silent nod "
        "after the question. 3.2-3.8s quiet pause; both mouths closed. 3.8-7.4s C03 asks "
        "exactly じぶんのしたことを、わるいことだったとおもっていますか. Only C03 "
        "speaks, with a gentle but firm expression. 7.4-8.3s C01 breathes, looks at the two "
        "paints once, then lowers his eyes; no speech. 8.3-10.0s C01 answers exactly "
        "……はい。わるいことでした. Only C01 speaks, then gives one restrained nod. "
        "Final state: C01 has admitted wrongdoing, C03 remains calm, exactly two P02 remain "
        "on the desk, and no other people are present. Do not duplicate P02, do not bring "
        "C02 or C91 back, do not open the door, do not add dialogue, and do not add text, "
        "subtitles, logos, or watermarks."
    )


@contextmanager
def install_e02_g03_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e02_g03_manifest(prepared, plan, bundle, segment_id=segment_id)

    def segment_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id == "E02-G03":
            rewritten += "\n" + e02_g03_direction()
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_directed_continuity_prompt = append_e02_g03_continuity_prompt
    directed.append_segment_direction = segment_direction
    directed.rewrite_dialogue_delivery = rewrite_e02_g03_dialogue
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
        raise E02G03SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E02-G03":
            raise E02G03SelectedR2VError("this wrapper is pinned to E02-G03")
        if _option_value(arguments, "--input-mode") != "references":
            raise E02G03SelectedR2VError(
                "E02-G03 selected-reference route requires --input-mode references"
            )
        with install_e02_g03_policy():
            return directed.main(arguments)
    except E02G03SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
