# PR7 Design: Live Image, Video, and TTS Providers

## Purpose

PR7 replaces the generation-only mocks from PR6 with the real provider adapters
already present in LumenX while retaining the restart-safe production engine.

```text
PreparedEpisode
  -> PR5 LumenX persistence
  -> PR6 dependency-ordered RenderGraph runner
  -> DashScope image generation
  -> DashScope image-to-video generation
  -> DashScope Japanese TTS
  -> PR6 subtitles / mux / BGM / cut finalization
  -> ordered 9:16 MP4
```

PR6 remains available as a zero-cost regression path. PR7 adds a separate live
entry point so CI never spends provider credits accidentally.

## Reused LumenX adapters

PR7 does not create a second provider SDK implementation.

| Modality | Existing LumenX adapter | PR7 default |
|---|---|---|
| image | `src.models.image.WanxImageModel` | `wan2.7-image-pro` |
| video | `src.models.wanx.WanxModel` | `wan2.7-i2v` |
| TTS | `src.audio.tts.TTSProcessor` | `qwen3-tts-flash` |
| Japanese voice | Qwen3 voice registry | `Ono Anna` |

The adapters continue to own DashScope request creation, uploads, polling,
result download, and provider-specific media transport.

## Live command

```bash
export DASHSCOPE_API_KEY="..."

python -m src.apps.jp_drama.workflows.render_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/episode.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json
```

The same command resumes after a failed task. Use `--reset` only when all prior
provider results should be discarded and regenerated.

## Preflight

Preflight validates the PreparedEpisode, provider config, model names, target
voice mapping, expected provider-call count, and credential presence without
making an API call:

```bash
python -m src.apps.jp_drama.workflows.render_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/episode.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --preflight \
  --print-report
```

A missing credential is reported by preflight but does not prevent inspection.
A real render refuses to start unless the configured environment variable is
present.

## Provider configuration

The provider config is strict JSON. Unknown fields are rejected.

```json
{
  "schema_version": "1.0.0",
  "mode": "live",
  "dashscope": {
    "api_key_env": "DASHSCOPE_API_KEY",
    "image_model": "wan2.7-image-pro",
    "video_model": "wan2.7-i2v",
    "tts_model": "qwen3-tts-flash",
    "default_voice": "Ono Anna"
  }
}
```

Only the environment-variable name is persisted. The API key value is never
written into state, reports, fingerprints, logs produced by PR7, or the LumenX
project.

## Task replacement

| RenderGraph task | PR7 behavior |
|---|---|
| `generate_image` | call the configured image model |
| `generate_video` | generate a keyframe, then call image-to-video |
| `generate_native_av` | generate keyframe + video + timed TTS inside one task |
| `generate_tts` | synthesize each dialogue cue and place it at cue timing |
| `generate_subtitles` | unchanged local ASS generation |
| `apply_still_motion` | unchanged local FFmpeg motion |
| `mux_audio_video` | unchanged local voice/SFX/subtitle mux |
| `finalize_shot` | unchanged local normalization and quiet BGM |

The current PR4 sample has 3 dialogue cues and requires an estimated 9 provider
calls: 3 keyframes, 3 video generations, and 3 TTS calls.

## Prompt construction

Each live image/video prompt combines:

- Japanese live-action vertical-drama direction;
- frame visual description and action;
- camera size, angle, movement, and speed;
- location continuity prompt;
- referenced character visual prompts;
- referenced prop prompts;
- an instruction not to generate text, logos, or subtitles.

Negative prompts include identity drift, costume changes, malformed anatomy,
watermarks, text, and black frames.

## Timing and normalization

Provider clip duration is configurable and defaults to 5 seconds. The current
PR4 shots are 15 seconds. PR7 normalizes provider output to the exact shot
contract with FFmpeg and repeats the source clip when necessary so the existing
graph can be completed without changing the editorial timing model.

This is an integration bridge, not the final quality strategy. A later quality
PR should split long shots into provider-native subshots instead of repeating a
short provider clip.

TTS is generated per dialogue cue, trimmed to the cue window, delayed to its
specified start time, and mixed into a full-length shot audio track.

## Restart safety and cost accounting

Provider configuration contributes to the execution profile. A work directory
created by mock mode cannot be reused by live mode, and changing any live model,
voice, ratio, resolution, or seed profile causes a state conflict instead of
silently reusing incompatible assets.

State records:

- execution-profile fingerprint;
- non-secret provider manifest;
- total external API calls;
- external API calls attributed to each graph task;
- attempts, errors, and generated files from PR6.

Calls are counted before provider submission so failed requests are retained in
the cost evidence. Completed tasks are not called again during resume.

## CI policy

GitHub Actions must not receive a paid provider key for PR7 validation. CI uses
injected contract clients that create valid local image, video, and speech files
while exercising the exact live executor, call accounting, state isolation,
timing, muxing, and final MP4 validation.

CI also runs:

- strict provider-config tests;
- missing-credential tests;
- secret non-serialization tests;
- provider preflight with zero external calls;
- PR3 through PR7 focused tests;
- the original PR6 zero-cost MP4 render;
- Foundation CI.

## Completion criteria

- PR6 is merged before the PR7 branch;
- one live CLI selects real LumenX image/video/TTS adapters;
- Japanese TTS voice mapping is configurable per character;
- generated media returns to the unchanged PR6 local composition pipeline;
- live and mock state cannot be mixed;
- failed provider tasks remain resumable without repeating earlier cuts;
- external API-call counts persist per task and per run;
- secrets are never serialized;
- preflight performs zero provider calls;
- CI performs zero paid calls while validating the full live-provider contract.