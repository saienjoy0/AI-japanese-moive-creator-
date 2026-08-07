"""Render E01-G04 through the proven HappyHorse R2V continuity route.

This wrapper is intentionally pinned to the reviewed G4 scene cut. It validates
the full approved G4 master set, then sends only the four visually useful
masters selected for this shot (C01, C02, S02, P01). The verified E01-G03 end
frame is appended by the existing continuity bridge as Image 5 and is used only
for C01 character/state continuity, never as the G4 opening composition.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class G4SelectedR2VError(RuntimeError):
    """E01-G04 cannot safely use the selected-reference R2V route."""


_FULL_G4_SUBJECTS = ("C01", "C02", "C90", "C91", "S02", "P01", "P02")
_SELECTED_G4_SUBJECTS = ("C01", "C02", "S02", "P01")


def build_g4_selected_manifest(prepared, plan, bundle, *, segment_id: str):
    """Validate the full planned master set, then return the reviewed four refs."""

    if segment_id != "E01-G04":
        raise G4SelectedR2VError("selected-reference manifest is pinned to E01-G04")

    original = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in original.references)
    if subjects != _FULL_G4_SUBJECTS:
        raise G4SelectedR2VError(
            "E01-G04 planned master order changed; refusing unreviewed reference selection: "
            + ",".join(subjects)
        )

    by_subject = {item.subject_id: item for item in original.references}
    selected = [
        by_subject[subject].model_copy(update={"order": order})
        for order, subject in enumerate(_SELECTED_G4_SUBJECTS)
    ]
    return wan_refs.WanMasterReferenceManifest.build_with_digest(
        generation_plan_digest=original.generation_plan_digest,
        master_asset_set_digest=wan_refs._master_asset_set_digest(selected),
        segment_id=original.segment_id,
        provider_route_id=original.provider_route_id,
        references=selected,
    )


def append_g4_scene_cut_reference_prompt(
    prompt: str,
    *,
    existing_reference_count: int,
) -> str:
    """Use G3 end-frame only for C01 identity/state continuity."""

    if existing_reference_count != 4:
        raise G4SelectedR2VError(
            f"E01-G04 requires exactly four selected masters before continuity; got {existing_reference_count}"
        )
    reference_number = existing_reference_count + 1
    if reference_number > base.HappyHorse11R2VModel.MAX_REFERENCE_IMAGES:
        raise base.HappyHorseCanaryError(
            "HappyHorse R2V master references plus continuity frame exceed 9 images"
        )
    return (
        prompt
        + "\n"
        + f"[Image {reference_number}] is the exact final frame of E01-G03. "
        + "Use Image 5 only as a character-and-state continuity reference for C01 after "
        + "the theft: preserve C01's face, hair, costume, body proportions, tense emotional "
        + "carry-over, and the story fact that the two missing P02 paints remain hidden in "
        + "C01's anatomical right coat pocket. Do not copy the classroom, desk, paint-box "
        + "close-up, framing, or camera composition from Image 5. Do not show P02. "
        + "E01-G04 must begin as a hard cut in the S02 school corridor defined by Image 3."
    )


def append_g4_scene_cut_direction(prompt: str, *, segment_id: str) -> str:
    """Keep the reviewed G4 timing while removing obsolete first-frame semantics."""

    rewritten = directed.append_segment_direction(prompt, segment_id=segment_id)
    if segment_id != "E01-G04":
        return rewritten

    old = "0.0-1.0s begin exactly from the approved E01-G04 first frame:"
    new = (
        "0.0-1.0s begin directly in the S02 school corridor. Use Image 1 for C01, "
        "Image 2 for C02, Image 3 for the corridor, and Image 4 for P01:"
    )
    if old not in rewritten:
        raise G4SelectedR2VError(
            "could not replace obsolete E01-G04 first-frame direction"
        )
    rewritten = rewritten.replace(old, new, 1)
    rewritten += (
        " Background classmates C90 and C91 are generic soft-focus extras in this shot; "
        "do not attempt to reproduce their master-image identities. P02 has no reference "
        "image in this request because both paints must remain completely off-screen."
    )
    return rewritten


@contextmanager
def install_g4_selected_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_g4_selected_manifest(
            prepared,
            plan,
            bundle,
            segment_id=segment_id,
        )

    def scene_cut_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id != "E01-G04":
            return rewritten
        old = "0.0-1.0s begin exactly from the approved E01-G04 first frame:"
        new = (
            "0.0-1.0s begin directly in the S02 school corridor. Use Image 1 for C01, "
            "Image 2 for C02, Image 3 for the corridor, and Image 4 for P01:"
        )
        if old not in rewritten:
            raise G4SelectedR2VError(
                "could not replace obsolete E01-G04 first-frame direction"
            )
        rewritten = rewritten.replace(old, new, 1)
        rewritten += (
            " Background classmates C90 and C91 are generic soft-focus extras in this shot; "
            "do not attempt to reproduce their master-image identities. P02 has no reference "
            "image in this request because both paints must remain completely off-screen."
        )
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_directed_continuity_prompt = append_g4_scene_cut_reference_prompt
    directed.append_segment_direction = scene_cut_direction
    try:
        yield
    finally:
        base.build_wan_master_reference_manifest = original_builder
        directed.append_directed_continuity_prompt = original_continuity
        directed.append_segment_direction = original_direction


def _option_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise G4SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        segment_id = _option_value(arguments, "--segment-id")
        input_mode = _option_value(arguments, "--input-mode")
        if segment_id != "E01-G04":
            raise G4SelectedR2VError("this wrapper is pinned to E01-G04")
        if input_mode != "references":
            raise G4SelectedR2VError(
                "E01-G04 selected-reference route requires --input-mode references"
            )
        with install_g4_selected_policy():
            return directed.main(arguments)
    except G4SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
