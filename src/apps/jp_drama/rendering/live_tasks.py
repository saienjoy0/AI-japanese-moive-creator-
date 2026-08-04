"""Live DashScope-backed task implementations for Japanese short dramas."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from ..preparation.models import PreparedEpisode
from .ffmpeg import ffmpeg
from .mock_tasks import MockTaskExecutor, TaskContext
from .provider_config import LiveProviderConfig, ProviderConfigurationError


class LiveTaskExecutor(MockTaskExecutor):
    """Replace PR6 generation mocks while retaining local composition tasks."""

    def __init__(
        self,
        config: LiveProviderConfig,
        *,
        image_model: Any | None = None,
        video_model: Any | None = None,
        tts_processor: Any | None = None,
        require_credentials: bool = True,
    ) -> None:
        self.config = config
        if require_credentials:
            config.require_environment()
        self.image_model = image_model
        self.video_model = video_model
        self.tts_processor = tts_processor
        self._external_api_calls = 0

    @property
    def execution_profile(self) -> str:
        return self.config.execution_profile

    @property
    def provider_manifest(self) -> dict[str, str]:
        return self.config.provider_manifest

    @property
    def external_api_calls(self) -> int:
        return self._external_api_calls

    @staticmethod
    def estimate_api_calls(prepared: PreparedEpisode) -> int:
        frames = {frame.source_shot_id: frame for frame in prepared.storyboard_frame_drafts}
        total = 0
        for node in prepared.render_graph.nodes:
            frame = frames[node.shot_id]
            if node.task_type == "generate_image":
                total += 1
            elif node.task_type == "generate_video":
                total += 2
            elif node.task_type == "generate_tts":
                total += len(frame.dialogue_cues)
            elif node.task_type == "generate_native_av":
                total += 2 + len(frame.dialogue_cues)
        return total

    def _ensure_clients(self) -> None:
        if self.image_model is None:
            try:
                from src.models.image import WanxImageModel
            except Exception as exc:  # pragma: no cover
                raise ProviderConfigurationError(
                    "cannot import the LumenX image provider; install requirements.txt"
                ) from exc
            self.image_model = WanxImageModel({"params": {}})
        if self.video_model is None:
            try:
                from src.models.wanx import WanxModel
            except Exception as exc:  # pragma: no cover
                raise ProviderConfigurationError(
                    "cannot import the LumenX video provider; install requirements.txt"
                ) from exc
            self.video_model = WanxModel({"params": {}})
        if self.tts_processor is None:
            try:
                from src.audio.tts import TTSProcessor
            except Exception as exc:  # pragma: no cover
                raise ProviderConfigurationError(
                    "cannot import the LumenX TTS provider; install requirements.txt"
                ) from exc
            provider = self.config.dashscope
            self.tts_processor = TTSProcessor(
                model=provider.tts_model,
                voice=provider.default_voice,
            )

    def _provider_call(self, operation: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        self._external_api_calls += 1
        return operation(*args, **kwargs)

    def _run_generate_image(self, context: TaskContext) -> list[Path]:
        self._ensure_clients()
        directory = self._task_dir(context)
        output = directory / "provider_image.png"
        provider = self.config.dashscope
        self._provider_call(
            self.image_model.generate,
            self._visual_prompt(context),
            str(output),
            negative_prompt=self._negative_prompt(context),
            model_name=provider.image_model,
            size=provider.image_size,
            seed=self._seed(context, "image"),
            prompt_extend=provider.prompt_extend,
            watermark=provider.watermark,
        )
        return [output]

    def _run_generate_video(self, context: TaskContext) -> list[Path]:
        self._ensure_clients()
        directory = self._task_dir(context)
        image = directory / "provider_keyframe.png"
        raw_video = directory / "provider_video_raw.mp4"
        output = directory / "provider_video.mp4"
        provider = self.config.dashscope
        prompt = self._visual_prompt(context)

        self._provider_call(
            self.image_model.generate,
            prompt,
            str(image),
            negative_prompt=self._negative_prompt(context),
            model_name=provider.image_model,
            size=provider.image_size,
            seed=self._seed(context, "video-keyframe"),
            prompt_extend=provider.prompt_extend,
            watermark=provider.watermark,
        )
        self._provider_call(
            self.video_model.generate,
            prompt,
            str(raw_video),
            img_path=str(image),
            model_name=provider.video_model,
            duration=min(provider.provider_clip_seconds, max(1, round(context.frame.duration_seconds))),
            ratio=provider.video_ratio,
            resolution=provider.video_resolution,
            seed=self._seed(context, "video"),
            prompt_extend=provider.prompt_extend,
            watermark=provider.watermark,
        )
        self._normalise_provider_video(raw_video, output, context)
        return [image, output, raw_video]

    def _run_generate_tts(self, context: TaskContext) -> list[Path]:
        self._ensure_clients()
        directory = self._task_dir(context)
        output = directory / "provider_voice.wav"
        raw_files = self._generate_tts_track(context, output, directory / "raw_voice")
        return [output, *raw_files]

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

        self._provider_call(
            self.image_model.generate,
            prompt,
            str(image),
            negative_prompt=self._negative_prompt(context),
            model_name=provider.image_model,
            size=provider.image_size,
            seed=self._seed(context, "native-keyframe"),
            prompt_extend=provider.prompt_extend,
            watermark=provider.watermark,
        )
        self._provider_call(
            self.video_model.generate,
            prompt,
            str(raw_video),
            img_path=str(image),
            model_name=provider.native_av_video_model,
            duration=min(provider.provider_clip_seconds, max(1, round(context.frame.duration_seconds))),
            ratio=provider.video_ratio,
            resolution=provider.video_resolution,
            seed=self._seed(context, "native-video"),
            prompt_extend=provider.prompt_extend,
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
            voice = provider.voice_by_character.get(cue.speaker_character_id, provider.default_voice)
            instruction_parts = [part for part in (cue.emotion, cue.delivery) if part]
            instructions = "、".join(instruction_parts) or None
            self._provider_call(
                self.tts_processor.synthesize,
                cue.text,
                str(raw),
                voice=voice,
                instructions=instructions,
                model_override=provider.tts_model,
                family_override=provider.tts_family,
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

    def _normalise_provider_video(self, source: Path, output: Path, context: TaskContext) -> None:
        duration = self._duration(context.frame)
        fps = context.prepared.project_draft.fps
        ffmpeg(
            "-stream_loop", "-1",
            "-i", str(source),
            "-vf",
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
            f"crop={self.width}:{self.height},fps={fps},format=yuv420p",
            "-t", f"{duration:.3f}",
            "-an",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "21",
            "-pix_fmt", "yuv420p",
            "-map_metadata", "-1",
            str(output),
        )

    def _visual_prompt(self, context: TaskContext) -> str:
        prepared = context.prepared
        frame = context.frame
        character_ids = set(frame.character_seed_ids)
        characters = [seed for seed in prepared.character_seeds if seed.seed_id in character_ids]
        location = next(
            (seed for seed in prepared.location_seeds if seed.seed_id == frame.location_seed_id),
            None,
        )
        prop_ids = set(frame.prop_seed_ids)
        props = [seed for seed in prepared.prop_seeds if seed.seed_id in prop_ids]

        parts = [
            "Japanese live-action vertical short drama, cinematic realism, consistent cast",
            frame.visual_description,
            frame.action,
            (
                f"Camera: {frame.camera.shot_size}, {frame.camera.angle}, "
                f"{frame.camera.movement}, {frame.camera.speed}"
            ),
        ]
        if location:
            parts.append(f"Location continuity: {location.visual_prompt}")
        if characters:
            parts.append("Characters: " + "; ".join(item.visual_prompt for item in characters))
        if props:
            parts.append("Props: " + "; ".join(item.visual_prompt for item in props))
        parts.append("No captions or on-screen text; subtitles are added later")
        return ". ".join(part.strip(" .") for part in parts if part)

    def _negative_prompt(self, context: TaskContext) -> str:
        character_ids = set(context.frame.character_seed_ids)
        negatives = [
            seed.negative_prompt
            for seed in context.prepared.character_seeds
            if seed.seed_id in character_ids and seed.negative_prompt
        ]
        base = (
            "text, subtitles, watermark, logo, malformed hands, duplicated people, "
            "identity drift, costume change, low resolution, black frame"
        )
        return ", ".join([base, *negatives])

    def _seed(self, context: TaskContext, purpose: str) -> int:
        provider = self.config.dashscope
        material = f"{context.prepared.source_digest}|{context.node.task_id}|{purpose}".encode(
            "utf-8"
        )
        offset = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
        return (provider.seed_base + offset) % 2_147_483_647