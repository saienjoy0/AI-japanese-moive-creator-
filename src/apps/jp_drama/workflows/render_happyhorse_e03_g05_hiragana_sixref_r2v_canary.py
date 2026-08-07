"""Render E03-G05 through the proven HappyHorse selected-reference R2V route.

E03-G05 is the final segment. The authoritative storyboard stays unchanged. We
use only the approved C01/C02/C03/S03/P05/P06 masters: the E03-G04 terminal
frame is intentionally not sent because it contains a stray P07-like shoulder
strap and G05 starts a new prop-focused composition. Provider-facing speech is
converted to explicit hiragana while preserving C03 spoken delivery and C01
voice-over delivery.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class E03G05SixRefR2VError(RuntimeError):
    """E03-G05 cannot safely use the reviewed six-reference route."""


_EXPECTED_SUBJECTS = ("C01", "C02", "C03", "S03", "P05", "P06")
_EXPECTED_DIALOGUE = (
    "先生(spoken): これは、二人に | "
    "僕(voice_over): 僕はその時から、前より少しだけいい子になり、少しだけ、はにかみ屋でなくなった"
)
_LINE1_HIRAGANA = "これは、ふたりに"
_LINE2_HIRAGANA = "ぼくはそのときから、まえよりすこしだけいいこになり、すこしだけ、はにかみやでなくなった"


def build_e03_g05_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E03-G05":
        raise E03G05SixRefR2VError("selected-reference manifest is pinned to E03-G05")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E03G05SixRefR2VError(
            "E03-G05 master order changed; refusing unreviewed payload: " + ",".join(subjects)
        )
    return manifest


def rewrite_e03_g05_dialogue(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E03G05SixRefR2VError(f"E03-G05 dialogue changed or is missing: {dialogue_prompt!r}")
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: " + dialogue_prompt
    )
    if old_line not in prompt:
        raise E03G05SixRefR2VError("could not locate generated E03-G05 mixed-dialogue instruction")
    new_line = (
        "Provider-facing Japanese audio for E03-G05: 0.5-1.92s C03 says exactly "
        f"{_LINE1_HIRAGANA}. Only C03 lip-syncs this short spoken line; C01 and C02 keep "
        "their mouths closed. 1.92-9.5s use C01 internal voice-over with exactly these words: "
        f"{_LINE2_HIRAGANA}. C01 does not speak aloud during the voice-over; keep C01's mouth "
        "closed and relaxed with no lip synchronization or visible articulation. C02 and C03 "
        "also remain silent after 1.92s. Do not add any other words, vocalizations, or narration."
    )
    rewritten = prompt.replace(old_line, new_line, 1)
    for forbidden in (
        "これは、二人に",
        "僕はその時から、前より少しだけいい子になり、少しだけ、はにかみ屋でなくなった",
    ):
        if forbidden in rewritten:
            raise E03G05SixRefR2VError("kanji dialogue leaked into the final provider-facing prompt")
    return rewritten


def e03_g05_direction() -> str:
    return (
        "Directed shot progression for E03-G05: remain inside the approved S03 teacher's room, "
        "but use a fresh prop-focused medium eye-level mostly static composition. Do not use, "
        "recreate, or visually inherit the E03-G04 terminal frame or its stray satchel-like "
        "shoulder strap. No P07 or school bag is visible anywhere in this final segment. Start "
        "with exactly C01, C02, C03, one fresh intact P05 grape bunch, and one small silver P06 "
        "scissors. [Image 5] is the P05 species/style master only: this shot must depict a "
        "distinct next-day in-story bunch, not the same physical bunch used in E02. 0.0-0.5s "
        "establish the fresh single P05 bunch on a small wooden table between the three characters, "
        "with P06 beside it or calmly in C03's hand. 0.5-1.92s C03 says これは、ふたりに while "
        "indicating the fresh bunch; only C03 moves the mouth and the boys remain mostly still. "
        "1.92-3.4s after the voice-over begins, C03 performs exactly one controlled scissor action "
        "at the central stem of P05 using P06. Keep this in a medium view, not a finger or blade "
        "close-up. The single intact bunch becomes exactly two similarly sized smaller grape "
        "clusters; do not create a third cluster, loose duplicate grapes, or a second intact bunch. "
        "3.4-4.0s C03 sets P06 down on the table and does not use or hold the scissors again. "
        "4.0-5.2s C03 gives one small grape cluster to C01 in one clean handoff. 5.2-6.4s C03 gives "
        "the other small grape cluster to C02 in a second clean handoff. Do not cross arms, swap "
        "the two boys, or hand both clusters to one child. 6.4-9.5s hold a stable warm medium "
        "composition while C01's internal voice-over continues: C01 and C02 each hold exactly one "
        "similarly sized small grape cluster at a natural waist or chest level, C03 watches gently, "
        "and P06 remains lying on the table. C01 may show only a small shy relieved smile near the "
        "end. 9.5-10.0s settle into the final state: exactly one small grape cluster in C01's "
        "possession, exactly one in C02's possession, no intact full bunch remains, no extra grapes "
        "appear, and all three stay in S03. Preserve faces, ages, costumes, and period setting. "
        "No satchel, no extra people, no repeated cutting, no weapon-like scissors, no blood, no "
        "hugging, no celebratory pose, no modern furniture or devices, no readable text, subtitles, "
        "logos, or watermarks."
    )


@contextmanager
def install_e03_g05_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e03_g05_manifest(prepared, plan, bundle, segment_id=segment_id)

    def segment_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id == "E03-G05":
            rewritten += "\n" + e03_g05_direction()
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_segment_direction = segment_direction
    directed.rewrite_dialogue_delivery = rewrite_e03_g05_dialogue
    try:
        yield
    finally:
        base.build_wan_master_reference_manifest = original_builder
        directed.append_segment_direction = original_direction
        directed.rewrite_dialogue_delivery = original_dialogue


def _option_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise E03G05SixRefR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E03-G05":
            raise E03G05SixRefR2VError("this wrapper is pinned to E03-G05")
        if _option_value(arguments, "--input-mode") != "references":
            raise E03G05SixRefR2VError("E03-G05 six-reference route requires --input-mode references")
        with install_e03_g05_policy():
            return directed.main(arguments)
    except E03G05SixRefR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
