"""Render E03-G03 through the proven HappyHorse selected-reference R2V route.

This reuses the successful E03-G02 same-scene continuity path and changes only
what E03-G03 requires: C02 is added as a master reference, the exact E03-G02
terminal frame is bound as continuity, and the two provider-facing spoken lines
use explicit hiragana readings. The authoritative storyboard text is unchanged.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class E03G03SelectedR2VError(RuntimeError):
    """E03-G03 cannot safely use the reviewed selected-reference route."""


_EXPECTED_SUBJECTS = ("C01", "C02", "S04", "P07")
_EXPECTED_DIALOGUE = (
    "ジム(spoken): 来てくれたんだね | "
    "ジム(spoken): 先生が待ってる。行こう"
)
_LINE1_HIRAGANA = "きてくれたんだね"
_LINE2_HIRAGANA = "せんせいがまってる。いこう"


def build_e03_g03_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E03-G03":
        raise E03G03SelectedR2VError("selected-reference manifest is pinned to E03-G03")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E03G03SelectedR2VError(
            "E03-G03 master order changed; refusing unreviewed payload: "
            + ",".join(subjects)
        )
    return manifest


def rewrite_e03_g03_dialogue(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E03G03SelectedR2VError(
            f"E03-G03 dialogue changed or is missing: {dialogue_prompt!r}"
        )
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise E03G03SelectedR2VError(
            "could not locate generated E03-G03 spoken-dialogue instruction"
        )
    new_line = (
        "Provider-facing spoken dialogue for E03-G03: 0.5-4.3s C02 says exactly "
        f"{_LINE1_HIRAGANA}. 4.3-9.5s C02 says exactly {_LINE2_HIRAGANA}. "
        "Use these hiragana readings literally for Japanese speech. C02 is the only "
        "visible speaker and lip-syncs these two lines naturally. C01 does not speak "
        "and keeps the mouth closed. Do not add any other words or vocalizations."
    )
    rewritten = prompt.replace(old_line, new_line, 1)
    for forbidden in ("来てくれたんだね", "先生が待ってる。行こう"):
        if forbidden in rewritten:
            raise E03G03SelectedR2VError(
                "kanji dialogue leaked into the final provider-facing prompt"
            )
    return rewritten


def append_e03_g03_continuity_prompt(prompt: str, *, existing_reference_count: int) -> str:
    if existing_reference_count != 4:
        raise E03G03SelectedR2VError(
            f"E03-G03 requires four masters before continuity; got {existing_reference_count}"
        )
    return (
        prompt
        + "\n"
        + "[Image 5] is the exact final frame of E03-G02 and is the primary same-scene "
        + "continuity reference. Preserve C01 already inside S04 facing the school, the "
        + "same autumn morning light, camera axis, costume, and exactly one P07 brown "
        + "leather satchel on C01. Continue directly without a scene cut. C02 is not in "
        + "Image 5 and must enter naturally from the deeper school/campus side; do not "
        + "teleport C02 beside C01 at the first frame. Image 5 is the opening state only."
    )


def e03_g03_direction() -> str:
    return (
        "Directed shot progression for E03-G03: 0.0-0.7s continue exactly from the "
        "E03-G02 terminal frame: C01 is already inside S04 facing the school building, "
        "wearing the same costume and carrying exactly one P07 brown leather satchel. "
        "0.7-2.2s C02 enters from the deeper school/campus side at a brief childlike jog, "
        "then clearly slows down before reaching C01; keep the run short and natural, "
        "not athletic or heroic. C02 begins the first line きてくれたんだね and finishes "
        "it after slowing near C01. 2.2-4.3s C02 is stopped beside C01, warmly finishing "
        "the first line; there is still no hand contact. C01 reacts with restrained surprise. "
        "4.3-6.2s C02 says せんせいがまってる。いこう and only after stopping extends one "
        "hand toward C01. Avoid a close-up of fingers. C01 accepts that hand once, gently, "
        "with anatomically normal hands and no duplicated arms. 6.2-9.5s while C02 finishes "
        "the second line, the two children keep one light handhold and turn together toward "
        "the school building; C02 guides rather than pulls. They may take only one or two "
        "small steps. 9.5-10.0s settle in a stable full shot with both children facing the "
        "school, lightly holding one hand, ready to continue toward the teacher. Final state: "
        "C01 and C02 are together inside S04, hand contact is clear but natural, P07 remains "
        "exactly one unchanged satchel on C01, and both face the school. Do not reach S03 or "
        "the teacher's room in this segment. Do not show C03. No extra students, no running "
        "while grabbing hands, no dragging, no embrace, no triumphant pose, no duplicated "
        "hands or props, no cars, modern roads, power lines, modern signs, readable text, "
        "subtitles, logos, or watermarks."
    )


@contextmanager
def install_e03_g03_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_continuity = directed.append_directed_continuity_prompt
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e03_g03_manifest(prepared, plan, bundle, segment_id=segment_id)

    def segment_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id == "E03-G03":
            rewritten += "\n" + e03_g03_direction()
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_directed_continuity_prompt = append_e03_g03_continuity_prompt
    directed.append_segment_direction = segment_direction
    directed.rewrite_dialogue_delivery = rewrite_e03_g03_dialogue
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
        raise E03G03SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E03-G03":
            raise E03G03SelectedR2VError("this wrapper is pinned to E03-G03")
        if _option_value(arguments, "--input-mode") != "references":
            raise E03G03SelectedR2VError(
                "E03-G03 selected-reference route requires --input-mode references"
            )
        with install_e03_g03_policy():
            return directed.main(arguments)
    except E03G03SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
