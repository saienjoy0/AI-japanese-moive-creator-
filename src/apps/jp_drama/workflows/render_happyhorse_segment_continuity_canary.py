"""Run HappyHorse R2V with automatic prior-frame handoff and end-frame capture."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..rendering.ffmpeg import file_sha256
from ..rendering.happyhorse11 import require_local_first_frame
from . import render_happyhorse_segment_canary as base
from .extract_segment_end_frame import SCHEMA_VERSION, extract_segment_end_frame
from .segment_id import previous_segment_id


@dataclass(frozen=True)
class ContinuityFrame:
    path: Path
    metadata_path: Path
    source_segment_id: str
    frame_sha256: str
    metadata_sha256: str
    source_video_sha256: str
    width: int
    height: int
    offset_from_end_seconds: float

    @property
    def asset_id(self) -> str:
        return f"continuity_end_{self.source_segment_id}"


class ContinuityFrameError(RuntimeError):
    """A continuity frame is missing, changed, or incompatible with R2V."""


def build_continuity_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--continuity-frame")
    parser.add_argument("--continuity-frame-metadata")
    parser.add_argument(
        "--continuity-dir",
        help=(
            "Shared directory containing <segment>/<segment>_end.png and JSON. "
            "Defaults to a continuity directory beside --output."
        ),
    )
    return parser


def _option_value(arguments: list[str], option: str, default: str | None = None) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return default
    if index + 1 >= len(arguments):
        raise ContinuityFrameError(f"{option} requires a value")
    return arguments[index + 1]


def _default_continuity_dir(arguments: list[str]) -> Path | None:
    output = _option_value(arguments, "--output")
    if not output:
        return None
    return Path(output).resolve().parent / "continuity"


def _continuity_candidates(
    continuity_dir: str | Path,
    source_segment_id: str,
) -> list[tuple[Path, Path]]:
    root = Path(continuity_dir).resolve()
    filename = f"{source_segment_id}_end"
    return [
        (
            root / source_segment_id / f"{filename}.png",
            root / source_segment_id / f"{filename}.json",
        ),
        (root / f"{filename}.png", root / f"{filename}.json"),
    ]


def resolve_previous_continuity(
    continuity_dir: str | Path,
    *,
    target_segment_id: str,
) -> tuple[Path, Path] | None:
    """Find the immediately prior segment's SHA-bound continuity pair."""

    source_segment_id = previous_segment_id(target_segment_id)
    if source_segment_id is None:
        return None
    for frame, metadata in _continuity_candidates(continuity_dir, source_segment_id):
        frame_exists = frame.is_file() and frame.stat().st_size > 0
        metadata_exists = metadata.is_file() and metadata.stat().st_size > 0
        if frame_exists and metadata_exists:
            return frame, metadata
        if frame_exists != metadata_exists:
            raise ContinuityFrameError(
                f"incomplete continuity pair for {source_segment_id}: "
                f"frame={frame_exists}, metadata={metadata_exists}"
            )
    return None


