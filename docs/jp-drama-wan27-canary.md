# PR8 Design: Wan 2.7 Compatibility and Cost-Bounded Canary

## Decision

PR7 proved the provider execution boundary with injected clients, but it did
not prove that the current DashScope requests match the live Wan 2.7 protocol.
PR8 closes that gap without making a paid call in CI.

A full 45-second render remains blocked until a one-shot canary is reviewed.

## Corrected provider contracts

### Wan 2.7 Image

The official Wan 2.7 image contract differs from earlier Wan image models:

- custom width and height must each be at least 768 pixels;
- `negative_prompt` is unsupported;
- `prompt_extend` is unsupported;
- `thinking_mode` is the supported prompt-reasoning control.

The Japanese-drama profile now uses `960*1696`, which is a valid vertical
custom size. The compatibility adapter keeps the existing LumenX public model
interface but removes the unsupported parameters from the outgoing request.

### Wan 2.7 Image-to-Video

Wan 2.7 I2V uses the unified multimodal protocol:

```json
{
  "input": {
    "prompt": "...",
    "media": [
      {"type": "first_frame", "url": "..."}
    ]
  },
  "parameters": {
    "resolution": "720P",
    "duration": 5
  }
}
```

It does not accept the older `img_url` request field or a direct output
`ratio`. The output aspect ratio follows the approved first-frame image. PR8
therefore keeps the first frame vertical and sends `resolution=720P`.

### Region and endpoint

API keys, models, and endpoints must be from the same region. PR8 supports the
Wan 2.7 regions used by this profile:

- `beijing` -> `{workspace}.cn-beijing.maas.aliyuncs.com`
- `singapore` -> `{workspace}.ap-southeast-1.maas.aliyuncs.com`

The operator configures:

```bash
export DASHSCOPE_API_KEY="..."
export DASHSCOPE_WORKSPACE_ID="ws_..."
```

A complete custom base URL may instead be supplied through
`DASHSCOPE_BASE_URL`. Credential and workspace values are never serialized.

Official references:

- https://help.aliyun.com/en/model-studio/text-to-image
- https://help.aliyun.com/en/model-studio/wan-image-generation-and-editing-api-reference
- https://help.aliyun.com/en/model-studio/image-to-video-general-api-reference
- https://help.aliyun.com/en/model-studio/base-url
- https://help.aliyun.com/en/model-studio/qwen-tts-voice-list

## Canary stages

The canary command isolates exactly one shot and creates a separate project ID,
source digest, LumenX store, task state, and output.

### 1. Zero-cost preflight

```bash
python -m src.apps.jp_drama.workflows.render_canary_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/canary_shot_01.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --shot-id shot_01 \
  --stage preflight \
  --max-api-calls 3 \
  --print-report
```

For `shot_01`, preflight reports:

- keyframe stage: one image request;
- render after keyframe approval: one video request plus one TTS request;
- CI provider requests: zero.

### 2. Generate one keyframe

```bash
python -m src.apps.jp_drama.workflows.render_canary_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/canary_shot_01.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --shot-id shot_01 \
  --stage keyframe \
  --max-api-calls 1
```

The command stops after the image and prints its SHA-256. A person reviews the
face, costume, composition, text artifacts, and setting before continuing.

### 3. Render from the approved keyframe

```bash
python -m src.apps.jp_drama.workflows.render_canary_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/canary_shot_01.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --shot-id shot_01 \
  --stage render \
  --approved-keyframe output/jp_drama/canary_shot_01_shot_01_keyframe.png \
  --max-api-calls 2
```

The approved image is copied into the restart-safe task directory and is not
regenerated. The external providers are called only for video and the shot's
TTS cue. Subtitles, muxing, BGM placeholder, finalization, and validation remain
local.

## Full-render guard

The full live CLI now defaults to a zero-call ceiling. Even with credentials
present it refuses to start unless `--max-api-calls` explicitly covers the
preflight estimate. This prevents an accidental nine-call sample render.

## Hard safety properties

- `--max-api-calls` cannot exceed 3 in the canary CLI.
- The executor checks the ceiling before provider submission.
- The render stage internally narrows its ceiling to the approved-shot estimate.
- A failed request is counted before submission and persisted by the runner.
- There is no automatic in-process retry.
- Rerunning reuses completed task outputs.
- Changing the approved keyframe changes the execution fingerprint and blocks
  incompatible state reuse.
- `--stage render` refuses to run without a non-empty approved image.
- CI does not receive API credentials and never calls DashScope.

## Remaining boundary

PR8 validates the official request structures and adds an operator-controlled
canary. It does not claim visual quality, character continuity across shots,
real BGM/SFX, lip synchronization, current price synchronization, or a complete
45-second paid render. Those require evidence from the reviewed canary output.
