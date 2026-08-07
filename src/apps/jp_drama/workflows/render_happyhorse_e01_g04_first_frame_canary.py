"""Render E01-G04 from its own approved first frame, never from G03 continuity.

E01-G03 and E01-G04 belong to different continuity groups and locations.  This
small wrapper therefore keeps the existing HappyHorse ledger, approval gate,
I2V transport, and automatic end-frame extraction, while enforcing the scene
boundary required by the episode plan:

* E01-G04 must use ``--input-mode first_frame``;
* an E01-G03 continuity frame must not be supplied;
* the approved E01-G04 first frame defines the corridor opening composition;
* the generated G04 end frame is still extracted for the same-group G04->G05
  handoff by the existing continuity runner.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any, Iterator

from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_continuity_canary as continuity


class G4FirstFrameError(RuntimeError):
    """The E01-G04 scene-boundary request is ambiguous or unsafe."""


G4_DIRECTION = (
    "Directed shot progression for E01-G04: This is a hard scene cut from the "
    "classroom in E01-G03 into the S02 school corridor. Do not begin from, "
    "recreate, dissolve from, or visually morph the E01-G03 final frame. "
    "0.0-1.0s begin exactly from the approved E01-G04 first frame: C01 and C02 "
    "face each other in the corridor, C90 and C91 remain secondary and softly "
    "focused, and C02 holds the open wooden P01 so the two missing paint spaces "
    "are clear. P02 is not visible because both paint cakes remain fully inside "
    "C01's anatomical right coat pocket. C01's hands are empty. "
    "1.0-6.5s C02 says exactly: 藍と洋紅がない。休みに教室にいたのは、君だけだ. "
    "Only C02 moves the mouth for this line; C01, C90, and C91 keep their mouths "
    "closed. Use restrained concern rather than shouting or villainous anger. "
    "6.5-8.0s hold a short silent reaction on C01 becoming more cornered, without "
    "revealing the pocket contents. 8.0-10.0s C01 says exactly: 僕じゃない. "
    "Only C01 moves the mouth for this reply; C02, C90, and C91 keep their mouths "
    "closed. Preserve the approved faces, ages, costumes, corridor geometry, "
    "screen direction, P01 identity, and the exact count of two hidden P02. "
    "Do not return to the classroom, repeat the theft, show P02 in P01, a hand, "
    "or on the floor, introduce the teacher, drop the paints, or add text."
)


def _option_value(
    arguments: list[str],
    option: str,
    default: str | None = None,
) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return default
    if index + 1 >= len(arguments):
        raise G4FirstFrameError(f"{option} requires a value")
    return arguments[index + 1]


def validate_g4_arguments(arguments: list[str]) -> None:
    """Require the dedicated G04 I2V route before any provider boundary."""

    segment_id = _option_value(arguments, "--segment-id")
    if segment_id != "E01-G04":
        raise G4FirstFrameError(
            "this runner is sealed to --segment-id E01-G04"
        )

    input_mode = _option_value(arguments, "--input-mode", "first_frame")
    if input_mode != "first_frame":
        raise G4FirstFrameError(
            "E01-G04 must use --input-mode first_frame because G03 is a different scene"
        )

    forbidden = [
        option
        for option in ("--continuity-frame", "--continuity-frame-metadata")
        if option in arguments
    ]
    if forbidden:
        raise G4FirstFrameError(
            "E01-G04 must not consume E01-G03 continuity inputs: "
            + ", ".join(forbidden)
        )


def append_g4_direction(prompt: str) -> str:
    """Attach the reviewed corridor blocking and two-speaker delivery policy."""

    return prompt + "\n" + G4_DIRECTION


@contextmanager
def install_g4_prompt() -> Iterator[None]:
    """Temporarily add the G04 direction to the existing I2V prompt builder."""

    original_build_prompt = base.build_happyhorse_prompt

    def g4_build_prompt(
        bundle: Any,
        *,
        reference_context: str | None = None,
    ) -> str:
        prompt = original_build_prompt(
            bundle,
            reference_context=reference_context,
        )
        return append_g4_direction(prompt)

    base.build_happyhorse_prompt = g4_build_prompt
    try:
        yield
    finally:
        base.build_happyhorse_prompt = original_build_prompt


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        validate_g4_arguments(arguments)
        with install_g4_prompt():
            # The continuity runner is intentionally retained only for its normal
            # post-render G04 end-frame extraction. In first_frame mode it does not
            # resolve or attach a previous-segment continuity image.
            return continuity.main(arguments)
    except G4FirstFrameError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
