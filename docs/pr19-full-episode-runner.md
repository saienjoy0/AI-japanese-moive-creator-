# PR19 — Restart-Safe Full Episode Runner

## Goal

Turn the route-ready 15-segment GenerationPlan into one exact editorial MP4 without losing paid work when the process stops midway.

```text
PreparedEpisode + GenerationPlan + ApprovedAssetBundle + ExecutionBudget
  -> operator-approved paid execution
  -> one persistent ledger and report per segment
  -> provider MP4 per segment
  -> exact used-frame trimming
  -> immutable-order concatenation
  -> final full-episode validation
```

## Modes

### `preflight`

Preflight makes zero provider calls. It verifies:

- PreparedEpisode and GenerationPlan digest binding
- full-episode reference-asset and voice readiness
- provider-bound CNY operation budget
- committed and remaining ledger exposure
- hard call and CNY limits
- final output identity

It returns a deterministic paid-execution approval digest.

### `render`

Render is blocked unless both are supplied:

- `--execute-paid`
- the exact `--approval-digest` printed by preflight

The digest binds the plan, asset bundle, price snapshot, selected segments, total lifecycle exposure, reserves, hard limits, and output path. Before every segment submission, the complete budget is rebuilt from all ledgers. Any changed exposure, duplicate operation, unknown cost, or exceeded limit stops execution before the next provider request.

No automatic retry or provider fallback is performed. Re-running the same command resumes through existing provider task IDs and immutable ledgers.

### `compose`

Compose accepts an explicit JSON map from every GenerationSegment ID to an existing provider MP4. It makes zero provider calls and is used by CI and by operators importing already generated outputs.

## Persistent state

`full_episode_state.json` binds:

- GenerationPlan digest
- PreparedEpisode digest
- approved asset bundle digest
- paid authorization digest
- output path
- target dimensions, FPS, and frame count
- immutable segment order
- source and trimmed media SHA-256 values
- per-segment ledger and report paths
- attempts, status, and last error

Successful unchanged segments are reused. If one provider MP4 changes, only that segment is trimmed and validated again before the episode is recomposed.

## Exact editorial trimming

For every GenerationSegment the runner uses:

- `used_start_frame`
- `used_end_frame`
- `editorial_frame_count`
- `timeline_fps`

The provider clip is normalized to the target 9:16 profile and encoded to exactly the declared editorial frame count. Clips without audio receive a deterministic silent stereo track so concatenation always produces a stable audio/video episode.

## Final validation

The final MP4 must have:

- exactly the GenerationPlan target frame count
- the target FPS
- 9:16 dimensions
- one video stream
- one audio stream
- duration within two frames of the GenerationPlan target
- all segments in immutable plan order
- no missing or invalid trimmed segment
- no mostly-black result

## Safety

- CI uses only synthetic FFmpeg media
- CI contains no provider credentials
- preflight and compose always make zero provider calls
- paid render requires a separate operator authorization digest
- each segment has an immutable provider ledger
- duplicate paid submissions are refused
- no automatic retry or fallback can silently increase cost