def load_continuity_frame(
    frame_path: str | Path,
    metadata_path: str | Path,
    *,
    target_segment_id: str,
) -> ContinuityFrame:
    frame = require_local_first_frame(frame_path)
    metadata_file = Path(metadata_path).resolve()
    if not metadata_file.is_file() or metadata_file.stat().st_size == 0:
        raise ContinuityFrameError(
            f"continuity metadata is missing or empty: {metadata_file}"
        )
    try:
        payload = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuityFrameError(f"invalid continuity metadata: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContinuityFrameError("continuity metadata must be a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ContinuityFrameError("unsupported continuity metadata schema")

    source_segment_id = str(payload.get("source_segment_id", "")).strip()
    if not source_segment_id:
        raise ContinuityFrameError("continuity metadata has no source_segment_id")
    if source_segment_id == target_segment_id:
        raise ContinuityFrameError("continuity frame must come from a prior segment")
    expected_source = previous_segment_id(target_segment_id)
    if expected_source is not None and source_segment_id != expected_source:
        raise ContinuityFrameError(
            f"continuity frame must come from {expected_source}, not {source_segment_id}"
        )
    derived_for = str(payload.get("derived_for_next_segment_id") or "").strip()
    if derived_for and derived_for != target_segment_id:
        raise ContinuityFrameError(
            f"continuity metadata targets {derived_for}, not {target_segment_id}"
        )
    if payload.get("frame_file") != frame.name:
        raise ContinuityFrameError("continuity frame filename does not match metadata")

    expected_sha = str(payload.get("frame_sha256", ""))
    actual_sha = file_sha256(frame)
    if expected_sha != actual_sha:
        raise ContinuityFrameError("continuity frame SHA changed after extraction")

    source_video_sha = str(payload.get("source_video_sha256", ""))
    if not source_video_sha.startswith("sha256:"):
        raise ContinuityFrameError("continuity metadata has no source video SHA")

    width = int(payload.get("width") or 0)
    height = int(payload.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ContinuityFrameError("continuity frame dimensions are invalid")

    return ContinuityFrame(
        path=frame,
        metadata_path=metadata_file,
        source_segment_id=source_segment_id,
        frame_sha256=actual_sha,
        metadata_sha256=file_sha256(metadata_file),
        source_video_sha256=source_video_sha,
        width=width,
        height=height,
        offset_from_end_seconds=float(payload.get("offset_from_end_seconds") or 0.0),
    )


def append_continuity_prompt(
    prompt: str,
    *,
    existing_reference_count: int,
) -> str:
    reference_number = existing_reference_count + 1
    if reference_number > base.HappyHorse11R2VModel.MAX_REFERENCE_IMAGES:
        raise base.HappyHorseCanaryError(
            "HappyHorse R2V master references plus continuity frame exceed 9 images"
        )
    return (
        prompt
        + "\n"
        + f"[Image {reference_number}] is the derived final frame of the previous segment. "
        + "Preserve its pose, framing, screen direction, lighting, and spatial continuity."
    )


@contextmanager
def install_continuity_bridge(frame: ContinuityFrame) -> Iterator[None]:
    original_model = base.HappyHorse11R2VModel
    original_prompt = base.build_happyhorse_reference_prompt
    original_fingerprint = base._request_fingerprint
    original_report = base._base_report

    class ContinuityR2VModel(original_model):
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
            continuity_path = str(frame.path)
            if continuity_path in {str(item) for item in references}:
                raise ValueError("continuity frame duplicates an existing reference")
            references.append(continuity_path)
            if len(references) > self.MAX_REFERENCE_IMAGES:
                raise ValueError(
                    "HappyHorse R2V master references plus continuity frame exceed 9 images"
                )
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

    def continuity_prompt(bundle, manifest):
        return append_continuity_prompt(
            original_prompt(bundle, manifest),
            existing_reference_count=len(manifest.references),
        )

    def continuity_fingerprint(**kwargs):
        asset_ids = list(kwargs["ordered_asset_ids"])
        asset_hashes = list(kwargs["ordered_asset_hashes"])
        if len(asset_ids) + 1 > original_model.MAX_REFERENCE_IMAGES:
            raise base.HappyHorseCanaryError(
                "HappyHorse R2V master references plus continuity frame exceed 9 images"
            )
        kwargs["ordered_asset_ids"] = [*asset_ids, frame.asset_id]
        kwargs["ordered_asset_hashes"] = [*asset_hashes, frame.frame_sha256]
        return original_fingerprint(**kwargs)

    def continuity_report(**kwargs):
        payload = original_report(**kwargs)
        payload["continuity_frame"] = {
            "asset_id": frame.asset_id,
            "source_segment_id": frame.source_segment_id,
            "path": str(frame.path),
            "sha256": frame.frame_sha256,
            "metadata_path": str(frame.metadata_path),
            "metadata_sha256": frame.metadata_sha256,
            "source_video_sha256": frame.source_video_sha256,
            "width": frame.width,
            "height": frame.height,
            "offset_from_end_seconds": frame.offset_from_end_seconds,
        }
        if "references" in payload:
            payload["references"].append(
                {
                    "order": len(payload["references"]),
                    "asset_id": frame.asset_id,
                    "subject_id": frame.source_segment_id,
                    "role": "continuity_frame",
                    "sha256": frame.frame_sha256,
                    "width": frame.width,
                    "height": frame.height,
                }
            )
        return payload

    base.HappyHorse11R2VModel = ContinuityR2VModel
    base.build_happyhorse_reference_prompt = continuity_prompt
    base._request_fingerprint = continuity_fingerprint
    base._base_report = continuity_report
    try:
        yield
    finally:
        base.HappyHorse11R2VModel = original_model
        base.build_happyhorse_reference_prompt = original_prompt
        base._request_fingerprint = original_fingerprint
        base._base_report = original_report


def _run_base(arguments: list[str], frame: ContinuityFrame | None) -> int:
    if frame is None:
        return base.main(arguments)
    with install_continuity_bridge(frame):
        return base.main(arguments)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    continuity_args, remaining = build_continuity_parser().parse_known_args(arguments)
    supplied = bool(continuity_args.continuity_frame)
    supplied_metadata = bool(continuity_args.continuity_frame_metadata)
    if supplied != supplied_metadata:
        print(
            "input error: --continuity-frame and --continuity-frame-metadata "
            "must be supplied together",
            file=sys.stderr,
        )
        return base.EXIT_INPUT

    try:
        input_mode = _option_value(remaining, "--input-mode", "first_frame")
        target_segment_id = _option_value(remaining, "--segment-id")
        output_value = _option_value(remaining, "--output")
        stage = _option_value(remaining, "--stage", "preflight")
        if not target_segment_id:
            raise ContinuityFrameError("--segment-id is required")
        if not output_value:
            raise ContinuityFrameError("--output is required")

        continuity_dir = (
            Path(continuity_args.continuity_dir).resolve()
            if continuity_args.continuity_dir
            else _default_continuity_dir(remaining)
        )
        if continuity_dir is None:
            raise ContinuityFrameError("could not resolve continuity directory")

        frame: ContinuityFrame | None = None
        if supplied:
            if input_mode != "references":
                raise ContinuityFrameError(
                    "continuity frame is supported only with --input-mode references"
                )
            frame = load_continuity_frame(
                continuity_args.continuity_frame,
                continuity_args.continuity_frame_metadata,
                target_segment_id=target_segment_id,
            )
        elif input_mode == "references":
            resolved = resolve_previous_continuity(
                continuity_dir,
                target_segment_id=target_segment_id,
            )
            if resolved is not None:
                frame = load_continuity_frame(
                    resolved[0],
                    resolved[1],
                    target_segment_id=target_segment_id,
                )
    except (OSError, ValueError, ContinuityFrameError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT

    result = _run_base(remaining, frame)
    if result != base.EXIT_OK or stage != "render":
        return result

    try:
        derived = extract_segment_end_frame(
            segment_id=target_segment_id,
            video=output_value,
            output_dir=continuity_dir / target_segment_id,
            offset_seconds=0.10,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            f"provider error: generated video was preserved but continuity extraction failed: {exc}",
            file=sys.stderr,
        )
        return base.EXIT_PROVIDER

    print(
        "Continuity frame: GENERATED\n"
        f"Source segment: {target_segment_id}\n"
        f"Next segment: {derived['derived_for_next_segment_id']}\n"
        f"Frame: {derived['frame_path']}\n"
        f"Metadata: {derived['metadata_path']}"
    )
    return base.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
