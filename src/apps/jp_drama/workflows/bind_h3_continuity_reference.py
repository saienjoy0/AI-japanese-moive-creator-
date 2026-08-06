"""Bind PR45's SHA-bound segment-end frame to the MiniMax H3 handoff.

This module never extracts a frame and never calls a provider. It consumes the
portable continuity artifact already produced by PR45, verifies that it belongs
to the reused E01-G01 video, and writes the reference contract for E01-G02.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from ..rendering.ffmpeg import file_sha256
from .render_happyhorse_segment_continuity_canary import (
    ContinuityFrameError,
    load_continuity_frame,
)


EXIT_OK = 0
EXIT_INPUT = 1
SCHEMA_VERSION = "jp-drama-h3-continuity-reference/v1"
SOURCE_RUN_ID = "31091499445"
SOURCE_ARTIFACT = "jp-drama-E01-G01-continuity-frame"
SOURCE_SEGMENT_ID = "E01-G01"
TARGET_SEGMENT_ID = "E01-G02"
TARGET_ROUTE_ID = "minimax/h3-reference-av"
REFERENCE_ROLE = "storyboard"


class H3ContinuityBindingError(RuntimeError):
    """The PR45 continuity artifact cannot be bound to the H3 handoff safely."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate PR45's E01-G01 end-frame artifact and bind it to E01-G02 "
            "as an H3 reference image. Provider calls: zero."
        )
    )
    parser.add_argument("--handoff", required=True, type=Path)
    parser.add_argument("--segment-artifact", required=True, type=Path)
    parser.add_argument("--continuity-frame", required=True, type=Path)
    parser.add_argument("--continuity-frame-metadata", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_object(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise H3ContinuityBindingError(f"required JSON is missing or empty: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise H3ContinuityBindingError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise H3ContinuityBindingError(f"JSON must be an object: {path}")
    return payload


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _atomic_write(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    path = path.resolve()
    if path.exists() and not overwrite:
        raise H3ContinuityBindingError(f"refusing to overwrite output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _copy(source: Path, destination: Path, *, overwrite: bool) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise H3ContinuityBindingError(f"continuity file is missing or empty: {source}")
    if destination.exists() and not overwrite:
        raise H3ContinuityBindingError(f"refusing to overwrite output: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def bind(
    *,
    handoff_path: Path,
    segment_artifact_path: Path,
    frame_path: Path,
    metadata_path: Path,
    output_dir: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    handoff = _load_object(handoff_path)
    segment_artifact = _load_object(segment_artifact_path)

    if handoff.get("reused_segment_id") != SOURCE_SEGMENT_ID:
        raise H3ContinuityBindingError("handoff does not reuse E01-G01")
    remaining = handoff.get("remaining_segment_ids")
    if not isinstance(remaining, list) or TARGET_SEGMENT_ID not in remaining:
        raise H3ContinuityBindingError("E01-G02 is not a remaining H3 segment")
    if handoff.get("target_provider_route_id") != TARGET_ROUTE_ID:
        raise H3ContinuityBindingError("handoff is not using H3 reference mode")
    if int(handoff.get("external_api_calls", -1)) != 0:
        raise H3ContinuityBindingError("handoff does not prove zero provider calls")

    if segment_artifact.get("segment_id") != SOURCE_SEGMENT_ID:
        raise H3ContinuityBindingError("segment artifact is not E01-G01")
    source_video_sha = str(segment_artifact.get("output_sha256") or "")
    if not source_video_sha.startswith("sha256:"):
        raise H3ContinuityBindingError("segment artifact has no output SHA")

    try:
        continuity = load_continuity_frame(
            frame_path,
            metadata_path,
            target_segment_id=TARGET_SEGMENT_ID,
        )
    except ContinuityFrameError as exc:
        raise H3ContinuityBindingError(str(exc)) from exc
    if continuity.source_segment_id != SOURCE_SEGMENT_ID:
        raise H3ContinuityBindingError("continuity frame is not derived from E01-G01")
    if continuity.source_video_sha256 != source_video_sha:
        raise H3ContinuityBindingError(
            "PR45 continuity frame belongs to a different E01-G01 video"
        )

    output_dir = output_dir.resolve()
    continuity_dir = output_dir / "continuity"
    copied_frame = continuity_dir / continuity.path.name
    copied_metadata = continuity_dir / continuity.metadata_path.name
    _copy(continuity.path, copied_frame, overwrite=overwrite)
    _copy(continuity.metadata_path, copied_metadata, overwrite=overwrite)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_handoff_path": str(handoff_path.resolve()),
        "source_handoff_sha256": file_sha256(handoff_path.resolve()),
        "source_workflow_run_id": SOURCE_RUN_ID,
        "source_artifact": SOURCE_ARTIFACT,
        "source_segment_id": SOURCE_SEGMENT_ID,
        "target_segment_id": TARGET_SEGMENT_ID,
        "target_provider_route_id": TARGET_ROUTE_ID,
        "reference_role": REFERENCE_ROLE,
        "frame_path": str(copied_frame),
        "frame_sha256": continuity.frame_sha256,
        "metadata_path": str(copied_metadata),
        "metadata_sha256": continuity.metadata_sha256,
        "source_video_sha256": continuity.source_video_sha256,
        "width": continuity.width,
        "height": continuity.height,
        "offset_from_end_seconds": continuity.offset_from_end_seconds,
        "send_full_previous_video": False,
        "external_api_calls": 0,
    }
    payload["content_digest"] = _digest(payload)
    output_path = output_dir / f"{TARGET_SEGMENT_ID}.h3-continuity-reference.json"
    _atomic_write(output_path, payload, overwrite=overwrite)
    return {
        "valid": True,
        "binding": str(output_path),
        "content_digest": payload["content_digest"],
        "frame_sha256": continuity.frame_sha256,
        "source_workflow_run_id": SOURCE_RUN_ID,
        "source_artifact": SOURCE_ARTIFACT,
        "source_segment_id": SOURCE_SEGMENT_ID,
        "target_segment_id": TARGET_SEGMENT_ID,
        "target_provider_route_id": TARGET_ROUTE_ID,
        "send_full_previous_video": False,
        "external_api_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = bind(
            handoff_path=args.handoff,
            segment_artifact_path=args.segment_artifact,
            frame_path=args.continuity_frame,
            metadata_path=args.continuity_frame_metadata,
            output_dir=args.output_dir,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, H3ContinuityBindingError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    if args.print_report:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(report["binding"])
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
