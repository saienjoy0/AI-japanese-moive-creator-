"""Extract one portable continuity frame from a completed segment video."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..rendering.approval import ApprovalError, png_dimensions
from ..rendering.ffmpeg import file_sha256
from .segment_id import next_segment_id


EXIT_OK = 0
EXIT_INPUT = 1
SCHEMA_VERSION = "jp-drama-continuity-frame/v1"
TAIL_FALLBACK_SECONDS = (0.0, 0.03, 0.05, 0.10, 0.25, 0.50)


class SegmentEndFrameError(RuntimeError):
    """A completed segment cannot produce a safe continuity frame."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract the frame immediately before a segment video ends and write "
            "portable SHA-bound metadata for the next segment."
        )
    )
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--offset-seconds", type=float, default=0.10)
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _safe_segment_id(value: str) -> str:
    segment_id = value.strip()
    if not segment_id:
        raise SegmentEndFrameError("segment-id must not be empty")
    if any(token in segment_id for token in ("/", "\\", "..")):
        raise SegmentEndFrameError("segment-id must not contain path traversal")
    # Validate the ordered generation-segment format while preserving the input text.
    next_segment_id(segment_id)
    return segment_id


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _extract_with_tail_fallback(
    *,
    source: Path,
    frame: Path,
    requested_offset_seconds: float,
    ffmpeg_bin: str,
) -> float:
    """Try the requested tail offset, then move slightly earlier if undecodable."""

    last_message = "ffmpeg produced no continuity PNG"
    tried: set[float] = set()
    for extra_seconds in TAIL_FALLBACK_SECONDS:
        actual_offset = round(min(2.0, requested_offset_seconds + extra_seconds), 3)
        if actual_offset in tried:
            continue
        tried.add(actual_offset)
        frame.unlink(missing_ok=True)
        command = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-sseof",
            f"-{actual_offset:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            str(frame),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise SegmentEndFrameError(f"failed to execute ffmpeg: {exc}") from exc

        if completed.returncode != 0:
            last_message = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or "unknown ffmpeg error"
            )
            continue
        if frame.is_file() and frame.stat().st_size > 0:
            return actual_offset
        last_message = f"ffmpeg produced no PNG at tail offset {actual_offset:.3f}s"

    raise SegmentEndFrameError(f"ffmpeg continuity extraction failed: {last_message}")


def extract_segment_end_frame(
    *,
    segment_id: str,
    video: str | Path,
    output_dir: str | Path,
    offset_seconds: float = 0.10,
    ffmpeg_bin: str = "ffmpeg",
) -> dict[str, Any]:
    safe_segment_id = _safe_segment_id(segment_id)
    if not 0.01 <= offset_seconds <= 2.0:
        raise SegmentEndFrameError("offset-seconds must be between 0.01 and 2.0")

    source = Path(video).resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise SegmentEndFrameError(f"source video is missing or empty: {source}")

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    frame = destination / f"{safe_segment_id}_end.png"
    metadata = destination / f"{safe_segment_id}_end.json"

    actual_offset_seconds = _extract_with_tail_fallback(
        source=source,
        frame=frame,
        requested_offset_seconds=offset_seconds,
        ffmpeg_bin=ffmpeg_bin,
    )

    try:
        width, height = png_dimensions(frame)
    except ApprovalError as exc:
        raise SegmentEndFrameError(str(exc)) from exc

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_segment_id": safe_segment_id,
        "derived_for_next_segment_id": next_segment_id(safe_segment_id),
        "source_video_file": source.name,
        "source_video_sha256": file_sha256(source),
        "frame_file": frame.name,
        "frame_sha256": file_sha256(frame),
        "width": width,
        "height": height,
        "requested_offset_from_end_seconds": float(offset_seconds),
        "offset_from_end_seconds": float(actual_offset_seconds),
    }
    _atomic_write_json(metadata, payload)
    return {
        **payload,
        "frame_path": str(frame),
        "metadata_path": str(metadata),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = extract_segment_end_frame(
            segment_id=args.segment_id,
            video=args.video,
            output_dir=args.output_dir,
            offset_seconds=args.offset_seconds,
            ffmpeg_bin=args.ffmpeg_bin,
        )
    except (OSError, ValueError, SegmentEndFrameError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    if args.print_report:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(report["frame_path"])
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
