"""Zero-provider-call exact-frame composition for production segment artifacts."""

from __future__ import annotations

import json
import os
import shutil
from fractions import Fraction
from pathlib import Path

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
    EpisodeComposeReport,
    SegmentArtifact,
    SegmentArtifactManifest,
    SegmentComposeValidation,
)


class ProductionComposeError(RuntimeError):
    """The artifact set cannot be composed without violating the plan contract."""


class ProductionEpisodeComposer:
    """Verify, trim, and concatenate existing segment MP4s without provider calls."""

    def __init__(
        self,
        plan: GenerationPlanEpisode,
        manifest: SegmentArtifactManifest,
        *,
        output_file: str | Path,
        work_dir: str | Path,
        target_width: int = 720,
        target_height: int = 1280,
    ) -> None:
        if not plan.readiness_report.planning_ready:
            raise ProductionComposeError("GenerationPlan is not planning-ready")
        if manifest.generation_plan_digest != plan.content_digest:
            raise ProductionComposeError("segment manifest belongs to another GenerationPlan")
        if target_width <= 0 or target_height <= 0:
            raise ValueError("target dimensions must be positive")
        if target_width * 16 != target_height * 9:
            raise ValueError("target dimensions must be 9:16")
        self.plan = plan
        self.manifest = manifest
        self.output_file = Path(output_file).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.segment_dir = self.work_dir / "segments"
        self.concat_file = self.work_dir / "concat.txt"
        self.concat_raw_file = self.work_dir / "episode_concat_raw.mp4"
        self.report_file = self.work_dir / "production_compose_report.json"
        self.target_width = target_width
        self.target_height = target_height
        self.artifacts = {item.segment_id: item for item in manifest.artifacts}

    def compose(self, *, reset: bool = False) -> EpisodeComposeReport:
        require_ffmpeg()
        expected = [item.segment_id for item in self.plan.segments]
        missing = [item for item in expected if item not in self.artifacts]
        extra = sorted(set(self.artifacts) - set(expected))
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if extra:
                details.append("unexpected=" + ",".join(extra))
            raise ProductionComposeError(
                "segment artifact set does not match GenerationPlan: " + "; ".join(details)
            )
        if reset and self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.segment_dir.mkdir(parents=True, exist_ok=True)
        self.output_file.parent.mkdir(parents=True, exist_ok=True)

        validations: list[SegmentComposeValidation] = []
        for segment in self.plan.segments:
            artifact = self.artifacts[segment.segment_id]
            validations.append(self._prepare_segment(segment, artifact))

        invalid = [item for item in validations if not item.valid]
        if invalid:
            raise ProductionComposeError(
                "invalid segment artifacts: "
                + "; ".join(
                    f"{item.segment_id}: {', '.join(item.errors)}" for item in invalid
                )
            )

        self._concatenate(validations)
        report = self._validate_episode(validations)
        self._atomic_write(self.report_file, report.to_canonical_json())
        if not report.valid:
            raise ProductionComposeError("; ".join(report.errors))
        return report

    def _prepare_segment(
        self,
        segment: GenerationSegment,
        artifact: SegmentArtifact,
    ) -> SegmentComposeValidation:
        errors: list[str] = []
        source = Path(artifact.output_path).resolve()
        trimmed = self.segment_dir / f"{segment.order:03d}_{segment.segment_id}.mp4"

        if not artifact.valid:
            errors.extend(artifact.errors or ["artifact marked invalid"])
        if artifact.provider_route_id != segment.provider_route_id:
            errors.append(
                f"provider route {artifact.provider_route_id} != {segment.provider_route_id}"
            )
        if not source.is_file() or source.stat().st_size == 0:
            errors.append(f"source MP4 is missing or empty: {source}")
            return self._invalid_validation(segment, artifact, source, trimmed, errors)
        actual_source_sha = file_sha256(source)
        if actual_source_sha != artifact.output_sha256:
            errors.append("source MP4 SHA-256 differs from SegmentArtifact")

        probe = ffprobe_json(source)
        streams = probe.get("streams", [])
        videos = [item for item in streams if item.get("codec_type") == "video"]
        audios = [item for item in streams if item.get("codec_type") == "audio"]
        video = videos[0] if videos else {}
        source_width = int(video.get("width") or 0)
        source_height = int(video.get("height") or 0)
        source_fps = self._parse_rate(
            video.get("avg_frame_rate") or video.get("r_frame_rate")
        )
        source_duration = float(probe.get("format", {}).get("duration") or 0.0)
        source_frames = self._count_frames(source) if videos else 0
        if not videos:
            errors.append("source MP4 has no video stream")
        if source_width != artifact.width or source_height != artifact.height:
            errors.append(
                f"artifact dimensions {artifact.width}x{artifact.height} "
                f"!= source {source_width}x{source_height}"
            )
        if abs(source_fps - artifact.fps) > 0.05:
            errors.append(f"artifact fps {artifact.fps} != source {source_fps:.4f}")
        if source_frames != artifact.frame_count:
            errors.append(
                f"artifact frame_count {artifact.frame_count} != source {source_frames}"
            )
        tolerance = max(0.08, 2 / segment.timeline_fps)
        if abs(source_duration - artifact.duration_seconds) > tolerance:
            errors.append(
                f"artifact duration {artifact.duration_seconds:.4f}s "
                f"!= source {source_duration:.4f}s"
            )
        if bool(audios) != artifact.audio_present:
            errors.append("artifact audio_present does not match source MP4")
        required_end = segment.used_end_frame / segment.timeline_fps
        if source_duration + tolerance < required_end:
            errors.append(
                f"source duration {source_duration:.4f}s is shorter than "
                f"required editorial window end {required_end:.4f}s"
            )
        if errors:
            return self._invalid_validation(segment, artifact, source, trimmed, errors)

        self._trim_segment(segment, source, trimmed)
        return self._validate_trimmed(segment, source, trimmed)

    def _invalid_validation(
        self,
        segment: GenerationSegment,
        artifact: SegmentArtifact,
        source: Path,
        trimmed: Path,
        errors: list[str],
    ) -> SegmentComposeValidation:
        return SegmentComposeValidation(
            segment_id=segment.segment_id,
            source_file=str(source),
            trimmed_file=str(trimmed),
            source_sha256=(
                file_sha256(source)
                if source.is_file() and source.stat().st_size > 0
                else artifact.output_sha256
            ),
            trimmed_sha256=artifact.output_sha256,
            expected_frames=segment.editorial_frame_count,
            actual_frames=max(1, artifact.frame_count),
            expected_duration_seconds=(
                segment.editorial_frame_count / segment.timeline_fps
            ),
            actual_duration_seconds=max(0.001, artifact.duration_seconds),
            width=max(1, artifact.width),
            height=max(1, artifact.height),
            fps=max(0.001, artifact.fps),
            video_streams=0,
            audio_streams=1 if artifact.audio_present else 0,
            valid=False,
            errors=errors,
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
            f"trim=start_frame={segment.used_start_frame}:"
            f"end_frame={segment.used_end_frame},"
            "setpts=PTS-STARTPTS,"
            f"scale={self.target_width}:{self.target_height}:"
            "force_original_aspect_ratio=increase,"
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

    def _validate_trimmed(
        self,
        segment: GenerationSegment,
        source: Path,
        trimmed: Path,
    ) -> SegmentComposeValidation:
        probe = ffprobe_json(trimmed)
        streams = probe.get("streams", [])
        videos = [item for item in streams if item.get("codec_type") == "video"]
        audios = [item for item in streams if item.get("codec_type") == "audio"]
        video = videos[0] if videos else {}
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        fps = self._parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        duration = float(probe.get("format", {}).get("duration") or 0.0)
        frames = self._count_frames(trimmed) if videos else 0
        expected_duration = segment.editorial_frame_count / segment.timeline_fps
        errors: list[str] = []
        if not videos:
            errors.append("trimmed segment has no video stream")
        if not audios:
            errors.append("trimmed segment has no audio stream")
        if (width, height) != (self.target_width, self.target_height):
            errors.append(
                f"dimensions {width}x{height} != "
                f"{self.target_width}x{self.target_height}"
            )
        if abs(fps - segment.timeline_fps) > 0.01:
            errors.append(f"fps {fps:.4f} != {segment.timeline_fps}")
        if frames != segment.editorial_frame_count:
            errors.append(f"frame count {frames} != {segment.editorial_frame_count}")
        tolerance = max(0.08, 2 / segment.timeline_fps)
        if abs(duration - expected_duration) > tolerance:
            errors.append(
                f"duration {duration:.4f}s != {expected_duration:.4f}s"
            )
        return SegmentComposeValidation(
            segment_id=segment.segment_id,
            source_file=str(source),
            trimmed_file=str(trimmed),
            source_sha256=file_sha256(source),
            trimmed_sha256=file_sha256(trimmed),
            expected_frames=segment.editorial_frame_count,
            actual_frames=max(1, frames),
            expected_duration_seconds=expected_duration,
            actual_duration_seconds=max(0.001, duration),
            width=max(1, width),
            height=max(1, height),
            fps=max(0.001, fps),
            video_streams=len(videos),
            audio_streams=len(audios),
            valid=not errors,
            errors=errors,
        )

    def _concatenate(self, validations: list[SegmentComposeValidation]) -> None:
        lines: list[str] = []
        for item in validations:
            path = Path(item.trimmed_file).resolve()
            escaped = str(path).replace("'", "'\\''")
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
        validations: list[SegmentComposeValidation],
    ) -> EpisodeComposeReport:
        probe = ffprobe_json(self.output_file)
        streams = probe.get("streams", [])
        videos = [item for item in streams if item.get("codec_type") == "video"]
        audios = [item for item in streams if item.get("codec_type") == "audio"]
        video = videos[0] if videos else {}
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        fps = self._parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        duration = float(probe.get("format", {}).get("duration") or 0.0)
        frames = self._count_frames(self.output_file) if videos else 0
        black = black_duration(self.output_file)
        expected_duration = self.plan.target_frame_count / self.plan.timeline_fps
        errors: list[str] = []
        if not videos:
            errors.append("final output has no video stream")
        if not audios:
            errors.append("final output has no audio stream")
        if (width, height) != (self.target_width, self.target_height):
            errors.append(
                f"dimensions {width}x{height} != "
                f"{self.target_width}x{self.target_height}"
            )
        if abs(fps - self.plan.timeline_fps) > 0.01:
            errors.append(f"fps {fps:.4f} != {self.plan.timeline_fps}")
        if frames != self.plan.target_frame_count:
            errors.append(f"frame count {frames} != {self.plan.target_frame_count}")
        tolerance = max(0.08, 2 / self.plan.timeline_fps)
        if abs(duration - expected_duration) > tolerance:
            errors.append(f"duration {duration:.4f}s != {expected_duration:.4f}s")
        if black > 0.25:
            errors.append(f"black-frame duration {black:.4f}s exceeds 0.25s")
        return EpisodeComposeReport.build_with_digest(
            generation_plan_digest=self.plan.content_digest,
            segment_manifest_digest=self.manifest.content_digest,
            output_file=str(self.output_file),
            output_sha256=file_sha256(self.output_file),
            width=max(1, width),
            height=max(1, height),
            fps=max(0.001, fps),
            duration_seconds=max(0.001, duration),
            expected_frame_count=self.plan.target_frame_count,
            actual_frame_count=max(1, frames),
            video_streams=len(videos),
            audio_streams=len(audios),
            black_duration_seconds=max(0.0, black),
            segment_order=[item.segment_id for item in self.plan.segments],
            segment_validations=validations,
            external_api_calls=0,
            valid=not errors,
            errors=errors,
        )

    @staticmethod
    def _parse_rate(value: object) -> float:
        if not value:
            return 0.0
        try:
            return float(Fraction(str(value)))
        except (ValueError, ZeroDivisionError):
            return 0.0

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
        if not value or value == "N/A":
            return 0
        return int(value)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
