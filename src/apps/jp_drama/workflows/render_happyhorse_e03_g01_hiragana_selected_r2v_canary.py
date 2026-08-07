"""Render E03-G01 through the proven HappyHorse selected-reference R2V route.

E03 starts the next morning at S04, so it intentionally does not bind the
E02-G05 terminal frame. The authoritative storyboard text stays unchanged;
only provider-facing speech is rewritten to explicit hiragana readings.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base


class E03G01SelectedR2VError(RuntimeError):
    """E03-G01 cannot safely use the reviewed selected-reference route."""


_EXPECTED_SUBJECTS = ("C01", "S04", "P07")
_EXPECTED_DIALOGUE = (
    "僕(inner_monologue): みんな、僕を泥棒だと言うだろう | "
    "先生(memory_voice): あなたの顔を見ないと、私は悲しいわ"
)
_C01_HIRAGANA = "みんな、ぼくをどろぼうだというだろう"
_C03_HIRAGANA = "あなたのかおをみないと、わたしはかなしいわ"


def build_e03_g01_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E03-G01":
        raise E03G01SelectedR2VError("selected-reference manifest is pinned to E03-G01")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E03G01SelectedR2VError(
            "E03-G01 master order changed; refusing unreviewed payload: "
            + ",".join(subjects)
        )
    return manifest


def rewrite_e03_g01_provider_prompt(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E03G01SelectedR2VError(
            f"E03-G01 dialogue changed or is missing: {dialogue_prompt!r}"
        )
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise E03G01SelectedR2VError(
            "could not locate generated E03-G01 spoken-dialogue instruction"
        )
    new_line = (
        "Provider-facing voice delivery for E03-G01: treat both lines as non-lip-synced "
        "voice-over. 0.5-4.7s C01 inner monologue says exactly "
        f"{_C01_HIRAGANA}. 4.7-9.5s C03 memory voice says exactly {_C03_HIRAGANA}. "
        "Use these hiragana readings literally for Japanese speech. C01 remains the only "
        "visible person and keeps the mouth closed for both lines. C03 is heard only as a "
        "memory voice and must not appear in the image. Do not add other words or vocalizations."
    )
    rewritten = prompt.replace(old_line, new_line, 1)
    for forbidden in (
        "みんな、僕を泥棒だと言うだろう",
        "あなたの顔を見ないと、私は悲しいわ",
    ):
        if forbidden in rewritten:
            raise E03G01SelectedR2VError(
                "kanji dialogue leaked into the final provider-facing prompt"
            )
    return rewritten


def e03_g01_direction() -> str:
    return (
        "Directed shot progression for E03-G01: this is the next morning and a deliberate "
        "new-scene cut from E02. Do not reproduce the S03 teacher room or the prior grape. "
        "Use Image 1 only for C01 identity and period costume, Image 2 for the exact S04 "
        "school-gate architecture and autumn morning atmosphere, and Image 3 for the exact "
        "P07 brown leather school satchel. 0.0-2.0s medium-wide: C01 approaches the outside "
        "of S04 with P07 naturally carried, then stops before crossing the gate. 2.0-4.7s "
        "slow push-in: C01 looks toward the school, then slightly down, visibly ashamed and "
        "afraid while the inner voice plays; mouth remains closed. 4.7-7.5s hold on C01 as "
        "C03's remembered voice is heard; C03 must not appear. C01 grips the P07 strap a "
        "little tighter but does not walk away. 7.5-10.0s C01 raises the eyes toward the "
        "gate and remains outside it, undecided but no longer turning away. Final state: "
        "C01 stands outside S04 facing the gate, P07 remains one brown leather satchel, and "
        "the emotion is unresolved conflict ready for E03-G02. No other people, no cars, "
        "no modern roads, no modern signs, no readable text, subtitles, logos, or watermarks."
    )


@contextmanager
def install_e03_g01_policy() -> Iterator[None]:
    original_manifest = base.build_wan_master_reference_manifest
    original_prompt_builder = base.build_happyhorse_reference_prompt

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e03_g01_manifest(prepared, plan, bundle, segment_id=segment_id)

    def prompt_builder(bundle, manifest):
        prompt = original_prompt_builder(bundle, manifest)
        prompt = rewrite_e03_g01_provider_prompt(prompt, bundle.dialogue_prompt)
        return prompt + "\n" + e03_g01_direction()

    base.build_wan_master_reference_manifest = selected_builder
    base.build_happyhorse_reference_prompt = prompt_builder
    try:
        yield
    finally:
        base.build_wan_master_reference_manifest = original_manifest
        base.build_happyhorse_reference_prompt = original_prompt_builder


def _option_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise E03G01SelectedR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E03-G01":
            raise E03G01SelectedR2VError("this wrapper is pinned to E03-G01")
        if _option_value(arguments, "--input-mode") != "references":
            raise E03G01SelectedR2VError(
                "E03-G01 selected-reference route requires --input-mode references"
            )
        with install_e03_g01_policy():
            return base.main(arguments)
    except E03G01SelectedR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
