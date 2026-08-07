"""Render E01-G05 through the proven HappyHorse R2V continuity route.

This wrapper is pinned to the reviewed G5 continuation. It validates the full
approved G5 master set, then sends only the four visually useful masters for
this shot (C02, C03, S02, P02). The verified E01-G04 end frame is appended by
the existing continuity bridge as Image 5 and supplies the exact current C01
appearance and opening corridor state.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class G5SelectedR2VError(RuntimeError):
    """E01-G05 cannot safely use the selected-reference R2V route."""


_FULL_G5_SUBJECTS = ("C01", "C02", "C03", "C90", "C91", "S02", "P02")
_SELECTED_G5_SUBJECTS = ("C02", "C03", "S02", "P02")
_EXPECTED_DIALOGUE = "先生(off_screen_then_on_screen): お入り"


def build_g5_selected_manifest(prepared, plan, bundle, *, segment_id: str):
    """Validate the full planned master set, then return the reviewed four refs."""

    if segment_id != "E01-G05":
        raise G5SelectedR2VError("selected-reference manifest is pinned to E01-G05")

    original = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in original.references)
    if subjects != _FULL_G5_SUBJECTS:
        raise G5SelectedR2VError(
            "E01-G05 planned master order changed; refusing unreviewed reference selection: "
            + ",".join(subjects)
        )

    by_subject = {item.subject_id: item for item in original.references}
    selected = [
        by_subject[subject].model_copy(update={"order": order})
        for order, subject in enumerate(_SELECTED_G5_SUBJECTS)
    ]
    return wan_refs.WanMasterReferenceManifest.build_with_digest(
        generation_plan_digest=original.generation_plan_digest,
        master_asset_set_digest=wan_refs._master_asset_set_digest(selected),
        segment_id=original.segment_id,
        provider_route_id=original.provider_route_id,
        references=selected,
    )


def rewrite_g5_dialogue_delivery(prompt: str, dialogue_prompt: str | None) -> str:
    """Make the teacher reveal line unambiguous and keep every other mouth closed."""

    if dialogue_prompt != _EXPECTED_DIALOGUE:
        if dialogue_prompt:
            raise G5SelectedR2VError(
                f"E01-G05 dialogue changed; refusing unreviewed delivery: {dialogue_prompt}"
            )
        raise G5SelectedR2VError("E01-G05 dialogue is missing")

    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise G5SelectedR2VError(
            "could not locate the generated E01-G05 spoken-dialogue instruction"
        )
    new_line = (
        "Teacher dialogue: C03 says exactly お入り once as the teacher-room door opens "
        "and C03 becomes visible. If the audio begins a fraction before C03 is fully "
        "visible, no other character may move the mouth. Once C03 is visible, only C03 "
        "may articulate this line; C01, C02, and every background student keep their "
        "mouths closed. Do not add any other spoken words."
    )
    return prompt.replace(old_line, new_line, 1)


def append_g5_continuity_prompt(
    prompt: str,
    *,
    existing_reference_count: int,
) -> str:
    """Use the G4 end frame as the exact opening C01/corridor continuity state."""

    if existing_reference_count != 4:
        raise G5SelectedR2VError(
            f"E01-G05 requires exactly four selected masters before continuity; got {existing_reference_count}"
        )
    reference_number = existing_reference_count + 1
    if reference_number > base.HappyHorse11R2VModel.MAX_REFERENCE_IMAGES:
        raise base.HappyHorseCanaryError(
            "HappyHorse R2V master references plus continuity frame exceed 9 images"
        )
    return (
        prompt
        + "\n"
        + f"[Image {reference_number}] is the exact final frame of E01-G04. "
        + "Use Image 5 as the opening continuity state for C01 and the S02 corridor: "
        + "preserve C01's exact face, hair, costume, body proportions, tense expression, "
        + "camera side, lighting, and corridor geometry at the start. Image 1 is C02, "
        + "Image 2 is C03 the teacher, Image 3 is the S02 corridor, and Image 4 is exactly "
        + "two P02 solid paint cakes, one indigo and one magenta. Do not freeze Image 5; "
        + "the shot must evolve into the planned paint-drop and teacher-door reveal."
    )


def append_g5_direction(prompt: str, *, segment_id: str) -> str:
    """Add the reviewed timing and end-state contract for E01-G05."""

    rewritten = directed.append_segment_direction(prompt, segment_id=segment_id)
    if segment_id != "E01-G05":
        return rewritten
    return rewritten + "\n" + (
        "Directed shot progression for E01-G05: "
        "0.0-1.2s begin from Image 5, the exact final frame of E01-G04: C01 remains tense "
        "in the same S02 corridor with no identity, costume, lighting, or location jump. "
        "C02 may remain nearby as a secondary character using Image 1; C90 and C91 are "
        "generic soft-focus background students only and need no master-image identity. "
        "1.2-3.8s use one controlled tilt or downward reframe toward C01's anatomical right "
        "coat pocket and the wooden floor. Exactly two P02 solid paint cakes from Image 4, "
        "one indigo and one magenta, slip out of C01's anatomical right coat pocket together "
        "and fall naturally to the floor. They must visibly originate from C01's pocket; "
        "no hand places them, no extra paint appears, and neither paint changes color or form. "
        "3.8-5.5s hold long enough to make exactly two P02 clearly visible on the floor while "
        "C01 realizes he has been exposed. C02 may react silently; nobody touches or picks up "
        "either paint. 5.5-7.2s the teacher-room door in the same S02 corridor opens naturally "
        "and C03 enters the doorway using Image 2, without teleporting or changing the corridor. "
        "7.2-8.5s C03 says exactly: お入り. Only C03 moves the mouth for this one line; C01, "
        "C02, and all background students keep their mouths closed. Use a calm, restrained "
        "teacher delivery, not anger or shouting. 8.5-10.0s hold the consequence: C01 is "
        "visibly isolated, exactly two P02 remain on the floor, and C03 remains in the doorway. "
        "Final state must be exactly two P02 on the floor and C03 at the doorway. Do not return "
        "to the classroom, do not show P01, do not put P02 back in a pocket or box, do not let "
        "anyone pick them up, and do not add text, subtitles, logos, or extra dialogue."
    )


@contextmanager
def install_g5_selected_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_g5_selected_manifest(
            prepared,
            plan,
            bundle,
            segment_id=segment_id,
        )

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_directed_continuity_prompt = append_g5_continuity_prompt
    directed.append_segment_direction = append_g5_direction
    directed.rewrite_dialogue_delivery = rewrite_g5_dialogue_delivery
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
        raise G5SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        segment_id = _option_value(arguments, "--segment-id")
        input_mode = _option_value(arguments, "--input-mode")
        if segment_id != "E01-G05":
            raise G5SelectedR2VError("this wrapper is pinned to E01-G05")
        if input_mode != "references":
            raise G5SelectedR2VError(
                "E01-G05 selected-reference route requires --input-mode references"
            )
        with install_g5_selected_policy():
            return directed.main(arguments)
    except G5SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
