#!/usr/bin/env python3
"""Create and validate a tiny Japanese 9:16 episode without external AI APIs.

This is a foundation smoke test, not a production renderer. It proves that the
CI environment can create multiple shots, concatenate audio/video, render
Japanese subtitles, and inspect the final MP4 with ffprobe.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SHOTS = [
    {"id": "shot_01", "color": "0x1f2937", "frequency": 440, "caption": "その格好で入らないでください"},
    {"id": "shot_02", "color": "0x374151", "frequency": 554, "caption": "でも、確認してほしいことがあります"},
    {"id": "shot_03", "color": "0x111827", "frequency": 659, "caption": "私がこの店の新しい責任者です"},
]


def run(command: list[str], *, cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command))
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
    )


def srt_timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def write_subtitles(path: Path) -> None:
    blocks: list[str] = []
    for index, shot in enumerate(SHOTS, start=1):
        start = index - 1
        end = index
        blocks.append(
            f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{shot['caption']}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


def probe(path: Path) -> dict[str, Any]:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture=True,
    )
    return json.loads(result.stdout)


def validate_probe(data: dict[str, Any]) -> None:
    streams = data.get("streams", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(video_streams) != 1:
        raise RuntimeError(f"expected one video stream, got {len(video_streams)}")
    if len(audio_streams) != 1:
        raise RuntimeError(f"expected one audio stream, got {len(audio_streams)}")

    video = video_streams[0]
    if (video.get("width"), video.get("height")) != (360, 640):
        raise RuntimeError(f"expected 360x640 video, got {video.get('width')}x{video.get('height')}")

    duration = float(data.get("format", {}).get("duration", 0))
    if not 2.8 <= duration <= 3.3:
        raise RuntimeError(f"expected about 3 seconds, got {duration:.3f}")


def create_episode(output_dir: Path) -> Path:
    for executable in ("ffmpeg", "ffprobe"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"{executable} is required but was not found in PATH")

    output_dir.mkdir(parents=True, exist_ok=True)
    shot_paths: list[Path] = []

    for shot in SHOTS:
        shot_path = output_dir / f"{shot['id']}.mp4"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={shot['color']}:s=360x640:r=30:d=1",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={shot['frequency']}:sample_rate=48000:duration=1",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(shot_path),
            ]
        )
        shot_paths.append(shot_path)

    concat_file = output_dir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{path.name}'\n" for path in shot_paths),
        encoding="utf-8",
    )

    raw_episode = output_dir / "mock_episode_raw.mp4"
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file.name,
            "-c",
            "copy",
            raw_episode.name,
        ],
        cwd=output_dir,
    )

    subtitles = output_dir / "captions.srt"
    write_subtitles(subtitles)

    final_episode = output_dir / "mock_episode.mp4"
    subtitle_filter = (
        "subtitles=filename=captions.srt:force_style='"
        "FontName=Noto Sans CJK JP,FontSize=20,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Alignment=2,MarginV=48'"
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            raw_episode.name,
            "-vf",
            subtitle_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            final_episode.name,
        ],
        cwd=output_dir,
    )

    probe_data = probe(final_episode)
    validate_probe(probe_data)

    manifest = {
        "mode": "offline_foundation_smoke_test",
        "external_api_calls": 0,
        "aspect_ratio": "9:16",
        "resolution": {"width": 360, "height": 640},
        "shots": SHOTS,
        "probe": probe_data,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return final_episode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/mock_episode"))
    args = parser.parse_args()

    try:
        output = create_episode(args.output_dir.resolve())
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"MOCK EPISODE FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"MOCK EPISODE OK: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
