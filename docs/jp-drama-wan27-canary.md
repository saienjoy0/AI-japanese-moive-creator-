# PR8 Design: Wan 2.7 Restart-Safe, Cost-Bounded Canary

## Decision

PR8 validates the live Wan 2.7 request path without making paid calls in CI.
A full 45-second render remains blocked until one native-duration shot passes
keyframe approval and paid canary review.

This PR is the safe Wan foundation. Wan/Seedance provider switching belongs to
the next common-provider PR.

## Corrected provider contracts

### Wan 2.7 Image

The profile uses `960*1696`, a valid near-9:16 custom size whose width and
height are both at least 768 pixels. The compatibility adapter:

- removes unsupported `negative_prompt`;
- removes unsupported `prompt_extend`;
- uses `thinking_mode`;
- exposes the async provider task ID before polling.

### Wan 2.7 Image-to-Video

Wan 2.7 I2V uses the unified media-array contract:

```json
{
  "model": "wan2.7-i2v",
  "input": {
    "prompt": "...",
    "media": [
      {"type": "first_frame", "url": "oss://..."}
    ]
  },
  "parameters": {
    "resolution": "720P",
    "duration": 5
  }
}
```

The output aspect ratio follows the approved first frame. When a local PNG is
used, PR8 obtains a temporary upload policy for the exact inference model
`wan2.7-i2v`, uploads the asset, and enables
`X-DashScope-OssResourceResolve`. A Wan 2.6 upload policy is never reused for a
Wan 2.7 inference request.

### Inference and upload endpoints

Workspace inference and temporary uploads are configured separately:

```text
Inference:
  beijing   -> https://{workspace}.cn-beijing.maas.aliyuncs.com
  singapore -> https://{workspace}.ap-southeast-1.maas.aliyuncs.com

Temporary upload policy:
  beijing   -> https://dashscope.aliyuncs.com
  singapore -> https://dashscope-intl.aliyuncs.com
```

Required environment:

```bash
export DASHSCOPE_API_KEY="..."
export DASHSCOPE_WORKSPACE_ID="ws_..."
```

Optional overrides:

```bash
export DASHSCOPE_BASE_URL="..."
export DASHSCOPE_UPLOAD_BASE_URL="..."
```

Credential values are never serialized.

### Japanese TTS safeguard

The first canary keeps `qwen3-tts-flash` available for the existing render
graph, but Japanese emotion/delivery text is not sent as model instructions.
`tts_instructions_enabled` defaults to `false`.

Future Wan native-audio and driving-audio routing will be handled by the common
audio-strategy layer with Seedance.

## Native-duration canary

The selected shot is trimmed to the provider clip duration, currently five
seconds. The provider result is evaluated as a real five-second canary rather
than being looped into a misleading 15-second quality sample.

## Persistent budget ledger

The canary uses a JSON provider ledger stored separately from resettable render
state. The ledger records:

- operation ID;
- shot and stage;
- provider and model;
- estimated CNY cost;
- provider task ID and request ID;
- status and timestamps;
- output hash.

The immutable limits apply cumulatively across separate CLI processes:

```text
maximum submissions: 3
maximum estimated cost: 5 CNY by default
```

`--reset` may remove local render output and task state, but it never deletes or
resets the paid-operation ledger.

A task ID already present in the ledger is polled again rather than submitted
again. If a previous submission has uncertain status and no task ID, automatic
resubmission is refused.

## Four canary stages

All stages use the same output path, ledger path, `--max-api-calls`, and
`--max-cost-cny` values.

### 1. Preflight — zero provider calls

```bash
python -m src.apps.jp_drama.workflows.render_canary_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/canary_shot_01.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --shot-id shot_01 \
  --stage preflight \
  --max-api-calls 3 \
  --max-cost-cny 5 \
  --print-report
```

Preflight reports:

- native target duration;
- keyframe and render submission counts;
- dated price snapshot and estimated CNY cost;
- immutable cumulative limits;
- current committed operations;
- required and missing environment variables.

It creates an empty local ledger but makes zero provider calls.

### 2. Keyframe — at most one paid image operation

```bash
python -m src.apps.jp_drama.workflows.render_canary_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/canary_shot_01.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --shot-id shot_01 \
  --stage keyframe \
  --max-api-calls 3 \
  --max-cost-cny 5
```

The image operation is reserved in the ledger before submission. Its provider
task ID is persisted immediately after task creation. The command stops after
writing the image and its SHA-256.

### 3. Approve — zero provider calls

After visual review:

```bash
python -m src.apps.jp_drama.workflows.render_canary_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/canary_shot_01.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --shot-id shot_01 \
  --stage approve \
  --approved-keyframe output/jp_drama/canary_shot_01_shot_01_keyframe.png \
  --max-api-calls 3 \
  --max-cost-cny 5
```

Approval succeeds only when the ledger shows that the keyframe operation
succeeded. The generated manifest binds:

- shot ID;
- absolute asset path;
- prefixed SHA-256;
- decoded PNG dimensions;
- provider/model;
- operation ID;
- approval timestamp.

Changing the image after approval invalidates the manifest.

### 4. Render — only remaining approved operations

```bash
python -m src.apps.jp_drama.workflows.render_canary_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/canary_shot_01.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --shot-id shot_01 \
  --stage render \
  --approval-manifest output/jp_drama/canary_shot_01_shot_01_keyframe.approval.json \
  --max-api-calls 3 \
  --max-cost-cny 5
```

The approved image is reused without another image submission. Any operation
with a stored provider task ID resumes polling instead of creating a duplicate
paid task.

## Dated cost snapshot

The example profile records a price snapshot dated `2026-08-04`:

```text
wan2.7-image-pro:       0.562065 CNY / image
wan2.7-i2v 720P:        0.74942 CNY / output second
qwen3-tts-flash:        0.733924 CNY / 10,000 characters
```

This is an operator-visible safety estimate, not a live billing guarantee.
Price synchronization belongs to the later multi-provider pricing layer.

## CI guarantees

Pull-request CI:

- receives no DashScope credentials;
- compiles the provider and workflow code;
- runs 71 focused PR3–PR8 tests;
- mocks local PNG upload through task creation;
- verifies exact Wan 2.7 upload and inference model matching;
- verifies async task resumption without a second POST;
- verifies persistent call/cost ceilings;
- verifies keyframe tamper detection;
- runs preflight with zero provider calls;
- uploads only zero-cost JSON/config evidence.

## Final validation

- Japanese Drama Wan 2.7 Canary: success
- Japanese Drama Live Providers: success
- Japanese Drama Mock Render: success
- Japanese Drama Domain: success
- Japanese Drama Preparation: success
- Japanese Drama LumenX Persistence: success
- Foundation CI: success
- external paid provider calls: `0`

## Remaining boundary

PR8 does not claim:

- real visual quality;
- character continuity across shots;
- reliable Japanese native speech;
- lip synchronization;
- final Wan-versus-Seedance selection;
- a complete paid 45-second render.

Those require reviewed provider output. The next provider-core PR will preserve
this ledger/approval model while adding Seedance Platform and later Ark API
routes.
