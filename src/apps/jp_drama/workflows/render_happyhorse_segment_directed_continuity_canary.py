"""Run HappyHorse continuity with dialogue-aware and motion-first prompting.

The existing continuity wrapper safely binds the immediately previous segment's
SHA-verified end frame as an additional R2V reference. This module adds the two
prompting rules required by production use:

1. dialogue tagged as an inner monologue is delivered as off-screen/internal
   voice-over and must never request visible lip synchronization;
2. the previous segment frame defines only the opening state. The generated
   shot must evolve from that state according to the planned timing instead of
   freezing the previous composition for the entire clip.

The normal provider ledger, paid-call gate, asset validation, and end-frame
extraction remain owned by the existing wrappers. Segment-specific reviewed
directions are shared by both first-frame I2V and same-group continuity R2V.
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from typing import Any, Iterator

from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_continuity_canary as continuity


class DirectedPromptError(RuntimeError):
    """The production prompt cannot be rewritten without ambiguity."""


_INNER_MONOLOGUE_PATTERN = re.compile(
    r"\((?:inner_monologue|voice_over|voiceover|narration)\)\s*[:：]\s*(?P<text>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


_SEGMENT_DIRECTIONS: dict[str, str] = {
    "E01-G02": (
        "Directed shot progression for E01-G02: "
        "0.0-1.5s begin from the immediately previous segment's final frame, "
        "with C01 already looking screen-right toward the adjacent desk. "
        "1.5-4.0s reveal C02's hands and the wooden P01 paint box from screen-right, "
        "following the established gaze direction without teleporting characters or "
        "reversing screen direction. "
        "4.0-7.0s C02 opens P01 and reveals exactly two P02 solid paint cakes, "
        "one indigo and one magenta, still inside the box. "
        "7.0-10.0s hold on C01's eyes and restrained fixation using a subtle rack "
        "focus or minimal reframing while the camera remains mostly static. "
        "P02 remains inside P01 for the entire segment. C01 never touches, removes, "
        "or steals either paint in E01-G02. Do not return to the harbor memory."
    ),
    "E01-G03": (
        "Directed shot progression for E01-G03: "
        "0.0-1.5s begin from the immediately previous segment's final frame with "
        "C01's face centered and his eyes lowered toward the open P01 just below "
        "the frame. Keep his mouth closed during the inner voice-over. "
        "1.5-3.5s use a slow controlled tilt or minimal downward reframe to show "
        "the open wooden P01 on the desk and exactly two P02 paint cakes, one indigo "
        "and one magenta. C02 and all other people remain off-screen. "
        "3.5-6.5s C01's right hand takes both P02 together in one continuous action "
        "and places both into C01's anatomical right coat pocket, which is on "
        "screen-left in this frontal composition. No duplication, dropping, color "
        "swapping, hand switching, or leaving either paint inside P01. "
        "6.5-8.0s the same right hand returns and gently closes P01; P01 remains on "
        "the desk and C01 does not carry the box away. "
        "8.0-10.0s reframe to C01's face and upper body. He freezes with subtly "
        "widened eyes and tense breathing, changing from fixation to fear. "
        "At the end exactly two P02 are fully inside C01's anatomical right coat "
        "pocket, P01 is closed on the desk, and both hands are empty. Do not show "
        "discovery, escape, a teacher, C02, or later consequences."
    ),
    "E01-G04": (
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
    ),
}


def _option_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise DirectedPromptError(f"{option} requires a value")
    return arguments[index + 1]


def inner_monologue_text(dialogue_prompt: str | None) -> str | None:
    """Return the exact inner-monologue words, or ``None`` for spoken dialogue."""

    if not dialogue_prompt:
        return None
    match = _INNER_MONOLOGUE_PATTERN.search(dialogue_prompt.strip())
    if match is None:
        return None
    text = match.group("text").strip()
    if not text:
        raise DirectedPromptError("inner monologue has no text")
    return text


def rewrite_dialogue_delivery(prompt: str, dialogue_prompt: str | None) -> str:
    """Replace visible lip-sync instructions for tagged inner monologue."""

    words = inner_monologue_text(dialogue_prompt)
    if words is None:
        return prompt

    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise DirectedPromptError(
            "could not locate the generated visible lip-sync instruction for "
            "the tagged inner monologue"
        )
    new_line = (
        "Inner voice-over: Use a natural Japanese internal monologue with these "
        f"exact words: {words} The visible character does not speak aloud. Keep "
        "the mouth closed and relaxed, with no lip synchronization, mouthing, or "
        "visible articulation."
    )
    return prompt.replace(old_line, new_line, 1)


def append_segment_direction(prompt: str, *, segment_id: str) -> str:
    """Add a reviewed segment-specific shot progression when one is pinned."""

    direction = _SEGMENT_DIRECTIONS.get(segment_id)
    if direction is None:
        return prompt
    return prompt + "\n" + direction


def append_directed_continuity_prompt(
    prompt: str,
    *,
    existing_reference_count: int,
) -> str:
    """Treat the prior end frame as the opening state, not a frozen composition."""

    reference_number = existing_reference_count + 1
    if reference_number > base.HappyHorse11R2VModel.MAX_REFERENCE_IMAGES:
        raise base.HappyHorseCanaryError(
            "HappyHorse R2V master references plus continuity frame exceed 9 images"
        )
    return (
        prompt
        + "\n"
        + f"[Image {reference_number}] is the exact final frame of the immediately "
        + "previous segment. Use it as the opening frame and continuity anchor only. "
        + "Begin with its pose, gaze direction, framing, lighting, screen direction, "
        + "and object positions, then let the shot evolve naturally according to the "
        + "Timing, Motion, and directed shot progression above. Do not freeze, loop, "
        + "or repeat the opening composition for the whole clip. Preserve spatial "
        + "continuity while allowing only the planned characters and props to enter "
        + "from the established gaze side."
    )


@contextmanager
def install_directed_prompt(segment_id: str) -> Iterator[None]:
    """Temporarily install the reviewed prompt policy around the existing runner."""

    original_build_prompt = base.build_happyhorse_prompt
    original_continuity_prompt = continuity.append_continuity_prompt

    def directed_build_prompt(
        bundle: Any,
        *,
        reference_context: str | None = None,
    ) -> str:
        prompt = original_build_prompt(
            bundle,
            reference_context=reference_context,
        )
        prompt = rewrite_dialogue_delivery(prompt, bundle.dialogue_prompt)
        return append_segment_direction(prompt, segment_id=segment_id)

    base.build_happyhorse_prompt = directed_build_prompt
    continuity.append_continuity_prompt = append_directed_continuity_prompt
    try:
        yield
    finally:
        base.build_happyhorse_prompt = original_build_prompt
        continuity.append_continuity_prompt = original_continuity_prompt


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        segment_id = _option_value(arguments, "--segment-id")
        if not segment_id:
            raise DirectedPromptError("--segment-id is required")
        with install_directed_prompt(segment_id):
            return continuity.main(arguments)
    except DirectedPromptError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
