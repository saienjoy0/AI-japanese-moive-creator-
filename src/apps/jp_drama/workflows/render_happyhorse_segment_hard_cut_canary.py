"""Render a hard-cut R2V segment with an approved composition frame and prior state.

E01-G04 changes location from the classroom to the corridor. The immediately
previous end frame must therefore constrain state and identity without becoming
the opening composition. This wrapper adds two SHA-bound references after the
normal approved master-reference manifest:

* the approved E01-G04 composition frame, used as the exact opening anchor;
* the E01-G03 end frame, used only for prior-state continuity.

The existing HappyHorse ledger, paid-call cap, provider task persistence, output
validation, and end-frame extraction remain unchanged.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..rendering.approval import ApprovedKeyframeManifest, load_and_verify_approval
from ..rendering.ffmpeg import file_sha256
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_continuity_canary as continuity
from .extract_segment_end_frame import extract_segment_end_frame


TARGET_SEGMENT_ID = "E01-G04"
COMPOSITION_ASSET_ID = "ref_first_E01-G04"
PRIOR_STATE_ASSET_ID = "continuity_state_E01-G03"

EXPECTED_MASTER_ASSET_IDS = (
    "ref_char_C01_aaf13357f6",
    "ref_char_C02_aaf13357f6",
    "ref_char_C90_aaf13357f6",
    "ref_char_C91_aaf13357f6",
    "ref_loc_S02_aaf13357f6",
    "ref_prop_P01_aaf13357f6",
    "ref_prop_P02_aaf13357f6",
)
EXPECTED_MASTER_ASSET_HASHES = (
    "sha256:ad79e1aa803ac3ae059b3f70f1293fe6c3e01c0fb178a93991193f601628455c",
    "sha256:74beff4a8a9a8e86000324d64ce3328460812c7e4df1548859650983c458e9c7",
    "sha256:d2fb350ba579d46c22002ce635c2b8756e61e92027b2e3f5624cfc8a76e6fe6d",
    "sha256:50f7e262db04aeb6d63b044a2f6ff60463666a7bbde6bb526b68f3ad7ff9d68e",
    "sha256:68fae03059f001c43aadcf024b0b6efe29af1bec6240f10d4ba586b6a438e9e4",
    "sha256:aa674d0aaffd7df81d30acf4b45ffa3ade373a5a805fe193786cdc1874fe52eb",
    "sha256:6eabbf5c85fcb0aea786d0200f1340d3e8bd4d3b6ff8b64a0a4ec60d89c46e20",
)


class HardCutCanaryError(RuntimeError):
    """The hard-cut composition or prior-state contract is invalid."""


@dataclass(frozen=True)
class CompositionFrame:
    path: Path
    approval_path: Path
    manifest: ApprovedKeyframeManifest
    approval_sha256: str

    @property
    def frame_sha256(self) -> str:
        return self.manifest.asset_sha256


def build_hard_cut_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--composition-frame", required=True)
    parser.add_argument("--composition-frame-approval", required=True)
    parser.add_argument("--prior-state-frame", required=True)
    parser.add_argument("--prior-state-metadata", required=True)
    parser.add_argument("--continuity-dir")
    return parser


def _option_value(arguments: list[str], option: str, default: str | None = None) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return default
    if index + 1 >= len(arguments):
        raise HardCutCanaryError(f"{option} requires a value")
    return arguments[index + 1]


def load_composition_frame(
    frame_path: str | Path,
    approval_path: str | Path,
    *,
    target_segment_id: str,
) -> CompositionFrame:
    manifest, verified_path = load_and_verify_approval(
        approval_path,
        expected_shot_id=target_segment_id,
    )
    supplied = Path(frame_path).resolve()
    if supplied != verified_path:
        raise HardCutCanaryError(
            f"composition frame {supplied} differs from approved asset {verified_path}"
        )
    return CompositionFrame(
        path=supplied,
        approval_path=Path(approval_path).resolve(),
        manifest=manifest,
        approval_sha256=file_sha256(approval_path),
    )


def append_hard_cut_prompt(
    prompt: str,
    *,
    existing_reference_count: int,
    segment_id: str,
) -> str:
    if segment_id != TARGET_SEGMENT_ID:
        raise HardCutCanaryError(
            f"hard-cut policy is pinned to {TARGET_SEGMENT_ID}, not {segment_id}"
        )
    composition_number = existing_reference_count + 1
    prior_state_number = existing_reference_count + 2
    if prior_state_number > base.HappyHorse11R2VModel.MAX_REFERENCE_IMAGES:
        raise base.HappyHorseCanaryError(
            "master references plus composition and prior-state frames exceed 9 images"
        )
    return (
        prompt
        + "\n"
        + f"[Image {composition_number}] is the exact approved opening composition "
        + "for E01-G04. Begin directly in this wooden school corridor as a clean "
        + "hard cut. Preserve its character positions, camera height, framing, "
        + "lighting, screen direction, crowd pressure, and the closed teacher-room "
        + "door behind C01. Do not morph from the classroom. "
        + f"[Image {prior_state_number}] is the exact final frame of E01-G03, but it "
        + "is a state-only continuity reference. Use it only for C01's clothing, "
        + "satchel, P01 identity, and the fact that the theft has just occurred. "
        + "Never copy its desk, classroom, hand pose, or framing into E01-G04.\n"
        + "Directed hard-cut progression for E01-G04: "
        + "0.0-0.5s hold the approved corridor composition. C02 stands screen-left "
        + "holding open P01 toward C01. Exactly two paint wells are empty: indigo "
        + "and magenta. All other ten paint cakes remain. P02 itself is hidden inside "
        + "C01's anatomical right coat pocket and is never visible. "
        + "0.5-2.2s C02 calmly says in Japanese: 藍と洋紅がない。 "
        + "2.2-6.8s C02 continues: 休みに教室にいたのは、君だけだ。 Shift focus "
        + "or minimally reframe toward C01's reaction while C02 remains in profile "
        + "or continues as off-axis speech. C01 lowers his gaze and breathes shallowly. "
        + "His hands remain visible, empty, and outside both pockets. C90 and C91 stay "
        + "silent and secondary. "
        + "6.8-7.4s hold a short silence; C01 swallows once and retreats half a step "
        + "toward the closed teacher-room door. "
        + "7.4-8.6s C01 quietly says in Japanese: 僕じゃない. Use restrained, short "
        + "lip movement. "
        + "8.6-10.0s hold the lie and isolation. C01 remains screen-right beside the "
        + "closed door, C02 remains screen-left holding P01, and C91 remains behind C02. "
        + "P02 stays hidden in C01's anatomical right pocket and does not fall. "
        + "Do not show a teacher, do not open the door, do not add another speaker, "
        + "and do not change the number, ownership, or state of any prop."
    )


@contextmanager
def install_hard_cut_bridge(
    composition: CompositionFrame,
    prior_state: continuity.ContinuityFrame,
    *,
    segment_id: str,
) -> Iterator[None]:
    original_model = base.HappyHorse11R2VModel
    original_prompt = base.build_happyhorse_reference_prompt
    original_fingerprint = base._request_fingerprint
    original_report = base._base_report

    class HardCutR2VModel(original_model):
        MODEL_NAME = original_model.MODEL_NAME

        def generate(
            self,
            prompt: str,
            output_path: str,
            img_path: str | None = None,
            model_name: str | None = None,
            **kwargs: Any,
        ):
            references = list(
                kwargs.get("reference_image_paths")
                or kwargs.get("reference_image_urls")
                or kwargs.get("ref_image_urls")
                or []
            )
            additions = [str(composition.path), str(prior_state.path)]
            if len(set(references + additions)) != len(references) + len(additions):
                raise ValueError("hard-cut references contain a duplicate image path")
            references.extend(additions)
            if len(references) > self.MAX_REFERENCE_IMAGES:
                raise ValueError("hard-cut R2V request exceeds nine reference images")
            kwargs["reference_image_paths"] = references
            kwargs.pop("reference_image_urls", None)
            kwargs.pop("ref_image_urls", None)
            return super().generate(
                prompt,
                output_path,
                img_path=img_path,
                model_name=model_name,
                **kwargs,
            )

    def hard_cut_prompt(bundle, manifest):
        if manifest.asset_ids != list(EXPECTED_MASTER_ASSET_IDS):
            raise HardCutCanaryError(
                "E01-G04 master-reference IDs changed from the approved seven-image set"
            )
        if manifest.asset_hashes != list(EXPECTED_MASTER_ASSET_HASHES):
            raise HardCutCanaryError(
                "E01-G04 master-reference hashes changed from the approved set"
            )
        return append_hard_cut_prompt(
            original_prompt(bundle, manifest),
            existing_reference_count=len(manifest.references),
            segment_id=segment_id,
        )

    def hard_cut_fingerprint(**kwargs):
        asset_ids = list(kwargs["ordered_asset_ids"])
        asset_hashes = list(kwargs["ordered_asset_hashes"])
        if len(asset_ids) + 2 > original_model.MAX_REFERENCE_IMAGES:
            raise base.HappyHorseCanaryError(
                "hard-cut R2V fingerprint exceeds nine reference images"
            )
        kwargs["ordered_asset_ids"] = [
            *asset_ids,
            COMPOSITION_ASSET_ID,
            PRIOR_STATE_ASSET_ID,
        ]
        kwargs["ordered_asset_hashes"] = [
            *asset_hashes,
            composition.frame_sha256,
            prior_state.frame_sha256,
        ]
        return original_fingerprint(**kwargs)

    def hard_cut_report(**kwargs):
        payload = original_report(**kwargs)
        payload["continuity_mode"] = "hard_cut_state"
        payload["composition_frame"] = {
            "asset_id": COMPOSITION_ASSET_ID,
            "path": str(composition.path),
            "sha256": composition.frame_sha256,
            "approval_path": str(composition.approval_path),
            "approval_sha256": composition.approval_sha256,
            "width": composition.manifest.width,
            "height": composition.manifest.height,
            "generated_by": composition.manifest.generated_by,
            "operation_id": composition.manifest.operation_id,
        }
        payload["prior_state_frame"] = {
            "asset_id": PRIOR_STATE_ASSET_ID,
            "source_segment_id": prior_state.source_segment_id,
            "path": str(prior_state.path),
            "sha256": prior_state.frame_sha256,
            "metadata_path": str(prior_state.metadata_path),
            "metadata_sha256": prior_state.metadata_sha256,
            "source_video_sha256": prior_state.source_video_sha256,
            "width": prior_state.width,
            "height": prior_state.height,
        }
        if "references" in payload:
            payload["references"].extend(
                [
                    {
                        "order": len(payload["references"]),
                        "asset_id": COMPOSITION_ASSET_ID,
                        "subject_id": segment_id,
                        "role": "composition_frame",
                        "sha256": composition.frame_sha256,
                        "width": composition.manifest.width,
                        "height": composition.manifest.height,
                    },
                    {
                        "order": len(payload["references"]) + 1,
                        "asset_id": PRIOR_STATE_ASSET_ID,
                        "subject_id": prior_state.source_segment_id,
                        "role": "prior_state_frame",
                        "sha256": prior_state.frame_sha256,
                        "width": prior_state.width,
                        "height": prior_state.height,
                    },
                ]
            )
        return payload

    base.HappyHorse11R2VModel = HardCutR2VModel
    base.build_happyhorse_reference_prompt = hard_cut_prompt
    base._request_fingerprint = hard_cut_fingerprint
    base._base_report = hard_cut_report
    try:
        yield
    finally:
        base.HappyHorse11R2VModel = original_model
        base.build_happyhorse_reference_prompt = original_prompt
        base._request_fingerprint = original_fingerprint
        base._base_report = original_report


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    hard_cut_args, remaining = build_hard_cut_parser().parse_known_args(arguments)
    try:
        segment_id = _option_value(remaining, "--segment-id")
        output_value = _option_value(remaining, "--output")
        input_mode = _option_value(remaining, "--input-mode", "first_frame")
        stage = _option_value(remaining, "--stage", "preflight")
        if segment_id != TARGET_SEGMENT_ID:
            raise HardCutCanaryError(
                f"hard-cut runner accepts only {TARGET_SEGMENT_ID}"
            )
        if input_mode != "references":
            raise HardCutCanaryError("hard-cut runner requires --input-mode references")
        if not output_value:
            raise HardCutCanaryError("--output is required")

        composition = load_composition_frame(
            hard_cut_args.composition_frame,
            hard_cut_args.composition_frame_approval,
            target_segment_id=segment_id,
        )
        prior_state = continuity.load_continuity_frame(
            hard_cut_args.prior_state_frame,
            hard_cut_args.prior_state_metadata,
            target_segment_id=segment_id,
        )
        if composition.frame_sha256 == prior_state.frame_sha256:
            raise HardCutCanaryError(
                "composition frame and prior-state frame must be different"
            )
    except (OSError, ValueError, HardCutCanaryError, continuity.ContinuityFrameError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT

    try:
        with install_hard_cut_bridge(
            composition,
            prior_state,
            segment_id=segment_id,
        ):
            result = base.main(remaining)
    except HardCutCanaryError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT

    if result != base.EXIT_OK or stage != "render":
        return result

    output = Path(output_value).resolve()
    continuity_dir = (
        Path(hard_cut_args.continuity_dir).resolve()
        if hard_cut_args.continuity_dir
        else output.parent / "continuity"
    )
    try:
        derived = extract_segment_end_frame(
            segment_id=segment_id,
            video=output,
            output_dir=continuity_dir / segment_id,
            offset_seconds=0.10,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            "provider error: generated video was preserved but continuity extraction "
            f"failed: {exc}",
            file=sys.stderr,
        )
        return base.EXIT_PROVIDER

    print(
        "Continuity frame: GENERATED\n"
        f"Source segment: {segment_id}\n"
        f"Next segment: {derived['derived_for_next_segment_id']}\n"
        f"Frame: {derived['frame_path']}\n"
        f"Metadata: {derived['metadata_path']}"
    )
    return base.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
