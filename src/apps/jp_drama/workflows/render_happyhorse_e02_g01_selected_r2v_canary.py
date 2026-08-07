"""Render E02-G01 through the proven HappyHorse selected-reference R2V route.

The shot is the first segment of episode two. It uses the six authoritative
masters required by the pinned storyboard (C01, C02, C03, C91, S03, P02) plus
the exact E01-G05 end frame as a seventh reference. The previous frame carries
teacher/doorway state only; the new shot is a hard move into S03 and must not
copy the S02 corridor composition.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class E02G01SelectedR2VError(RuntimeError):
    """E02-G01 cannot safely use the reviewed selected-reference route."""


_EXPECTED_SUBJECTS = ("C01", "C02", "C03", "C91", "S03", "P02")
_EXPECTED_DIALOGUE = (
    "追及する級友(spoken): この子が、ジムの絵具を取りました | "
    "先生(spoken): それは本当ですか"
)


def build_e02_g01_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E02-G01":
        raise E02G01SelectedR2VError("selected-reference manifest is pinned to E02-G01")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E02G01SelectedR2VError(
            "E02-G01 master order changed; refusing unreviewed payload: "
            + ",".join(subjects)
        )
    return manifest


def rewrite_e02_g01_dialogue(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E02G01SelectedR2VError(
            f"E02-G01 dialogue changed or is missing: {dialogue_prompt!r}"
        )
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise E02G01SelectedR2VError(
            "could not locate generated E02-G01 spoken-dialogue instruction"
        )
    new_line = (
        "Dialogue delivery for E02-G01: 4.0-6.0s C91 says exactly "
        "この子が、ジムの絵具を取りました. Only C91 moves the mouth for this "
        "line; C01, C02, and C03 keep their mouths closed. 8.0-10.0s C03 says "
        "exactly それは本当ですか. Only C03 moves the mouth for this line; C01, "
        "C02, and C91 keep their mouths closed. C01 gives no spoken reply in this "
        "segment; his lips may tremble silently while one restrained tear appears. "
        "Do not add any other words."
    )
    return prompt.replace(old_line, new_line, 1)


def append_e02_g01_continuity_prompt(
    prompt: str,
    *,
    existing_reference_count: int,
) -> str:
    if existing_reference_count != 6:
        raise E02G01SelectedR2VError(
            f"E02-G01 requires six masters before continuity; got {existing_reference_count}"
        )
    return (
        prompt
        + "\n"
        + "[Image 7] is the exact final frame of E01-G05. Use Image 7 only as the "
        + "immediate story-state and C03 doorway continuity reference: preserve C03's "
        + "face, hair, white dress, restrained expression, lighting direction, and the "
        + "fact that the teacher-room door has just opened. Do not copy the S02 corridor "
        + "composition into E02-G01. The new primary location is the S03 teacher room "
        + "defined by Image 5."
    )


def e02_g01_direction() -> str:
    return (
        "Directed shot progression for E02-G01: "
        "0.0-2.0s continue immediately after E01-G05: from the open doorway, C01, C02, "
        "C03, and C91 enter the quiet S03 teacher room. Use Image 1 for C01, Image 2 "
        "for C02, Image 3 for C03, Image 4 for C91, Image 5 for S03, and Image 6 for "
        "exactly two P02 solid paints, one indigo and one magenta. C01 enters last and "
        "looks pale and trapped. 2.0-4.0s show C91 placing exactly two P02 on C03's desk "
        "with one small natural placement action; no extra paints appear and nobody else "
        "touches them. 4.0-6.0s C91 reports exactly: この子が、ジムの絵具を取りました. "
        "Only C91 speaks. C02 remains hurt but restrained in the background. 6.0-8.0s "
        "C03 silently looks in sequence at the two P02, then C02, then C01; C03 does not "
        "smile or shout. 8.0-10.0s C03 asks exactly: それは本当ですか. Only C03 speaks. "
        "C01 cannot answer; his lips tremble silently and one restrained tear falls. "
        "Final state: exactly two P02 remain on C03's desk, C03 is facing C01 after the "
        "question, and C01 is silent and tearful. Preserve all four identities and period "
        "costumes. Do not return to S02, do not show P01, do not add background students, "
        "do not duplicate P02, and do not add text, subtitles, logos, or extra dialogue."
    )


@contextmanager
def install_e02_g01_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e02_g01_manifest(prepared, plan, bundle, segment_id=segment_id)

    def segment_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id == "E02-G01":
            rewritten += "\n" + e02_g01_direction()
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_directed_continuity_prompt = append_e02_g01_continuity_prompt
    directed.append_segment_direction = segment_direction
    directed.rewrite_dialogue_delivery = rewrite_e02_g01_dialogue
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
        raise E02G01SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E02-G01":
            raise E02G01SelectedR2VError("this wrapper is pinned to E02-G01")
        if _option_value(arguments, "--input-mode") != "references":
            raise E02G01SelectedR2VError(
                "E02-G01 selected-reference route requires --input-mode references"
            )
        with install_e02_g01_policy():
            return directed.main(arguments)
    except E02G01SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
