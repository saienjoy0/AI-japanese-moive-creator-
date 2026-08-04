"""Deterministic provider-free implementations for every PR4 RenderGraph task."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path

from ..preparation.models import PreparedEpisode, RenderTaskNode, StoryboardFrameDraft
from .ffmpeg import escape_filter_path, ffmpeg, media_has_audio


@dataclass(frozen=True)
class TaskContext:
    prepared: PreparedEpisode
    frame: StoryboardFrameDraft
    node: RenderTaskNode
    work_dir: Path
    dependency_outputs: list[Path]


class MockTaskExecutor:
    """Execute graph nodes using only local files, Python, and FFmpeg."""

    width = 540
    height = 960

    def _task_dir(self, context: TaskContext) -> Path:
        path = context.work_dir / "tasks" / context.node.task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _run_generate_image(self, context: TaskContext) -> list[Path]:
        output = self._task_dir(context) / "mock_image.ppm"
        self._write_mock_ppm(output, context.frame)
        return [output]

    def _run_generate_video(self, context: TaskContext) -> list[Path]:
        directory = self._task_dir(context)
        image = directory / "mock_image.ppm"
        output = directory / "mock_video.mp4"
        self._write_mock_ppm(image, context.frame)
        self._animate_image(image, output, context.frame.duration_seconds)
        return [image, output]

    def _run_generate_native_av(self, context: TaskContext) -> list[Path]:
        directory = self._task_dir(context)
        image = directory / "mock_image.ppm"
        video = directory / "mock_native_av.mp4"
        self._write_mock_ppm(image, context.frame)
        duration = self._duration(context.frame)
        frequency = self._frequency(context.frame.source_shot_id, base=180)
        ffmpeg(
            "-loop",
            "1",
            "-framerate",
            str(context.prepared.project_draft.fps),
            "-i",
            str(image),
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration={duration:.3f}",
            "-vf",
            self._motion_filter(context.frame, context.prepared.project_draft.fps),
            "-af",
            "volume=0.07",
            "-t",
            f"{duration:.3f}",
            "-r",
            str(context.prepared.project_draft.fps),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "27",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            str(video),
        )
        return [image, video]

    def _run_generate_tts(self, context: TaskContext) -> list[Path]:
        output = self._task_dir(context) / "mock_voice.wav"
        duration = self._duration(context.frame)
        inputs: list[str] = []
        filters: list[str] = []
        mix_labels: list[str] = []

        inputs.extend(["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={duration:.3f}"])
        mix_labels.append("[0:a]")
        for index, cue in enumerate(context.frame.dialogue_cues, start=1):
            frequency = self._frequency(cue.speaker_character_id, base=330 + index * 25)
            inputs.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={frequency}:sample_rate=48000:duration={duration:.3f}",
                ]
            )
            start = max(0.0, cue.start_seconds)
            end = min(duration, cue.end_seconds)
            filters.append(
                f"[{index}:a]volume='if(between(t,{start:.3f},{end:.3f}),0.20,0)'[voice{index}]"
            )
            mix_labels.append(f"[voice{index}]")

        filter_complex = ";".join(
            [
                *filters,
                f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:normalize=0,alimiter=limit=0.8[outa]",
            ]
        )
        ffmpeg(
            *inputs,
            "-filter_complex",
            filter_complex,
            "-map",
            "[outa]",
            "-t",
            f"{duration:.3f}",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(output),
        )
        return [output]

    def _run_generate_subtitles(self, context: TaskContext) -> list[Path]:
        output = self._task_dir(context) / "subtitles.ass"
        output.write_text(self._ass_document(context.frame, context.prepared), encoding="utf-8")
        return [output]

    def _run_apply_still_motion(self, context: TaskContext) -> list[Path]:
        image = self._first_with_suffix(context.dependency_outputs, {".ppm", ".png", ".jpg", ".jpeg"})
        output = self._task_dir(context) / "still_motion.mp4"
        self._animate_image(image, output, context.frame.duration_seconds)
        return [output]

    def _run_mux_audio_video(self, context: TaskContext) -> list[Path]:
        video = self._first_with_suffix(context.dependency_outputs, {".mp4", ".mov", ".mkv"})
        voice = self._first_with_suffix(context.dependency_outputs, {".wav", ".mp3", ".m4a", ".aac"})
        subtitle = self._first_with_suffix(context.dependency_outputs, {".ass", ".srt"})
        output = self._task_dir(context) / "muxed.mp4"
        duration = self._duration(context.frame)
        subtitle_filter = f"ass='{escape_filter_path(subtitle)}'"
        sfx_frequency = self._frequency(context.frame.source_shot_id, base=760)
        ffmpeg(
            "-i",
            str(video),
            "-i",
            str(voice),
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={sfx_frequency}:sample_rate=48000:duration={duration:.3f}",
            "-filter_complex",
            "[1:a]volume=1.0[voice];"
            "[2:a]volume='if(between(t,0.55,0.72),0.12,0)'[sfx];"
            "[voice][sfx]amix=inputs=2:normalize=0,alimiter=limit=0.9[outa]",
            "-map",
            "0:v:0",
            "-map",
            "[outa]",
            "-vf",
            subtitle_filter,
            "-t",
            f"{duration:.3f}",
            "-r",
            str(context.prepared.project_draft.fps),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "27",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            str(output),
        )
        return [output]

    def _run_finalize_shot(self, context: TaskContext) -> list[Path]:
        video = self._first_with_suffix(context.dependency_outputs, {".mp4", ".mov", ".mkv"})
        subtitles = [path for path in context.dependency_outputs if path.suffix.lower() in {".ass", ".srt"}]
        output = self._task_dir(context) / "final_shot.mp4"
        duration = self._duration(context.frame)
        bgm_frequency = self._frequency(context.prepared.episode_id, base=105)
        has_audio = media_has_audio(video)

        args: list[str] = ["-i", str(video)]
        if has_audio:
            args.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={bgm_frequency}:sample_rate=48000:duration={duration:.3f}",
                    "-filter_complex",
                    "[0:a]volume=0.95[base];[1:a]volume=0.025[bgm];"
                    "[base][bgm]amix=inputs=2:normalize=0,alimiter=limit=0.9[outa]",
                    "-map",
                    "0:v:0",
                    "-map",
                    "[outa]",
                ]
            )
        else:
            args.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={bgm_frequency}:sample_rate=48000:duration={duration:.3f}",
                    "-filter_complex",
                    "[1:a]volume=0.03[outa]",
                    "-map",
                    "0:v:0",
                    "-map",
                    "[outa]",
                ]
            )

        if subtitles:
            args.extend(["-vf", f"ass='{escape_filter_path(subtitles[0])}'"])
        args.extend(
            [
                "-t",
                f"{duration:.3f}",
                "-r",
                str(context.prepared.project_draft.fps),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "27",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                str(output),
            ]
        )
        ffmpeg(*args)
        return [output]

    def _animate_image(self, image: Path, output: Path, duration_seconds: float) -> None:
        duration = max(0.04, duration_seconds)
        fps = 30
        ffmpeg(
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(image),
            "-vf",
            self._motion_filter_for_duration(fps, duration),
            "-t",
            f"{duration:.3f}",
            "-r",
            str(fps),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "27",
            "-pix_fmt",
            "yuv420p",
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            str(output),
        )

    def _motion_filter(self, frame: StoryboardFrameDraft, fps: int) -> str:
        return self._motion_filter_for_duration(fps, self._duration(frame))

    def _motion_filter_for_duration(self, fps: int, duration: float) -> str:
        frames = max(1, int(round(duration * fps)))
        return (
            f"scale={self.width}:{self.height},"
            f"zoompan=z='min(zoom+0.00055,1.08)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={self.width}x{self.height}:fps={fps},"
            "format=yuv420p"
        )

    def _write_mock_ppm(self, path: Path, frame: StoryboardFrameDraft) -> None:
        width, height = self.width, self.height
        seed = hashlib.sha256(frame.source_shot_id.encode("utf-8")).digest()
        base = tuple(38 + value % 120 for value in seed[:3])
        accent = tuple(110 + value % 130 for value in seed[3:6])
        horizon = int(height * (0.30 + (seed[6] % 20) / 100))
        actor_x = int(width * (0.18 + (seed[7] % 25) / 100))
        actor_width = int(width * 0.22)
        actor_height = int(height * 0.34)

        with path.open("wb") as handle:
            handle.write(f"P6\n{width} {height}\n255\n".encode("ascii"))
            for y in range(height):
                row = bytearray()
                ratio = y / max(1, height - 1)
                for x in range(width):
                    if y < horizon:
                        pixel = tuple(min(255, int(channel * (0.72 + ratio * 0.35))) for channel in base)
                    else:
                        pixel = tuple(min(255, int(channel * (0.48 + ratio * 0.35))) for channel in base)

                    in_actor = (
                        actor_x <= x < actor_x + actor_width
                        and horizon + 70 <= y < horizon + 70 + actor_height
                    )
                    in_counter = horizon + actor_height + 120 <= y < horizon + actor_height + 190
                    in_light = 55 <= y < 76 and 60 <= x < width - 60
                    if in_actor:
                        pixel = accent
                    elif in_counter:
                        pixel = tuple(min(255, c + 25) for c in base)
                    elif in_light:
                        pixel = (236, 232, 192)
                    row.extend(pixel)
                handle.write(row)

    def _ass_document(
        self,
        frame: StoryboardFrameDraft,
        prepared: PreparedEpisode,
    ) -> str:
        names = {
            seed.source_character_id: seed.name
            for seed in prepared.character_seeds
        }
        names.update({seed.seed_id: seed.name for seed in prepared.character_seeds})
        events = []
        for cue in frame.dialogue_cues:
            speaker = names.get(cue.speaker_character_id, cue.speaker_character_id)
            text = self._escape_ass(f"{speaker}：{cue.text}")
            events.append(
                "Dialogue: 0,"
                f"{self._ass_time(cue.start_seconds)},{self._ass_time(cue.end_seconds)},"
                f"Default,,0,0,0,,{text}"
            )
        if not events:
            events.append(
                "Dialogue: 0,0:00:00.00,0:00:01.20,Default,,0,0,0,,"
                + self._escape_ass(frame.action)
            )
        return "\n".join(
            [
                "[Script Info]",
                "ScriptType: v4.00+",
                "PlayResX: 540",
                "PlayResY: 960",
                "ScaledBorderAndShadow: yes",
                "",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                "Style: Default,Noto Sans CJK JP,34,&H00FFFFFF,&H000000FF,&H00101010,&H90000000,-1,0,0,0,100,100,0,0,1,3,1,2,28,28,90,1",
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                *events,
                "",
            ]
        )

    def execute(self, context: TaskContext) -> list[Path]:
        injected = os.environ.get("JP_DRAMA_FAIL_TASK_ID")
        if injected and injected == context.node.task_id:
            raise RuntimeError(f"injected failure for {injected}")
        handler = getattr(self, f"_run_{context.node.task_type}", None)
        if handler is None:
            raise RuntimeError(f"unsupported mock task type: {context.node.task_type}")
        outputs = handler(context)
        for output in outputs:
            if not output.exists() or output.stat().st_size == 0:
                raise RuntimeError(f"task did not create a non-empty output: {output}")
        return outputs

    @staticmethod
    def _first_with_suffix(paths: list[Path], suffixes: set[str]) -> Path:
        for path in paths:
            if path.suffix.lower() in suffixes:
                return path
        raise RuntimeError(f"dependency output with suffix {sorted(suffixes)} was not found")

    @staticmethod
    def _duration(frame: StoryboardFrameDraft) -> float:
        return max(0.04, float(frame.duration_seconds))

    @staticmethod
    def _frequency(seed: str, *, base: int) -> int:
        value = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:2], "big")
        return base + value % 170

    @staticmethod
    def _ass_time(seconds: float) -> str:
        value = max(0.0, seconds)
        hours = int(value // 3600)
        minutes = int((value % 3600) // 60)
        whole = int(value % 60)
        centiseconds = int(round((value - math.floor(value)) * 100))
        if centiseconds == 100:
            whole += 1
            centiseconds = 0
        return f"{hours}:{minutes:02d}:{whole:02d}.{centiseconds:02d}"

    @staticmethod
    def _escape_ass(text: str) -> str:
        return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")
