"""Restart-safe exact-frame composition for provider-generated drama segments."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..rendering.ffmpeg import (
    black_duration,
    ffmpeg,
    ffprobe_json,
    file_sha256,
    media_has_audio,
    require_ffmpeg,
    run_command,
)
from .models import (
    FullEpisodeRunState,
    FullEpisodeSegmentState,
    FullEpisodeValidationReport,
    SegmentMediaValidation,
)


class FullEpisodeError(RuntimeError):
    """Base error for full-episode orchestration and composition."""


class FullEpisodeStateConflictError(FullEpisodeError):
    """Persisted state belongs to a different plan, output, or target profile."""


class FullEpisodeSegmentError(FullEpisodeError):
    """A provider segment cannot be trimmed or validated."""


class FullEpisodeValidationError(FullEpisodeError):
    """The final episode does not match the generation timeline contract."""


class FullEpisodeComposer:
    """Trim each provider clip to its editorial frame window and concatenate exactly."""

    def __init__(
        self,
        plan: GenerationPlanEpisode,
        *,
        output_file: str | Path,
        work_dir: str | Path,
        target_width: int = 720,
        target_height: int = 1280,
        asset_bundle_digest: str | None = None,
        execution_budget_digest: str | None = None,
    ) -> None:
        if not plan.readiness_report.execution_route_ready:
            raise FullEpisodeError("GenerationPlan is not execution-route-ready")
        if target_width <= 0 or target_height <= 0:
            raise ValueError("target dimensions must be greater than zero")
        if target_width * 16 != target_height * 9:
            raise ValueError("target dimensions must be 9:16")
        self.plan = plan
        self.output_file = Path(output_file).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.segment_dir = self.work_dir / "segments"
        self.state_file = self.work_dir / "full_episode_state.json"
        self.report_file = self.work_dir / "full_episode_validation.json"
        self.concat_file = self.work_dir / "concat.txt"
        self.concat_raw_file = self.work_dir / "episode_concat_raw.mp4"
        self.target_width = target_width
        self.target_height = target_height
        self.asset_bundle_digest = asset_bundle_digest
        self.execution_budget_digest = execution_budget_digest
        self.segments_by_id = {item.segment_id: item for item in plan.segments}

    def compose(
        self,
        segment_outputs: Mapping[str, str | Path],
        *,
        reset: bool = False,
        external_api_calls: int = 0,
    ) -> FullEpisodeValidationReport:
        require_ffmpeg()
        if external_api_calls < 0:
            raise ValueError("external_api_calls must not be negative")
        expected = [item.segment_id for item in self.plan.segments]
        missing = [item for item in expected if item not in segment_outputs]
        extra = sorted(set(segment_outputs) - set(expected))
        if missing or extra:
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if extra:
                details.append("unexpected=" + ",".join(extra))
            raise FullEpisodeSegmentError(
                "segment output mapping does not match GenerationPlan: " + "; ".join(details)
            )

        if reset:
            self._reset()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.segment_dir.mkdir(parents=True, exist_ok=True)
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        state = self._load_or_create_state()
        state.status = "composing"
        self._write_state(state)

        validations: list[SegmentMediaValidation] = []
        try:
            for segment in self.plan.segments:
                source = Path(segment_outputs[segment.segment_id]).resolve()
                validation = self._prepare_segment(state, segment, source)
                validations.append(validation)
            self._concatenate(state)
            report = self._validate_episode(
                state,
                validations,
                external_api_calls=external_api_calls,
            )
            self._atomic_write(self.report_file, report.to_canonical_json())
            if not report.valid:
                state.status = "failed"
                state.final_output_sha256 = (
                    file_sha256(self.output_file) if self.output_file.is_file() else None
                )
                self._write_state(state)
                raise FullEpisodeValidationError("; ".join(report.errors))
            state.status = "succeeded"
            state.final_output_sha256 = report.output_sha256
            self._write_state(state)
            return report
        except Exception:
            if state.status != "succeeded":
                state.status = "failed"
                self._write_state(state)
            raise

    def _prepare_segment(
        self,
        state: FullEpisodeRunState,
        segment: GenerationSegment,
        source: Path,
    ) -> SegmentMediaValidation:
        item = state.segments[segment.segment_id]
        if not source.is_file() or source.stat().st_size == 0:
            item.status = "failed"
            item.last_error = f"provider segment is missing or empty: {source}"
            self._write_state(state)
            raise FullEpisodeSegmentError(item.last_error)
        source_sha = file_sha256(source)
        trimmed = self.segment_dir / f"{segment.order:03d}_{segment.segment_id}.mp4"
        can_reuse = (
            item.status == "validated"
            and item.source_output_file == str(source)
            and item.source_output_sha256 == source_sha
            and item.trimmed_output_file == str(trimmed)
            and trimmed.is_file()
            and item.trimmed_output_sha256 == file_sha256(trimmed)
        )
        if can_reuse:
            return self._validate_segment(segment, source, trimmed)

        item.status = "trimming"
        item.attempts += 1
        item.source_output_file = str(source)
        item.source_output_sha256 = source_sha
        item.trimmed_output_file = str(trimmed)
        item.trimmed_output_sha256 = None
        item.last_error = None
        state.final_output_sha256 = None
        self._write_state(state)

        try:
            self._validate_source_window(segment, source)
            self._trim_segment(segment, source, trimmed)
            item.status = "trimmed"
            item.trimmed_output_sha256 = file_sha256(trimmed)
            self._write_state(state)
            validation = self._validate_segment(segment, source, trimmed)
            if not validation.valid:
                raise FullEpisodeSegmentError(
                    f"trimmed segment {segment.segment_id} is invalid: "
                    + "; ".join(validation.errors)
                )
            item.status = "validated"
            item.trimmed_output_sha256 = validation.trimmed_sha256
            item.last_error = None
            self._write_state(state)
            return validation
        except Exception as exc:
            item.status = "failed"
            item.last_error = f"{type(exc).__name__}: {exc}"
            item.trimmed_output_sha256 = None
            self._write_state(state)
            raise

    def _validate_source_window(self, segment: GenerationSegment, source: Path) -> None:
        probe = ffprobe_json(source)
        video = [
            item for item in probe.get("streams", []) if item.get("codec_type") == "video"
        ]
        if not video:
            raise FullEpisodeSegmentError(
                f"provider segment has no video stream: {source}"
            )
        duration = float(probe.get("format", {}).get("duration") or 0.0)
        required_end = segment.used_end_frame / segment.timeline_fps
        tolerance = max(0.08, 2 / segment.timeline_fps)
        if duration + tolerance < required_end:
            raise FullEpisodeSegmentError(
                f"provider segment {segment.segment_id} is {duration:.3f}s but the "
                f"editorial window requires {required_end:.3f}s"
            )

    def _trim_segment(
        self,
        segment: GenerationSegment,
        source: Path,
        destination: Path,
    ) -> None:
        start = segment.used_start_frame / segment.timeline_fps
        duration = segment.editorial_frame_count / segment.timeline_fps
        destination.parent.mkdir(parents=True, exist_ok=True)
        video_filter = (
            f"trim=start_frame={segment.used_start_frame}:end_frame={segment.used_end_frame},"
            "setpts=PTS-STARTPTS,"
            f"scale={self.target_width}:{self.target_height}:force_original_aspect_ratio=increase,"
            f"crop={self.target_width}:{self.target_height},"
            f"fps={segment.timeline_fps},format=yuv420p"
        )
        common = [
            "-map_metadata",
            "-1",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(segment.timeline_fps),
            "-keyint_min",
            str(segment.timeline_fps),
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-frames:v",
            str(segment.editorial_frame_count),
            "-t",
            f"{duration:.9f}",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        if media_has_audio(source):
            audio_filter = (
                f"atrim=start={start:.9f}:end={start + duration:.9f},"
                "asetpts=PTS-STARTPTS,aresample=48000"
            )
            ffmpeg(
                "-i",
                str(source),
                "-vf",
                video_filter,
                "-af",
                audio_filter,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                *common,
            )
        else:
            ffmpeg(
                "-i",
                str(source),
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=48000:cl=stereo:d={duration:.9f}",
                "-vf",
                video_filter,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                *common,
            )

    def _validate_segment(
        self,
        segment: GenerationSegment,
        source: Path,
        trimmed: Path,
    ) -> SegmentMediaValidation:
        probe = ffprobe_json(trimmed)
        streams = probe.get("streams", [])
        videos = [item for item in streams if item.get("codec_type") == "video"]
        audios = [item for item in streams if item.get("codec_type") == "audio"]
        video = videos[0] if videos else {}
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        fps = self._parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        duration = float(probe.get("format", {}).get("duration") or 0.0)
        frames = self._count_frames(trimmed)
        expected_duration = segment.editorial_frame_count / segment.timeline_fps
        errors: list[str] = []
        if not videos:
            errors.append("video stream is missing")
        if not audios:
            errors.append("audio stream is missing")
        if (width, height) != (self.target_width, self.target_height):
            errors.append(
                f"dimensions {width}x{height} != {self.target_width}x{self.target_height}"
            )
        if abs(fps - segment.timeline_fps) > 0.01:
            errors.append(f"fps {fps:.4f} != {segment.timeline_fps}")
        if frames != segment.editorial_frame_count:
            errors.append(
                f"frame count {frames} != {segment.editorial_frame_count}"
            )
        tolerance = max(0.08, 2 / segment.timeline_fps)
        if abs(duration - expected_duration) > tolerance:
            errors.append(
                f"duration {duration:.4f}s != {expected_duration:.4f}s"
            )
        return SegmentMediaValidation(
            segment_id=segment.segment_id,
            source_file=str(source),
            trimmed_file=str(trimmed),
            source_sha256=file_sha256(source),
            trimmed_sha256=file_sha256(trimmed),
            expected_frames=segment.editorial_frame_count,
            actual_frames=frames,
            expected_duration_seconds=expected_duration,
            actual_duration_seconds=duration,
            width=max(1, width),
            height=max(1, height),
            fps=max(0.0001, fps),
            video_streams=len(videos),
            audio_streams=len(audios),
            valid=not errors,
            errors=errors,
        )

    def _concatenate(self, state: FullEpisodeRunState) -> None:
        paths = [
            Path(state.segments[item].trimmed_output_file or "")
            for item in state.segment_order
        ]
        if any(not item.is_file() for item in paths):
            missing = [str(item) for item in paths if not item.is_file()]
            raise FullEpisodeSegmentError(
                "cannot concatenate missing trimmed segments: " + ", ".join(missing)
            )
        lines = []
        for path in paths:
            escaped = str(path.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        self._atomic_write(self.concat_file, "\n".join(lines) + "\n")
        ffmpeg(
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(self.concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(self.concat_raw_file),
        )
        target_duration = self.plan.target_frame_count / self.plan.timeline_fps
        ffmpeg(
            "-i",
            str(self.concat_raw_file),
            "-vf",
            (
                f"scale={self.target_width}:{self.target_height}:"
                "force_original_aspect_ratio=increase,"
                f"crop={self.target_width}:{self.target_height},"
                f"fps={self.plan.timeline_fps},format=yuv420p"
            ),
            "-af",
            "aresample=48000,asetpts=PTS-STARTPTS",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-frames:v",
            str(self.plan.target_frame_count),
            "-t",
            f"{target_duration:.9f}",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(self.plan.timeline_fps),
            "-keyint_min",
            str(self.plan.timeline_fps),
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            str(self.output_file),
        )

    def _validate_episode(
        self,
        state: FullEpisodeRunState,
        validations: list[SegmentMediaValidation],
        *,
        external_api_calls: int,
    ) -> FullEpisodeValidationReport:
        if not self.output_file.is_file() or self.output_file.stat().st_size == 0:
            raise FullEpisodeValidationError(
                f"full episode output is missing or empty: {self.output_file}"
            )
        probe = ffprobe_json(self.output_file)
        streams = probe.get("streams", [])
        videos = [item for item in streams if item.get("codec_type") == "video"]
        audios = [item for item in streams if item.get("codec_type") == "audio"]
        video = videos[0] if videos else {}
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        fps = self._parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        duration = float(probe.get("format", {}).get("duration") or 0.0)
        actual_frames = self._count_frames(self.output_file)
        black_seconds = black_duration(self.output_file)
        expected_duration = self.plan.target_frame_count / self.plan.timeline_fps
        errors: list[str] = []
        if not videos:
            errors.append("video stream is missing")
        if not audios:
            errors.append("audio stream is missing")
        if (width, height) != (self.target_width, self.target_height):
            errors.append(
                f"dimensions {width}x{height} != {self.target_width}x{self.target_height}"
            )
        if abs(fps - self.plan.timeline_fps) > 0.01:
            errors.append(f"fps {fps:.4f} != {self.plan.timeline_fps}")
        if actual_frames != self.plan.target_frame_count:
            errors.append(
                f"frame count {actual_frames} != {self.plan.target_frame_count}"
            )
        tolerance = max(0.08, 2 / self.plan.timeline_fps)
        if abs(duration - expected_duration) > tolerance:
            errors.append(
                f"duration {duration:.4f}s != {expected_duration:.4f}s"
            )
        if black_seconds >= max(1.0, duration * 0.80):
            errors.append(
                f"video is mostly black ({black_seconds:.3f}s of {duration:.3f}s)"
            )
        if any(not item.valid for item in validations):
            errors.append("one or more trimmed segments are invalid")
        expected_order = [item.segment_id for item in self.plan.segments]
        if state.segment_order != expected_order:
            errors.append("persisted segment order differs from GenerationPlan")
        return FullEpisodeValidationReport.build_with_digest(
            run_id=state.run_id,
            generation_plan_digest=self.plan.content_digest,
            output_file=str(self.output_file),
            output_sha256=file_sha256(self.output_file),
            width=max(1, width),
            height=max(1, height),
            fps=max(0.0001, fps),
            duration_seconds=max(0.0001, duration),
            expected_frame_count=self.plan.target_frame_count,
            actual_frame_count=max(1, actual_frames),
            video_streams=len(videos),
            audio_streams=len(audios),
            black_duration_seconds=max(0.0, black_seconds),
            segment_order=expected_order,
            segment_validations=validations,
            external_api_calls=external_api_calls,
            valid=not errors,
            errors=errors,
        )

    def _load_or_create_state(self) -> FullEpisodeRunState:
        if self.state_file.is_file():
            state = FullEpisodeRunState.model_validate_json(
                self.state_file.read_text(encoding="utf-8")
            )
            conflicts: list[str] = []
            if state.generation_plan_digest != self.plan.content_digest:
                conflicts.append("generation plan digest")
            if state.prepared_episode_digest != self.plan.source_prepared_episode_digest:
                conflicts.append("prepared episode digest")
            if state.asset_bundle_digest != self.asset_bundle_digest:
                conflicts.append("asset bundle digest")
            if state.execution_budget_digest != self.execution_budget_digest:
                conflicts.append("execution budget digest")
            if state.output_file != str(self.output_file):
                conflicts.append("output file")
            if state.target_fps != self.plan.timeline_fps:
                conflicts.append("target fps")
            if state.target_frame_count != self.plan.target_frame_count:
                conflicts.append("target frame count")
            if (state.target_width, state.target_height) != (
                self.target_width,
                self.target_height,
            ):
                conflicts.append("target dimensions")
            if conflicts:
                raise FullEpisodeStateConflictError(
                    "existing full-episode state belongs to another run: "
                    + ", ".join(conflicts)
                )
            return state

        segment_order = [item.segment_id for item in self.plan.segments]
        segments = {
            item.segment_id: FullEpisodeSegmentState(
                segment_id=item.segment_id,
                order=item.order,
                editorial_start_frame=item.editorial_start_frame,
                editorial_end_frame=item.editorial_end_frame,
                editorial_frame_count=item.editorial_frame_count,
                requested_duration_seconds=item.requested_duration_seconds,
                used_start_frame=item.used_start_frame,
                used_end_frame=item.used_end_frame,
            )
            for item in self.plan.segments
        }
        run_id = "full_episode_" + self.plan.content_digest.removeprefix("sha256:")[:16]
        state = FullEpisodeRunState(
            run_id=run_id,
            generation_plan_digest=self.plan.content_digest,
            prepared_episode_digest=self.plan.source_prepared_episode_digest,
            asset_bundle_digest=self.asset_bundle_digest,
            execution_budget_digest=self.execution_budget_digest,
            target_fps=self.plan.timeline_fps,
            target_frame_count=self.plan.target_frame_count,
            target_width=self.target_width,
            target_height=self.target_height,
            output_file=str(self.output_file),
            segment_order=segment_order,
            segments=segments,
        )
        self._write_state(state)
        return state

    def _write_state(self, state: FullEpisodeRunState) -> None:
        state.updated_at = datetime.now(timezone.utc)
        self._atomic_write(self.state_file, state.to_canonical_json())

    def _reset(self) -> None:
        for path in (
            self.state_file,
            self.report_file,
            self.concat_file,
            self.concat_raw_file,
            self.output_file,
        ):
            if path.exists():
                path.unlink()
        if self.segment_dir.exists():
            for child in self.segment_dir.iterdir():
                if child.is_file():
                    child.unlink()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _count_frames(path: Path) -> int:
        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "default=nokey=1:noprint_wrappers=1",
                str(path),
            ]
        )
        value = result.stdout.strip()
        try:
            return int(value)
        except ValueError as exc:
            raise FullEpisodeValidationError(
                f"cannot determine video frame count for {path}: {value!r}"
            ) from exc

    @staticmethod
    def _parse_rate(value: object) -> float:
        text = str(value or "0")
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            try:
                denominator_value = float(denominator)
                return float(numerator) / denominator_value if denominator_value else 0.0
            except ValueError:
                return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
