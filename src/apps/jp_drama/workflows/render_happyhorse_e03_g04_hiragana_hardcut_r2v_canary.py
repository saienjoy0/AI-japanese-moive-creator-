"""Render E03-G04 through the proven HappyHorse selected-reference R2V route.

E03-G04 is an intentional hard cut from S04 to S03. It therefore uses only the
approved C01/C02/C03/S03 master references and explicitly refuses the previous
segment terminal frame as a provider reference. The authoritative storyboard
text stays unchanged; only provider-facing spoken readings are converted to
hiragana and the reviewed three-character handshake staging is pinned.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from ..assets import wan_references as wan_refs
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_directed_continuity_canary as directed


class E03G04HardCutR2VError(RuntimeError):
    """E03-G04 cannot safely use the reviewed hard-cut selected-reference route."""


_EXPECTED_SUBJECTS = ("C01", "C02", "C03", "S03")
_EXPECTED_DIALOGUE = (
    "先生(spoken): よく来ましたね | "
    "先生(spoken): 二人は、これからいい友達になればいいの | "
    "ジム(spoken): もう大丈夫。友達になろう"
)
_LINE1_HIRAGANA = "よくきましたね"
_LINE2_HIRAGANA = "ふたりは、これからいいともだちになればいいの"
_LINE3_HIRAGANA = "もうだいじょうぶ。ともだちになろう"


def build_e03_g04_manifest(prepared, plan, bundle, *, segment_id: str):
    if segment_id != "E03-G04":
        raise E03G04HardCutR2VError("selected-reference manifest is pinned to E03-G04")
    manifest = wan_refs.build_wan_master_reference_manifest(
        prepared,
        plan,
        bundle,
        segment_id=segment_id,
    )
    subjects = tuple(item.subject_id for item in manifest.references)
    if subjects != _EXPECTED_SUBJECTS:
        raise E03G04HardCutR2VError(
            "E03-G04 master order changed; refusing unreviewed payload: "
            + ",".join(subjects)
        )
    return manifest


def rewrite_e03_g04_dialogue(prompt: str, dialogue_prompt: str | None) -> str:
    if dialogue_prompt != _EXPECTED_DIALOGUE:
        raise E03G04HardCutR2VError(
            f"E03-G04 dialogue changed or is missing: {dialogue_prompt!r}"
        )
    old_line = (
        "Spoken dialogue: Use natural Japanese and synchronize the visible "
        "speaker's mouth to this dialogue exactly: "
        f"{dialogue_prompt}"
    )
    if old_line not in prompt:
        raise E03G04HardCutR2VError(
            "could not locate generated E03-G04 spoken-dialogue instruction"
        )
    new_line = (
        "Provider-facing spoken dialogue for E03-G04: 0.5-2.17s C03 says exactly "
        f"{_LINE1_HIRAGANA}. 2.17-6.67s C03 says exactly {_LINE2_HIRAGANA}. "
        f"6.67-9.5s C02 says exactly {_LINE3_HIRAGANA}. Use these hiragana readings "
        "literally for Japanese speech. During the first two lines only C03 lip-syncs; "
        "C01 and C02 keep their mouths closed. During the third line only C02 lip-syncs; "
        "C01 and C03 keep their mouths closed. Do not add any other words or vocalizations."
    )
    rewritten = prompt.replace(old_line, new_line, 1)
    for forbidden in (
        "よく来ましたね",
        "二人は、これからいい友達になればいいの",
        "もう大丈夫。友達になろう",
    ):
        if forbidden in rewritten:
            raise E03G04HardCutR2VError(
                "kanji dialogue leaked into the final provider-facing prompt"
            )
    return rewritten


def e03_g04_direction() -> str:
    return (
        "Directed shot progression for E03-G04: this is a deliberate hard cut from the "
        "outdoor S04 school approach in E03-G03 into the approved S03 teacher's room. "
        "Do not recreate, dissolve from, visually morph from, or use the E03-G03 terminal "
        "frame. Start directly inside S03 with exactly C01, C02, and C03 in a restrained "
        "medium eye-level mostly static composition. No props are required. 0.0-0.5s establish "
        "C03 facing the two boys with calm warmth while C01 and C02 stand side by side. "
        "0.5-2.17s C03 says よくきましたね; only C03 moves the mouth. 2.17-6.67s C03 continues "
        "ふたりは、これからいいともだちになればいいの; keep the two boys mostly still, "
        "listening, with C01 reserved and C02 receptive. 6.67-8.6s C02 turns slightly toward "
        "C01 and says most of もうだいじょうぶ。ともだちになろう while beginning a slow, "
        "single open-hand offer. Do not touch yet and do not show a finger close-up. 8.6-9.5s "
        "C02 finishes the line with one hand clearly offered; C01 begins to reach toward it, "
        "while C03 stays quietly in the background. 9.5-10.0s after the line is finished, C01 "
        "accepts the offered hand once for one gentle handshake with normal anatomy, no pumping, "
        "no arm duplication, and no lingering hand close-up. C01 gives only a small restrained "
        "smile. Final state: C01 and C02 have completed one handshake, C01 shows a slight smile, "
        "and all three remain inside S03. Do not hug, high-five, bow dramatically, cry, cheer, "
        "or turn the moment heroic. Do not introduce grapes, scissors, satchels, extra students, "
        "modern furniture, electrical devices, readable text, subtitles, logos, or watermarks."
    )


@contextmanager
def install_e03_g04_policy() -> Iterator[None]:
    original_builder = base.build_wan_master_reference_manifest
    original_direction = directed.append_segment_direction
    original_dialogue = directed.rewrite_dialogue_delivery

    def selected_builder(prepared, plan, bundle, *, segment_id: str):
        return build_e03_g04_manifest(prepared, plan, bundle, segment_id=segment_id)

    def segment_direction(prompt: str, *, segment_id: str) -> str:
        rewritten = original_direction(prompt, segment_id=segment_id)
        if segment_id == "E03-G04":
            rewritten += "\n" + e03_g04_direction()
        return rewritten

    base.build_wan_master_reference_manifest = selected_builder
    directed.append_segment_direction = segment_direction
    directed.rewrite_dialogue_delivery = rewrite_e03_g04_dialogue
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
        raise E03G04HardCutR2VError(f"{option} requires a value")
    return arguments[index + 1]


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if _option_value(arguments, "--segment-id") != "E03-G04":
            raise E03G04HardCutR2VError("this wrapper is pinned to E03-G04")
        if _option_value(arguments, "--input-mode") != "references":
            raise E03G04HardCutR2VError(
                "E03-G04 hard-cut route requires --input-mode references"
            )
        with install_e03_g04_policy():
            return directed.main(arguments)
    except E03G04HardCutR2VError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
