"""E01-G04 HappyHorse R2V scene-cut wrapper.

Reuses the proven directed-continuity R2V route while treating the verified
E01-G03 end frame as a character/state reference only. E01-G04 is a hard scene
cut into S02, so the prior classroom composition must not become the opening
frame.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class G4SceneCutError(RuntimeError):
    """E01-G04 cannot safely use the scene-cut R2V wrapper."""


def append_g4_scene_cut_reference_prompt(
    prompt: str,
    *,
    existing_reference_count: int,
) -> str:
    """Use the G3 terminal frame for identity/state, never as G4's opening shot."""

    reference_number = existing_reference_count + 1
    if reference_number > base.HappyHorse11R2VModel.MAX_REFERENCE_IMAGES:
        raise base.HappyHorseCanaryError(
            "HappyHorse R2V master references plus continuity frame exceed 9 images"
        )
    return (
        prompt
        + "\n"
        + f"[Image {reference_number}] is the exact final frame of E01-G03. "
        + "Use it only as a character-and-state continuity reference for C01 after "
        + "the theft. Preserve C01's face, hair, costume, body proportions, emotional "
        + "carry-over, and the fact that both P02 remain fully hidden inside C01's "
        + "anatomical right coat pocket. Do not begin from, recreate, dissolve from, "
        + "or copy the classroom composition, desk, or paint-box close-up from this "
        + "image. E01-G04 must start in the approved S02 school corridor using the "
        + "approved C01, C02, C90, C91, S02, P01, and P02 reference identities."
    )


def append_g4_scene_cut_direction(prompt: str, *, segment_id: str) -> str:
    """Keep the reviewed G4 timing while removing the obsolete first-frame claim."""

    rewritten = directed.append_segment_direction(prompt, segment_id=segment_id)
    if segment_id != "E01-G04":
        return rewritten
    old = "0.0-1.0s begin exactly from the approved E01-G04 first frame:"
    new = (
        "0.0-1.0s begin directly in the approved S02 school corridor using "
        "Images 1-7 as the identity, location, and prop references:"
    )
    if old not in rewritten:
        raise G4SceneCutError("could not replace obsolete E01-G04 first-frame direction")
    return rewritten.replace(old, new, 1)


@contextmanager
def install_g4_scene_cut_reference_policy() -> Iterator[None]:
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction

    def scene_cut_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id != "E01-G04":
            return rewritten
        old = "0.0-1.0s begin exactly from the approved E01-G04 first frame:"
        new = (
            "0.0-1.0s begin directly in the approved S02 school corridor using "
            "Images 1-7 as the identity, location, and prop references:"
        )
        if old not in rewritten:
            raise G4SceneCutError(
                "could not replace obsolete E01-G04 first-frame direction"
            )
        return rewritten.replace(old, new, 1)

    directed.append_directed_continuity_prompt = append_g4_scene_cut_reference_prompt
    directed.append_segment_direction = scene_cut_direction
    try:
        yield
    finally:
        directed.append_directed_continuity_prompt = original_continuity
        directed.append_segment_direction = original_direction


def _option_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise G4SceneCutError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        segment_id = _option_value(arguments, "--segment-id")
        input_mode = _option_value(arguments, "--input-mode")
        if segment_id != "E01-G04":
            raise G4SceneCutError("this wrapper is pinned to E01-G04")
        if input_mode != "references":
            raise G4SceneCutError(
                "E01-G04 scene-cut wrapper requires --input-mode references"
            )
        with install_g4_scene_cut_reference_policy():
            return directed.main(arguments)
    except G4SceneCutError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
