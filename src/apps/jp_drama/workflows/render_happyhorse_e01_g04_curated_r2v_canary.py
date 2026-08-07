"""Render E01-G04 through HappyHorse R2V with a curated reference set.

The reviewed E01-G04 shot is a hard scene cut from the classroom to S02.  The
provider receives only the four masters needed for the visible shot (C01, C02,
S02, P01) plus the SHA-verified E01-G03 terminal frame as a fifth identity/state
reference.  P02, C90, and C91 are deliberately not sent as reference images.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


CURATED_SUBJECT_IDS = ("C01", "C02", "S02", "P01")


class G4CuratedReferenceError(RuntimeError):
    """E01-G04 cannot safely use the curated R2V request."""


_G4_DIRECTION = (
    "Directed shot progression for E01-G04: This is a hard scene cut from the "
    "classroom in E01-G03 into the S02 school corridor. Do not begin from, "
    "recreate, dissolve from, or visually morph the E01-G03 final frame. "
    "0.0-1.0s begin in S02 with C01 and C02 facing each other in the corridor. "
    "C02 holds the open wooden P01 so the two missing paint spaces are clear. "
    "P02 is not visible because both paint cakes remain fully hidden inside "
    "C01's anatomical right coat pocket. C01's hands are empty. Two same-age "
    "classmates may remain secondary in soft focus in the background; their "
    "identity is not a reference target. "
    "1.0-6.5s C02 says exactly: 藍と洋紅がない。休みに教室にいたのは、君だけだ. "
    "Only C02 moves the mouth for this line; C01 and all background classmates "
    "keep their mouths closed. Use restrained concern rather than shouting or "
    "villainous anger. 6.5-8.0s hold a short silent reaction on C01 becoming more "
    "cornered, without revealing the pocket contents. 8.0-10.0s C01 says exactly: "
    "僕じゃない. Only C01 moves the mouth for this reply; C02 and all background "
    "classmates keep their mouths closed. Preserve the approved C01 and C02 faces, "
    "ages and costumes, the S02 corridor geometry, P01 identity, and the exact "
    "state of two hidden P02. Do not return to the classroom, repeat the theft, "
    "show P02 in P01, in a hand, or on the floor, introduce the teacher, drop the "
    "paints, or add text."
)


def _option_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise G4CuratedReferenceError(f"{option} requires a value")
    return arguments[index + 1]


def append_g4_direction(prompt: str, *, segment_id: str) -> str:
    if segment_id != "E01-G04":
        raise G4CuratedReferenceError("this prompt policy is pinned to E01-G04")
    return prompt + "\n" + _G4_DIRECTION


def append_g4_continuity_reference(
    prompt: str,
    *,
    existing_reference_count: int,
) -> str:
    reference_number = existing_reference_count + 1
    if existing_reference_count != 4 or reference_number != 5:
        raise G4CuratedReferenceError(
            "E01-G04 must have exactly four curated masters before continuity"
        )
    if reference_number > base.HappyHorse11R2VModel.MAX_REFERENCE_IMAGES:
        raise base.HappyHorseCanaryError(
            "HappyHorse R2V master references plus continuity frame exceed 9 images"
        )
    return (
        prompt
        + "\n"
        + "[Image 5] is the exact final frame of E01-G03. Use it only to preserve "
        + "C01's face, hair, costume, body proportions, emotional carry-over, and "
        + "post-theft state. It is not an opening-frame instruction. Do not copy its "
        + "classroom, desk, framing, paint-box close-up, lighting layout, or camera "
        + "composition. Start E01-G04 in the S02 corridor defined by [Image 3]."
    )


def _curated_manifest_builder(original_builder):
    def build(prepared, plan, bundle, *, segment_id: str):
        full = original_builder(
            prepared,
            plan,
            bundle,
            segment_id=segment_id,
        )
        if segment_id != "E01-G04":
            raise G4CuratedReferenceError("curated manifest is pinned to E01-G04")
        by_subject = {item.subject_id: item for item in full.references}
        missing = [subject for subject in CURATED_SUBJECT_IDS if subject not in by_subject]
        if missing:
            raise G4CuratedReferenceError(
                "G4 curated reference subjects missing: " + ", ".join(missing)
            )
        curated = [
            by_subject[subject].model_copy(update={"order": order})
            for order, subject in enumerate(CURATED_SUBJECT_IDS)
        ]
        if any(item.subject_id in {"P02", "C90", "C91"} for item in curated):
            raise G4CuratedReferenceError("forbidden G4 reference leaked into curated set")
        return wan_refs.WanMasterReferenceManifest.build_with_digest(
            generation_plan_digest=full.generation_plan_digest,
            master_asset_set_digest=wan_refs._master_asset_set_digest(curated),
            segment_id=full.segment_id,
            provider_route_id=full.provider_route_id,
            references=curated,
        )

    return build


@contextmanager
def install_g4_curated_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_direction = directed.append_segment_direction
    original_continuity = directed.append_directed_continuity_prompt
    base.build_wan_master_reference_manifest = _curated_manifest_builder(original_builder)
    directed.append_segment_direction = append_g4_direction
    directed.append_directed_continuity_prompt = append_g4_continuity_reference
    try:
        yield
    finally:
        base.build_wan_master_reference_manifest = original_builder
        directed.append_segment_direction = original_direction
        directed.append_directed_continuity_prompt = original_continuity


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        segment_id = _option_value(arguments, "--segment-id")
        input_mode = _option_value(arguments, "--input-mode")
        if segment_id != "E01-G04":
            raise G4CuratedReferenceError("this wrapper is pinned to E01-G04")
        if input_mode != "references":
            raise G4CuratedReferenceError(
                "E01-G04 curated wrapper requires --input-mode references"
            )
        with install_g4_curated_policy():
            return directed.main(arguments)
    except G4CuratedReferenceError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
