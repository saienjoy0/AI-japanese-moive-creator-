"""Wan 2.7 live executor with persistent cost limits and approval gates."""

from __future__ import annotations

import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from ..preparation.models import PreparedEpisode
from .ffmpeg import ffmpeg, file_sha256
from .live_tasks import LiveTaskExecutor
from .mock_tasks import TaskContext
from .provider_config import LiveProviderConfig, ProviderConfigurationError
from .provider_ledger import (
    CanaryProviderLedger,
    CanaryProviderLedgerStore,
    ProviderLedgerError,
)


class ProviderCallLimitError(ProviderConfigurationError):
    """A canary run attempted to exceed its persistent paid-call or cost ceiling."""


class Wan27LiveTaskExecutor(LiveTaskExecutor):
    """Wan 2.7 executor with restart-safe submissions and approved keyframes."""

    def __init__(
        self,
        config: LiveProviderConfig,
        *,
        image_model: Any | None = None,
        video_model: Any | None = None,
        tts_processor: Any | None = None,
        require_credentials: bool = True,
        api_call_limit: int | None = None,
        approved_keyframes: dict[str, str | Path] | None = None,
        ledger_store: CanaryProviderLedgerStore | None = None,
        ledger: CanaryProviderLedger | None = None,
    ) -> None:
        if api_call_limit is not None and api_call_limit < 0:
            raise ValueError("api_call_limit must be zero or greater")
        if (ledger_store is None) != (ledger is None):
            raise ValueError("ledger_store and ledger must be configured together")
        self.api_call_limit = api_call_limit
        self.ledger_store = ledger_store
        self.ledger = ledger
        self.approved_keyframes = {
            shot_id: Path(path).resolve()
            for shot_id, path in (approved_keyframes or {}).items()
        }
        missing = [str(path) for path in self.approved_keyframes.values() if not path.is_file()]
        if missing:
            raise ProviderConfigurationError(
                "approved keyframe does not exist: " + ", ".join(missing)
            )
        super().__init__(
            config,
            image_model=image_model,
            video_model=video_model,
            tts_processor=tts_processor,
            require_credentials=require_credentials,
        )
        if self.ledger is not None:
            self._external_api_calls = self.ledger.committed_api_calls

    @property
    def execution_profile(self) -> str:
        assets = {
            shot_id: file_sha256(path)
            for shot_id, path in sorted(self.approved_keyframes.items())
        }
        payload = json.dumps(
            {
                "base": self.config.execution_profile,
                "approved_keyframes": assets,
                "api_call_limit": self.api_call_limit,
                "ledger_path": str(self.ledger_store.path) if self.ledger_store else None,
                "protocol": "wan27-official-v2",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"live:wan27:sha256:{hashlib.sha256(payload).hexdigest()}"

    @property
    def provider_manifest(self) -> dict[str, str]:
        manifest = dict(self.config.provider_manifest)
        manifest["protocol"] = "wan27-official-v2"
        manifest["approved_keyframes"] = str(len(self.approved_keyframes))
        if self.api_call_limit is not None:
            manifest["api_call_limit"] = str(self.api_call_limit)
        return manifest

    @staticmethod
    def estimate_api_calls(
        prepared: PreparedEpisode,
        *,
        approved_keyframe_shots: Iterable[str] = (),
    ) -> int:
        approved = set(approved_keyframe_shots)
        frames = {frame.source_shot_id: frame for frame in prepared.storyboard_frame_drafts}
        total = 0
        for node in prepared.render_graph.nodes:
            frame = frames[node.shot_id]
            if node.task_type == "generate_image":
                total += 1
            elif node.task_type == "generate_video":
                total += 1 if node.shot_id in approved else 2
            elif node.task_type == "generate_tts":
                total += len(frame.dialogue_cues)
            elif node.task_type == "generate_native_av":
                total += (1 if node.shot_id in approved else 2) + len(frame.dialogue_cues)
        return total

    @staticmethod
    def estimate_cost_cny(
        prepared: PreparedEpisode,
        config: LiveProviderConfig,
        *,
        approved_keyframe_shots: Iterable[str] = (),
    ) -> Decimal:
        approved = set(approved_keyframe_shots)
        provider = config.dashscope
        frames = {frame.source_shot_id: frame for frame in prepared.storyboard_frame_drafts}
        total = Decimal("0")
        for node in prepared.render_graph.nodes:
            frame = frames[node.shot_id]
            if node.task_type == "generate_image":
                total += provider.estimate_image_cost_cny()
            elif node.task_type == "generate_video":
                if node.shot_id not in approved:
                    total += provider.estimate_image_cost_cny()
                total += provider.estimate_video_cost_cny(
                    min(provider.provider_clip_seconds, frame.duration_seconds)
                )
            elif node.task_type == "generate_tts":
                total += sum(
                    (provider.estimate_tts_cost_cny(len(cue.text)) for cue in frame.dialogue_cues),
                    start=Decimal("0"),
                )
            elif node.task_type == "generate_native_av":
                if node.shot_id not in approved:
                    total += provider.estimate_image_cost_cny()
                total += provider.estimate_video_cost_cny(
                    min(provider.provider_clip_seconds, frame.duration_seconds)
                )
                total += sum(
                    (provider.estimate_tts_cost_cny(len(cue.text)) for cue in frame.dialogue_cues),
                    start=Decimal("0"),
                )
        return total

    def _ensure_clients(self) -> None:
        provider = self.config.dashscope
        if self.image_model is None or self.video_model is None or self.tts_processor is None:
            provider.configure_runtime()
        if self.image_model is None:
            from .wan27_adapters import Wan27ImageModel

            self.image_model = Wan27ImageModel(
                {"params": {"thinking_mode": provider.image_thinking_mode}}
            )
        if self.video_model is None:
            from .wan27_adapters import Wan27VideoModel

            self.video_model = Wan27VideoModel(
                {"params": {"resolution": provider.video_resolution}}
            )
        if self.tts_processor is None:
            try:
                from src.audio.tts import TTSProcessor
            except Exception as exc:  # pragma: no cover
                raise ProviderConfigurationError(
                    "cannot import the LumenX TTS provider; install requirements.txt"
                ) from exc
            self.tts_processor = TTSProcessor(
                model=provider.tts_model,
                voice=provider.default_voice,
            )

    def _provider_call(
        self,
        operation: Callable[..., Any],
        /,
        *args: Any,
        _operation_id: str | None = None,
        _stage: Literal["keyframe", "render"] = "render",
        _operation_type: Literal["image", "video", "tts"] = "video",
        _model: str = "unknown",
        _estimated_cost_cny: Decimal = Decimal("0"),
        **kwargs: Any,
    ) -> Any:
        if self.ledger is None or self.ledger_store is None or _operation_id is None:
            if self.api_call_limit is not None and self._external_api_calls >= self.api_call_limit:
                raise ProviderCallLimitError(
                    f"external API call limit reached ({self.api_call_limit}); no request submitted"
                )
            self._external_api_calls += 1
            return operation(*args, **kwargs)

        try:
            record, created = self.ledger_store.begin(
                self.ledger,
                operation_id=_operation_id,
                stage=_stage,
                operation_type=_operation_type,
                provider=self.config.dashscope.provider,
                model=_model,
                estimated_cost_cny=_estimated_cost_cny,
            )
        except ProviderLedgerError as exc:
            raise ProviderCallLimitError(str(exc)) from exc

        self._external_api_calls = self.ledger.committed_api_calls
        adapter = getattr(operation, "__self__", None)
        if not created and record.status == "succeeded":
            raise ProviderConfigurationError(
                f"provider operation {_operation_id} already succeeded; "
                "restore its recorded artifact instead of submitting again"
            )
        if not created and record.provider_task_id is None:
            raise ProviderConfigurationError(
                f"provider operation {_operation_id} has an uncertain prior submission "
                "without a task ID; refusing a duplicate paid request"
            )

        if hasattr(adapter, "configure_operation"):
            adapter.configure_operation(
                resume_task_id=None if created else record.provider_task_id,
                on_task_submitted=lambda task_id, request_id: self.ledger_store.mark_submitted(
                    self.ledger,
                    _operation_id,
                    provider_task_id=task_id,
                    provider_request_id=request_id,
                ),
            )

        try:
            result = operation(*args, **kwargs)
        except Exception as exc:
            self.ledger_store.mark_unknown(
                self.ledger,
                _operation_id,
                f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            if hasattr(adapter, "clear_operation"):
                adapter.clear_operation()

        output_sha256 = None
        if len(args) >= 2:
            candidate = Path(str(args[1]))
            if candidate.is_file() and candidate.stat().st_size > 0:
                output_sha256 = file_sha256(candidate)
        self.ledger_store.mark_succeeded(
            self.ledger,
            _operation_id,
            output_sha256=output_sha256,
        )
        return result

    def generate_canary_keyframe(
        self,
        prepared: PreparedEpisode,
        *,
        shot_id: str,
        output: str | Path,
    ) -> Path:
        frame = next(
            (item for item in prepared.storyboard_frame_drafts if item.source_shot_id == shot_id),
            None,
        )
        node = next(
            (
                item
                for item in prepared.render_graph.nodes
                if item.shot_id == shot_id
                and item.task_type in {"generate_video", "generate_native_av", "generate_image"}
            ),
            None,
        )
        if frame is None or node is None:
            raise ProviderConfigurationError(f"shot cannot generate a keyframe: {shot_id}")
        self._ensure_clients()
        destination = Path(output).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        context = TaskContext(
            prepared=prepared,
            frame=frame,
            node=node,
            work_dir=destination.parent,
            dependency_outputs=[],
        )
        provider = self.config.dashscope
        self._provider_call(
            self.image_model.generate,
            self._visual_prompt(context),
            str(destination),
            model_name=provider.image_model,
            size=provider.image_size,
            seed=self._seed(context, "canary-keyframe"),
            watermark=provider.watermark,
            _operation_id=f"{node.task_id}:canary-keyframe",
            _stage="keyframe",
            _operation_type="image",
            _model=provider.image_model,
            _estimated_cost_cny=provider.estimate_image_cost_cny(),
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ProviderConfigurationError(
                f"provider returned no usable keyframe: {destination}"
            )
        return destination

    def _copy_or_generate_keyframe(
        self,
        context: TaskContext,
        output: Path,
        *,
        purpose: str,
    ) -> Path:
        approved = self.approved_keyframes.get(context.frame.source_shot_id)
        if approved is not None:
            shutil.copy2(approved, output)
            return output
        provider = self.config.dashscope
        self._provider_call(
            self.image_model.generate,
            self._visual_prompt(context),
            str(output),
            model_name=provider.image_model,
            size=provider.image_size,
            seed=self._seed(context, purpose),
            watermark=provider.watermark,
            _operation_id=f"{context.node.task_id}:{purpose}",
            _stage="render",
            _operation_type="image",
            _model=provider.image_model,
            _estimated_cost_cny=provider.estimate_image_cost_cny(),
        )
        return output

    def _run_generate_video(self, context: TaskContext) -> list[Path]:
        self._ensure_clients()
        directory = self._task_dir(context)
        image = directory / "provider_keyframe.png"
        raw_video = directory / "provider_video_raw.mp4"
        output = directory / "provider_video.mp4"
        provider = self.config.dashscope
        prompt = self._visual_prompt(context)
        duration = min(
            provider.provider_clip_seconds,
            max(2, round(context.frame.duration_seconds)),
        )

        self._copy_or_generate_keyframe(context, image, purpose="video-keyframe")
        self._provider_call(
            self.video_model.generate,
            prompt,
            str(raw_video),
            img_path=str(image),
            model_name=provider.video_model,
            duration=duration,
            resolution=provider.video_resolution,
            seed=self._seed(context, "video"),
            prompt_extend=provider.prompt_extend,
            negative_prompt=self._negative_prompt(context),
            watermark=provider.watermark,
            _operation_id=f"{context.node.task_id}:video",
            _stage="render",
            _operation_type="video",
            _model=provider.video_model,
            _estimated_cost_cny=provider.estimate_video_cost_cny(duration),
        )
        self._normalise_provider_video(raw_video, output, context)
        return [image, output, raw_video]

    def _run_generate_native_av(self, context: TaskContext) -> list[Path]:
        self._ensure_clients()
        directory = self._task_dir(context)
        image = directory / "provider_keyframe.png"
        raw_video = directory / "provider_native_video_raw.mp4"
        video = directory / "provider_native_video.mp4"
        voice = directory / "provider_native_voice.wav"
        output = directory / "provider_native_av.mp4"
        provider = self.config.dashscope
        prompt = self._visual_prompt(context)
        duration_seconds = min(
            provider.provider_clip_seconds,
            max(2, round(context.frame.duration_seconds)),
        )

        self._copy_or_generate_keyframe(context, image, purpose="native-keyframe")
        self._provider_call(
            self.video_model.generate,
            prompt,
            str(raw_video),
            img_path=str(image),
            model_name=provider.native_av_video_model,
            duration=duration_seconds,
            resolution=provider.video_resolution,
            seed=self._seed(context, "native-video"),
            prompt_extend=provider.prompt_extend,
            negative_prompt=self._negative_prompt(context),
            watermark=provider.watermark,
            _operation_id=f"{context.node.task_id}:native-video",
            _stage="render",
            _operation_type="video",
            _model=provider.native_av_video_model,
            _estimated_cost_cny=provider.estimate_video_cost_cny(duration_seconds),
        )
        self._normalise_provider_video(raw_video, video, context)
        raw_voice_files = self._generate_tts_track(context, voice, directory / "raw_native_voice")
        duration = self._duration(context.frame)
        ffmpeg(
            "-i", str(video),
            "-i", str(voice),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", f"{duration:.3f}",
            "-r", str(context.prepared.project_draft.fps),
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "48000",
            "-ac", "2",
            "-movflags", "+faststart",
            str(output),
        )
        return [image, output, video, raw_video, voice, *raw_voice_files]

    def _generate_tts_track(self, context: TaskContext, output: Path, raw_dir: Path) -> list[Path]:
        duration = self._duration(context.frame)
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_files: list[Path] = []

        if not context.frame.dialogue_cues:
            ffmpeg(
                "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={duration:.3f}",
                "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(output),
            )
            return raw_files

        inputs: list[str] = [
            "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={duration:.3f}"
        ]
        labels = ["[0:a]"]
        filters: list[str] = []
        provider = self.config.dashscope

        for index, cue in enumerate(context.frame.dialogue_cues, start=1):
            raw = raw_dir / f"cue_{index:02d}.mp3"
            voice = provider.voice_by_character.get(
                cue.speaker_character_id,
                provider.default_voice,
            )
            instruction_parts = [part for part in (cue.emotion, cue.delivery) if part]
            instructions = (
                "、".join(instruction_parts)
                if provider.tts_instructions_enabled and instruction_parts
                else None
            )
            self._provider_call(
                self.tts_processor.synthesize,
                cue.text,
                str(raw),
                voice=voice,
                instructions=instructions,
                model_override=provider.tts_model,
                family_override=provider.tts_family,
                _operation_id=f"{context.node.task_id}:tts:{index:02d}",
                _stage="render",
                _operation_type="tts",
                _model=provider.tts_model,
                _estimated_cost_cny=provider.estimate_tts_cost_cny(len(cue.text)),
            )
            raw_files.append(raw)
            inputs.extend(["-i", str(raw)])
            start_ms = max(0, int(round(cue.start_seconds * 1000)))
            cue_duration = max(
                0.05,
                min(duration, cue.end_seconds) - max(0.0, cue.start_seconds),
            )
            filters.append(
                f"[{index}:a]aresample=48000,atrim=0:{cue_duration:.3f},"
                f"adelay={start_ms}|{start_ms}[voice{index}]"
            )
            labels.append(f"[voice{index}]")

        filter_complex = ";".join(
            [
                *filters,
                f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,"
                f"atrim=0:{duration:.3f},alimiter=limit=0.95[outa]",
            ]
        )
        ffmpeg(
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[outa]",
            "-t", f"{duration:.3f}",
            "-c:a", "pcm_s16le",
            "-ar", "48000",
            "-ac", "1",
            str(output),
        )
        return raw_files
