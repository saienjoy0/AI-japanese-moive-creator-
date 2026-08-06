"""Build a zero-provider-call continuity plan for MiniMax H3.

The already approved E01-G01 video remains the final first segment.  This command
also extracts one stable image near its end and makes that image the first-frame
continuity reference for E01-G02.  Later segments are chained only when adjacent
segments share the same continuity group.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


EXIT_OK = 0
EXIT_INPUT = 1
REUSED_SEGMENT_ID = "E01-G01"
FIRST_CONTINUATION_SEGMENT_ID = "E01-G02"
FRAME_OFFSET_FROM_END_SECONDS = 0.5


class ContinuityError(RuntimeError):
    """The continuity handoff cannot be built safely."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract the final stable frame of E01-G01 and bind it to E01-G02, "
            "without calling MiniMax or any other video provider."
        )
    )
    parser.add_argument("--generation-plan", required=True, type=Path)
    parser.add_argument("--input-video", required=True, type=Path)
    parser.add_argument("--h3-handoff", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_object(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ContinuityError(f"required input is missing or empty: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuityError(f"invalid JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContinuityError(f"JSON input must be an object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _content_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    path = path.resolve()
    if path.exists() and not overwrite:
        raise ContinuityError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _probe_duration(path: Path) -> float:
    if not path.is_file() or path.stat().st_size == 0:
        raise ContinuityError(f"input video is missing or empty: {path}")
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path.resolve()),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        duration = float(completed.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
        raise ContinuityError(f"unable to probe input video duration: {exc}") from exc
    if duration <= FRAME_OFFSET_FROM_END_SECONDS:
        raise ContinuityError(f"input video is too short for a stable end frame: {duration}")
    return duration


def _extract_stable_end_frame(video: Path, output: Path, duration: float) -> float:
    timestamp = max(0.0, duration - FRAME_OFFSET_FROM_END_SECONDS)
    if output.exists():
        raise ContinuityError(f"refusing to overwrite existing frame: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.tmp-{os.getpid()}.png")
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-ss",
        f"{timestamp:.6f}",
        "-i",
        str(video.resolve()),
        "-frames:v",
        "1",
        "-compression_level",
        "4",
        str(temporary),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        if temporary.exists():
            temporary.unlink()
        raise ContinuityError(f"unable to extract continuity frame: {exc}") from exc
    if not temporary.is_file() or temporary.stat().st_size == 0:
        raise ContinuityError("ffmpeg produced no continuity frame")
    os.replace(temporary, output)
    return timestamp


def _first_frame_asset(segment: dict[str, Any]) -> str:
    references = segment.get("reference_asset_ids")
    if not isinstance(references, list):
        raise ContinuityError(
            f"segment {segment.get('segment_id')} has no reference_asset_ids"
        )
    candidates = [str(item) for item in references if str(item).startswith("ref_first_")]
    if len(candidates) != 1:
        raise ContinuityError(
            f"segment {segment.get('segment_id')} must have exactly one ref_first asset"
        )
    return candidates[0]


def _ordered_segments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    segments = plan.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ContinuityError("generation plan has no segments")
    if not all(isinstance(item, dict) for item in segments):
        raise ContinuityError("generation plan contains a non-object segment")
    ordered = sorted(segments, key=lambda item: int(item.get("order", 0)))
    ids = [str(item.get("segment_id") or "") for item in ordered]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ContinuityError("generation plan segment IDs are empty or duplicated")
    return ordered


def _build_entries(
    segments: list[dict[str, Any]],
    *,
    extracted_frame_path: Path,
    extracted_frame_sha256: str,
    extracted_at_seconds: float,
) -> list[dict[str, Any]]:
    ids = [str(item["segment_id"]) for item in segments]
    try:
        reused_index = ids.index(REUSED_SEGMENT_ID)
    except ValueError as exc:
        raise ContinuityError("generation plan does not contain E01-G01") from exc
    entries: list[dict[str, Any]] = []
    for index in range(reused_index + 1, len(segments)):
        current = segments[index]
        previous = segments[index - 1]
        current_id = str(current["segment_id"])
        previous_id = str(previous["segment_id"])
        same_group = bool(current.get("continuity_group_id")) and (
            current.get("continuity_group_id") == previous.get("continuity_group_id")
        )
        if current_id == FIRST_CONTINUATION_SEGMENT_ID:
            if previous_id != REUSED_SEGMENT_ID or not same_group:
                raise ContinuityError(
                    "E01-G02 must immediately follow E01-G01 in the same continuity group"
                )
            entries.append(
                {
                    "segment_id": current_id,
                    "mode": "extracted_previous_segment_end_frame",
                    "status": "ready",
                    "source_segment_id": previous_id,
                    "reference_role": "first_frame",
                    "reference_path": str(extracted_frame_path.resolve()),
                    "reference_sha256": extracted_frame_sha256,
                    "extracted_at_seconds": round(extracted_at_seconds, 6),
                    "send_full_previous_video": False,
                }
            )
        elif same_group:
            entries.append(
                {
                    "segment_id": current_id,
                    "mode": "previous_segment_end_frame",
                    "status": "pending_previous_segment_generation",
                    "source_segment_id": previous_id,
                    "reference_role": "first_frame",
                    "send_full_previous_video": False,
                }
            )
        else:
            entries.append(
                {
                    "segment_id": current_id,
                    "mode": "planned_first_frame_asset",
                    "status": "ready_from_asset_bundle",
                    "source_asset_id": _first_frame_asset(current),
                    "reference_role": "first_frame",
                    "send_full_previous_video": False,
                }
            )
    return entries


def run(args: argparse.Namespace) -> dict[str, Any]:
    plan = _load_object(args.generation_plan)
    handoff = _load_object(args.h3_handoff)
    segments = _ordered_segments(plan)
    remaining = [
        str(item["segment_id"])
        for item in segments
        if str(item["segment_id"]) != REUSED_SEGMENT_ID
    ]
    if handoff.get("reused_segment_id") != REUSED_SEGMENT_ID:
        raise ContinuityError("H3 handoff does not reuse E01-G01")
    if handoff.get("remaining_segment_ids") != remaining:
        raise ContinuityError("H3 handoff remaining segments do not match the plan")
    if handoff.get("external_api_calls") != 0:
        raise ContinuityError("H3 handoff does not prove zero provider calls")

    output_dir = args.output_dir.resolve()
    frame_path = output_dir / f"{REUSED_SEGMENT_ID}.last-stable-frame.png"
    manifest_path = output_dir / "minimax_h3_cn_continuity_plan.json"
    if frame_path.exists() and args.overwrite:
        frame_path.unlink()
    duration = _probe_duration(args.input_video.resolve())
    timestamp = _extract_stable_end_frame(
        args.input_video.resolve(),
        frame_path,
        duration,
    )
    frame_sha = _sha256(frame_path)
    entries = _build_entries(
        segments,
        extracted_frame_path=frame_path,
        extracted_frame_sha256=frame_sha,
        extracted_at_seconds=timestamp,
    )
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "generation_plan_digest": plan.get("content_digest"),
        "h3_handoff_digest": handoff.get("content_digest"),
        "reused_segment_id": REUSED_SEGMENT_ID,
        "source_video_sha256": _sha256(args.input_video.resolve()),
        "continuity_frame": {
            "path": str(frame_path),
            "sha256": frame_sha,
            "extracted_at_seconds": round(timestamp, 6),
            "offset_from_end_seconds": FRAME_OFFSET_FROM_END_SECONDS,
        },
        "segment_continuity": entries,
        "rule": (
            "reference_the_previous_segment_end_frame_only_within_the_same_"
            "continuity_group"
        ),
        "external_api_calls": 0,
    }
    payload["content_digest"] = _content_digest(payload)
    _write_json(manifest_path, payload, overwrite=args.overwrite)
    return {
        "valid": True,
        "external_api_calls": 0,
        "source_video_used_as_reference": True,
        "full_source_video_sent_to_h3": False,
        "continuity_frame": str(frame_path),
        "continuity_frame_sha256": frame_sha,
        "continuity_plan": str(manifest_path),
        "first_target_segment": FIRST_CONTINUATION_SEGMENT_ID,
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except (OSError, ValueError, ContinuityError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    if args.print_report:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(
            "H3 continuity reference: VALID\n"
            f"Source video: {args.input_video.resolve()}\n"
            f"First target: {FIRST_CONTINUATION_SEGMENT_ID}\n"
            "Full source video sent to H3: no\n"
            "New provider calls: 0"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
