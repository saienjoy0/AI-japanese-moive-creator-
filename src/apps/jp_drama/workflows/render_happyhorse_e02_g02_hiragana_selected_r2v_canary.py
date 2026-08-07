"""Render E02-G02 through the proven HappyHorse selected-reference R2V route.

This keeps the successful E02-G01 path and changes only the target shot,
selected references, and provider-facing spoken reading. The authoritative
storyboard keeps its normal Japanese text, while the actual HappyHorse speech
instruction uses hiragana only for more stable pronunciation.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class E02G02SelectedR2VError(RuntimeError):
    """E02-G02 cannot safely use the reviewed selected-reference route."""


_EXPECTED_SUBJECTS = ("C01", "C02", "C03", "C91", "S03")
_EXPECTED_DIALOGUE = "先生(spoken): もう行ってもよろしい"
_PROVIDER_SPOKEN_HIRAGANA = "もういってもよろしい"


def build_e02_g02_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E02-G02":
        raise E02G02SelectedR2VError("selected-reference manifest is pinned to E02-G02")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E02G02SelectedR2VError(
            "E02-G02 master order changed; refusing unreviewed payload: "
            + ",".join(subjects)
        )
    return manifest


def rewrite_e02_g02_dialogue(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E02G02SelectedR2VError(
            f"E02-G02 dialogue changed or is missing: {dialogue_prompt!r}"
        )
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise E02G02SelectedR2VError(
            "could not locate generated E02-G02 spoken-dialogue instruction"
        )
    new_line = (
        "Dialogue delivery for E02-G02: 2.5-5.0s C03 says exactly "
        f"{_PROVIDER_SPOKEN_HIRAGANA}. Use this hiragana reading literally for the "
        "provider-facing Japanese speech. Only C03 moves the mouth for this line; "
        "C01, C02, and C91 keep their mouths closed. C01 does not speak. C02 and C91 "
        "do not speak while leaving. Do not add any other words or vocalizations."
    )
    rewritten = prompt.replace(old_line, new_line, 1)
    if "もう行ってもよろしい" in rewritten:
        raise E02G02SelectedR2VError(
            "kanji dialogue leaked into the final provider-facing prompt"
        )
    return rewritten


def append_e02_g02_continuity_prompt(
    prompt: str,
    *,
    existing_reference_count: int,
) -> str:
    if existing_reference_count != 5:
        raise E02G02SelectedR2VError(
            f"E02-G02 requires five masters before continuity; got {existing_reference_count}"
        )
    return (
        prompt
        + "\n"
        + "[Image 6] is the exact final frame of E02-G01 and is the primary same-scene "
        + "continuity reference. Preserve the S03 room layout, C01/C02/C03/C91 identities, "
        + "their relative positions, lighting, camera axis, and the fact that C01 has just "
        + "begun crying after C03's question. Continue from that state without a scene cut."
    )


def e02_g02_direction() -> str:
    return (
        "Directed shot progression for E02-G02: "
        "0.0-2.5s continue directly from E02-G01 in the same S03 teacher room. C01 is "
        "quietly crying and looking down. C03 calmly turns toward C02 and C91. Preserve "
        "Image 1 as C01, Image 2 as C02, Image 3 as C03, Image 4 as C91, Image 5 as S03, "
        "and Image 6 as the exact prior-frame continuity state. 2.5-5.0s C03 says exactly "
        "もういってもよろしい in natural Japanese. Only C03 speaks and moves the mouth. "
        "C02 looks back at C01 once with a hurt but restrained expression, then exits through "
        "the same door. 5.0-7.0s C91 exits after C02 and the door closes quietly. C01 and C03 "
        "remain in their established positions. 7.0-10.0s hold a wider quiet composition of "
        "the same room with exactly C01 and C03 remaining; C01 keeps his head lowered and C03 "
        "waits without rushing him. Final state: C02 and C91 are fully gone, the door is closed, "
        "and only C01 and C03 remain in S03. Do not leave extra children in the room, do not "
        "duplicate any person, do not change the door location, do not add dialogue, and do not "
        "add text, subtitles, logos, or watermarks."
    )


@contextmanager
def install_e02_g02_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e02_g02_manifest(prepared, plan, bundle, segment_id=segment_id)

    def segment_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id == "E02-G02":
            rewritten += "\n" + e02_g02_direction()
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_directed_continuity_prompt = append_e02_g02_continuity_prompt
    directed.append_segment_direction = segment_direction
    directed.rewrite_dialogue_delivery = rewrite_e02_g02_dialogue
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
        raise E02G02SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E02-G02":
            raise E02G02SelectedR2VError("this wrapper is pinned to E02-G02")
        if _option_value(arguments, "--input-mode") != "references":
            raise E02G02SelectedR2VError(
                "E02-G02 selected-reference route requires --input-mode references"
            )
        with install_e02_g02_policy():
            return directed.main(arguments)
    except E02G02SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
