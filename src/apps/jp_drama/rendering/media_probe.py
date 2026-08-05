"""Strict local media validation helpers for paid provider artifacts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class MediaProbeError(RuntimeError):
    """A downloaded media artifact is structurally invalid or cannot be verified."""


def validate_h3_mp4(
    path: str | Path,
    *,
    expected_duration_seconds: int,
    expected_resolution: str,
) -> None:
    media = Path(path)
    if not media.is_file() or media.stat().st_size < 12:
        raise MediaProbeError("downloaded H3 video is empty or too small")
    with media.open("rb") as handle:
        header = handle.read(64)
    if b"ftyp" not in header:
        raise MediaProbeError("downloaded H3 video is not an MP4 container")
    executable = shutil.which("ffprobe")
    if not executable:
        raise MediaProbeError("ffprobe is required for paid H3 artifact validation")
    command = [
        executable,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,width,height",
        "-of",
        "json",
        str(media),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise MediaProbeError(
            "ffprobe rejected downloaded H3 video: " + (completed.stderr.strip() or "unknown error")
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe returned invalid JSON") from exc
    video_streams = [
        item
        for item in payload.get("streams", [])
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if len(video_streams) != 1:
        raise MediaProbeError("downloaded H3 MP4 must contain exactly one video stream")
    stream = video_streams[0]
    codec = str(stream.get("codec_name") or "").lower()
    if codec not in {"h264", "hevc"}:
        raise MediaProbeError(f"downloaded H3 video codec is unsupported: {codec or '<empty>'}")
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0 or height <= width:
        raise MediaProbeError("downloaded H3 video is not a valid vertical video")
    minimum_short_edge = 700 if expected_resolution == "768P" else 1100
    if min(width, height) < minimum_short_edge:
        raise MediaProbeError(
            f"downloaded H3 video is below expected {expected_resolution} resolution"
        )
    try:
        duration = float(payload.get("format", {}).get("duration"))
    except (TypeError, ValueError) as exc:
        raise MediaProbeError("downloaded H3 video duration is missing") from exc
    tolerance = max(0.75, expected_duration_seconds * 0.15)
    if abs(duration - expected_duration_seconds) > tolerance:
        raise MediaProbeError(
            f"downloaded H3 duration {duration:.3f}s differs from requested "
            f"{expected_duration_seconds}s"
        )
