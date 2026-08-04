"""Wan 2.7 live executor with hard canary call limits and asset approval gates."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

from ..preparation.models import PreparedEpisode
from .ffmpeg import ffmpeg, file_sha256
from .live_tasks import LiveTaskExecutor
from .mock_tasks import TaskContext
from .provider_config import LiveProviderConfig, ProviderConfigurationError


class ProviderCallLimitError(ProviderConfigurationError):
    """A canary run attempted to exceed its explicit external-call ceiling."""


class Wan27LiveTaskExecutor(LiveTaskExecutor):
    """PR8 executor using official Wan 2.7 contracts and approved keyframes."""

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
    ) -> None:
        if api_call_limit is not None and api_call_limit < 0:
            raise ValueError("api_call_limit must be zero or greater")
        self.api_call_limit = api_call_limit
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
                "protocol": "wan27-official-v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"live:wan27:sha256:{hashlib.sha256(payload).hexdigest()}"

    @property
    def provider_manifest(self) -> dict[str, str]:
        manifest = dict(self.config.provider_manifest)
        manifest["protocol"] = "wan27-official-v1"
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

    def _provider_call(self, operation: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        if self.api_call_limit is not None and self._external_api_calls >= self.api_call_limit:
            raise ProviderCallLimitError(
                f"external API call limit reached ({self.api_call_limit}); no request submitted"
            )
        self._external_api_calls += 1
        return operation(*args, **kwargs)

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

        self._copy_or_generate_keyframe(context, image, purpose="video-keyframe")
        self._provider_call(
            self.video_model.generate,
            prompt,
            str(raw_video),
            img_path=str(image),
            model_name=provider.video_model,
            duration=min(provider.provider_clip_seconds, max(2, round(context.frame.duration_seconds))),
            resolution=provider.video_resolution,
            seed=self._seed(context, "video"),
            prompt_extend=provider.prompt_extend,
            negative_prompt=self._negative_prompt(context),
            watermark=provider.watermark,
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

        self._copy_or_generate_keyframe(context, image, purpose="native-keyframe")
        self._provider_call(
            self.video_model.generate,
            prompt,
            str(raw_video),
            img_path=str(image),
            model_name=provider.native_av_video_model,
            duration=min(provider.provider_clip_seconds, max(2, round(context.frame.duration_seconds))),
            resolution=provider.video_resolution,
            seed=self._seed(context, "native-video"),
            prompt_extend=provider.prompt_extend,
            negative_prompt=self._negative_prompt(context),
            watermark=provider.watermark,
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
