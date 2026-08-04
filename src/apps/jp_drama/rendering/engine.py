"""Resumable dependency-ordered execution engine for Japanese-drama RenderGraph."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable

from ..preparation.models import PreparedEpisode, RenderTaskNode
from .ffmpeg import (
    black_duration,
    canonical_digest,
    ffmpeg,
    ffprobe_json,
    file_sha256,
    require_ffmpeg,
)
from .mock_tasks import MockTaskExecutor, TaskContext
from .models import (
    RenderRunState,
    RenderValidationReport,
    ShotExecutionState,
    TaskExecutionState,
)


class RenderExecutionError(RuntimeError):
    """Base error for PR6 execution failures."""


class RenderStateConflictError(RenderExecutionError):
    """Existing state belongs to a different input or graph."""


class RenderTaskFailedError(RenderExecutionError):
    """A RenderGraph task failed and its state was persisted."""


class RenderValidationError(RenderExecutionError):
    """The final MP4 failed one or more completion checks."""


class RenderGraphRunner:
    """Execute a PreparedEpisode graph with restart-safe task state."""

    def __init__(
        self,
        prepared: PreparedEpisode,
        *,
        output_file: str | Path,
        work_dir: str | Path,
        executor: MockTaskExecutor | None = None,
        persistence_status: str | None = None,
    ) -> None:
        self.prepared = prepared
        self.output_file = Path(output_file).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.state_file = self.work_dir / "render_state.json"
        self.report_file = self.work_dir / "validation_report.json"
        self.executor = executor or MockTaskExecutor()
        self.persistence_status = persistence_status
        self.frames_by_shot = {
            frame.source_shot_id: frame
            for frame in prepared.storyboard_frame_drafts
        }
        self.nodes_by_id = {
            node.task_id: node
            for node in prepared.render_graph.nodes
        }
        self.task_order = self._topological_order(prepared.render_graph.nodes)
        self.graph_fingerprint = canonical_digest(
            [
                prepared.source_digest,
                json.dumps(
                    prepared.render_graph.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )

    def run(self, *, reset: bool = False) -> RenderValidationReport:
        if not self.prepared.readiness_report.generation_ready:
            raise RenderExecutionError("PreparedEpisode is not generation-ready")
        if self.prepared.readiness_report.external_api_calls != 0:
            raise RenderExecutionError("PreparedEpisode unexpectedly reports external API calls")
        require_ffmpeg()

        if reset:
            self._reset_outputs()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.output_file.parent.mkdir(parents=True, exist_ok=True)

        state = self._load_or_create_state()
        self._normalise_resumable_state(state)
        self._write_state(state)

        for task_id in self.task_order:
            node = self.nodes_by_id[task_id]
            task_state = state.task_states[task_id]
            if task_state.status == "succeeded" and self._outputs_exist(task_state.output_files):
                continue

            dependencies = [state.task_states[dependency] for dependency in node.depends_on]
            unresolved = [dependency.task_id for dependency in dependencies if dependency.status != "succeeded"]
            if unresolved:
                raise RenderExecutionError(
                    f"task {task_id} has unresolved dependencies: {unresolved}"
                )

            frame = self.frames_by_shot.get(node.shot_id)
            if frame is None:
                raise RenderExecutionError(f"task {task_id} references unknown shot {node.shot_id}")

            task_state.status = "running"
            task_state.attempts += 1
            task_state.last_error = None
            self._refresh_shot_states(state)
            self._write_state(state)

            dependency_outputs = [
                self.work_dir / relative
                for dependency in dependencies
                for relative in dependency.output_files
            ]
            try:
                outputs = self.executor.execute(
                    TaskContext(
                        prepared=self.prepared,
                        frame=frame,
                        node=node,
                        work_dir=self.work_dir,
                        dependency_outputs=dependency_outputs,
                    )
                )
            except Exception as exc:
                task_state.status = "failed"
                task_state.last_error = f"{type(exc).__name__}: {exc}"
                task_state.output_files = []
                self._refresh_shot_states(state)
                self._write_state(state)
                raise RenderTaskFailedError(
                    f"task {task_id} failed; rerun the same command to resume: {exc}"
                ) from exc

            task_state.status = "succeeded"
            task_state.last_error = None
            task_state.output_files = [self._relative_output(path) for path in outputs]
            self._refresh_shot_states(state)
            self._write_state(state)

        final_shots = self._final_shot_files(state)
        composition_fingerprint = canonical_digest(
            [
                self.prepared.source_digest,
                self.graph_fingerprint,
                *[file_sha256(path) for path in final_shots],
            ]
        )
        if (
            not self.output_file.exists()
            or self.output_file.stat().st_size == 0
            or state.final_output_fingerprint != composition_fingerprint
        ):
            self._concatenate(final_shots)
            state.final_output_fingerprint = composition_fingerprint
            self._write_state(state)

        report = self.validate(state)
        self._atomic_write(self.report_file, report.to_canonical_json())
        if not report.valid:
            raise RenderValidationError("; ".join(report.errors))
        return report

    def validate(self, state: RenderRunState | None = None) -> RenderValidationReport:
        active_state = state or self._load_state()
        if not self.output_file.exists():
            raise RenderValidationError(f"final output does not exist: {self.output_file}")

        probe = ffprobe_json(self.output_file)
        streams = probe.get("streams", [])
        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        first_video = video_streams[0] if video_streams else {}
        width = int(first_video.get("width") or 0)
        height = int(first_video.get("height") or 0)
        fps = self._parse_rate(first_video.get("avg_frame_rate") or first_video.get("r_frame_rate"))
        duration = float(probe.get("format", {}).get("duration") or 0.0)
        black_seconds = black_duration(self.output_file)
        subtitle_artifacts = sum(
            1
            for task in active_state.task_states.values()
            for relative in task.output_files
            if Path(relative).suffix.lower() in {".ass", ".srt"}
        )

        errors: list[str] = []
        target = float(self.prepared.project_draft.target_duration_seconds)
        expected_order = [
            frame.source_shot_id
            for frame in sorted(self.prepared.storyboard_frame_drafts, key=lambda item: item.order)
        ]
        if not video_streams:
            errors.append("video stream is missing")
        if not audio_streams:
            errors.append("audio stream is missing")
        if width <= 0 or height <= 0 or width * 16 != height * 9:
            errors.append(f"output is not 9:16: {width}x{height}")
        if abs(duration - target) > 0.75:
            errors.append(f"duration {duration:.3f}s differs from target {target:.3f}s")
        if fps <= 0 or abs(fps - self.prepared.project_draft.fps) > 0.1:
            errors.append(
                f"fps {fps:.3f} differs from target {self.prepared.project_draft.fps}"
            )
        if black_seconds >= max(1.0, duration * 0.80):
            errors.append(f"video is mostly black ({black_seconds:.3f}s of {duration:.3f}s)")
        if subtitle_artifacts == 0:
            errors.append("no subtitle artifact was generated")
        if active_state.final_shot_order != expected_order:
            errors.append(
                f"shot order mismatch: {active_state.final_shot_order} != {expected_order}"
            )
        failed = [
            task.task_id
            for task in active_state.task_states.values()
            if task.status != "succeeded"
        ]
        if failed:
            errors.append(f"unfinished tasks remain: {failed}")

        return RenderValidationReport(
            output_file=str(self.output_file),
            width=width,
            height=height,
            aspect_ratio="9:16" if width > 0 and width * 16 == height * 9 else "invalid",
            fps=fps,
            duration_seconds=duration,
            video_streams=len(video_streams),
            audio_streams=len(audio_streams),
            black_duration_seconds=black_seconds,
            subtitle_artifacts=subtitle_artifacts,
            shot_order=active_state.final_shot_order,
            source_digest=self.prepared.source_digest,
            graph_fingerprint=self.graph_fingerprint,
            external_api_calls=0,
            valid=not errors,
            errors=errors,
        )

    def _load_or_create_state(self) -> RenderRunState:
        if self.state_file.exists():
            state = self._load_state()
            conflicts = []
            if state.source_digest != self.prepared.source_digest:
                conflicts.append("source digest")
            if state.project_id != self.prepared.project_draft.project_id:
                conflicts.append("project ID")
            if state.graph_fingerprint != self.graph_fingerprint:
                conflicts.append("render graph")
            if Path(state.output_file).resolve() != self.output_file:
                conflicts.append("output file")
            if conflicts:
                raise RenderStateConflictError(
                    "existing work state belongs to another run: " + ", ".join(conflicts)
                )
            if state.persistence_status is None and self.persistence_status is not None:
                state.persistence_status = self.persistence_status
            return state

        shot_states: dict[str, ShotExecutionState] = {}
        ordered_frames = sorted(self.prepared.storyboard_frame_drafts, key=lambda item: item.order)
        for frame in ordered_frames:
            task_ids = [
                node.task_id
                for node in self.prepared.render_graph.nodes
                if node.shot_id == frame.source_shot_id
            ]
            shot_states[frame.source_shot_id] = ShotExecutionState(
                shot_id=frame.source_shot_id,
                order=frame.order,
                task_ids=task_ids,
            )

        task_states = {
            node.task_id: TaskExecutionState(
                task_id=node.task_id,
                shot_id=node.shot_id,
                task_type=node.task_type,
                input_fingerprint=canonical_digest(
                    [
                        self.prepared.source_digest,
                        json.dumps(
                            node.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ]
                ),
            )
            for node in self.prepared.render_graph.nodes
        }
        return RenderRunState(
            source_digest=self.prepared.source_digest,
            project_id=self.prepared.project_draft.project_id,
            output_file=str(self.output_file),
            graph_fingerprint=self.graph_fingerprint,
            task_order=self.task_order,
            final_shot_order=[frame.source_shot_id for frame in ordered_frames],
            task_states=task_states,
            shot_states=shot_states,
            persistence_status=self.persistence_status,
        )

    def _normalise_resumable_state(self, state: RenderRunState) -> None:
        invalid: set[str] = set()
        for task_id in self.task_order:
            node = self.nodes_by_id[task_id]
            task = state.task_states[task_id]
            if task.status == "running":
                task.status = "failed"
                task.last_error = "interrupted before completion"
            dependency_invalid = any(dependency in invalid for dependency in node.depends_on)
            if task.status == "succeeded" and not dependency_invalid and self._outputs_exist(task.output_files):
                continue
            if task.status == "failed" and not dependency_invalid:
                task.status = "pending"
            else:
                task.status = "pending"
            task.output_files = []
            invalid.add(task_id)
        self._refresh_shot_states(state)

    def _refresh_shot_states(self, state: RenderRunState) -> None:
        for shot in state.shot_states.values():
            tasks = [state.task_states[task_id] for task_id in shot.task_ids]
            if any(task.status == "failed" for task in tasks):
                shot.status = "failed"
            elif tasks and all(task.status == "succeeded" for task in tasks):
                shot.status = "succeeded"
            elif any(task.status in {"running", "succeeded"} for task in tasks):
                shot.status = "running"
            else:
                shot.status = "pending"
            final_tasks = [task for task in tasks if task.task_type == "finalize_shot"]
            if final_tasks and final_tasks[0].status == "succeeded":
                videos = [
                    self.work_dir / relative
                    for relative in final_tasks[0].output_files
                    if Path(relative).suffix.lower() == ".mp4"
                ]
                shot.final_video = str(videos[0]) if videos else None
            else:
                shot.final_video = None

    def _final_shot_files(self, state: RenderRunState) -> list[Path]:
        files: list[Path] = []
        for shot_id in state.final_shot_order:
            shot = state.shot_states[shot_id]
            if shot.status != "succeeded" or not shot.final_video:
                raise RenderExecutionError(f"shot {shot_id} has no finalized video")
            path = Path(shot.final_video)
            if not path.exists():
                raise RenderExecutionError(f"finalized shot file is missing: {path}")
            files.append(path)
        return files

    def _concatenate(self, final_shots: list[Path]) -> None:
        concat_file = self.work_dir / "concat.txt"
        lines = []
        for path in final_shots:
            escaped = str(path.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        self._atomic_write(concat_file, "\n".join(lines) + "\n")
        temporary = self.output_file.with_name(
            f".{self.output_file.stem}.tmp{self.output_file.suffix}"
        )
        ffmpeg(
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            str(temporary),
        )
        os.replace(temporary, self.output_file)

    def _topological_order(self, nodes: Iterable[RenderTaskNode]) -> list[str]:
        indexed = list(nodes)
        remaining = {node.task_id: set(node.depends_on) for node in indexed}
        order_index = {node.task_id: index for index, node in enumerate(indexed)}
        resolved: set[str] = set()
        ordered: list[str] = []
        while remaining:
            ready = sorted(
                (task_id for task_id, dependencies in remaining.items() if dependencies <= resolved),
                key=order_index.__getitem__,
            )
            if not ready:
                raise RenderExecutionError("render graph contains a cycle")
            task_id = ready[0]
            ordered.append(task_id)
            resolved.add(task_id)
            remaining.pop(task_id)
        return ordered

    def _outputs_exist(self, outputs: list[str]) -> bool:
        return bool(outputs) and all(
            (self.work_dir / relative).exists()
            and (self.work_dir / relative).stat().st_size > 0
            for relative in outputs
        )

    def _relative_output(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return str(resolved.relative_to(self.work_dir))
        except ValueError as exc:
            raise RenderExecutionError(
                f"task output must stay inside the work directory: {resolved}"
            ) from exc

    def _load_state(self) -> RenderRunState:
        try:
            return RenderRunState.model_validate_json(
                self.state_file.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise RenderStateConflictError(f"cannot read render state: {exc}") from exc

    def _write_state(self, state: RenderRunState) -> None:
        self._atomic_write(self.state_file, state.to_canonical_json())

    def _reset_outputs(self) -> None:
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        if self.output_file.exists():
            self.output_file.unlink()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _parse_rate(value: str | None) -> float:
        if not value or value == "0/0":
            return 0.0
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            return float(numerator) / float(denominator)
        return float(value)
