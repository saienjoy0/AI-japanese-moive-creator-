# PR22: Unified Production Entry and Zero-Call Episode Composer

## Purpose

This PR removes the unsafe split between the legacy full-episode renderer and
the newer GenerationPlan / ApprovedAssetBundle / budget-gated execution path.

The production contract is now:

```text
PreparedEpisode
+ GenerationPlanEpisode
+ ApprovedAssetBundle
+ provider or operator SegmentArtifact records
  -> exact frame trim
  -> immutable segment order
  -> episode compose
  -> final MP4 validation
```

Paid provider dispatch is intentionally **not** enabled in this PR. The new
entry supports only:

- zero-call production preflight
- zero-call composition of existing, hash-bound segment MP4 files
- fail-closed rejection of paid full-episode execution

## Why the legacy renderer is disabled

`render_episode.py` previously accepted only a `PreparedEpisode`, provider
configuration, and an API-call ceiling. That allowed a full paid run without:

- a deterministic `GenerationPlanEpisode`
- an `ApprovedAssetBundle`
- an `ExecutionBudget`
- a provider-neutral segment artifact
- an operator approval digest bound to the complete production state

The command remains available for zero-call compatibility preflight, but every
non-preflight invocation now returns exit code `6` before provider submission.

## New contracts

### SegmentArtifact

Every Wan, MiniMax H3, or operator-imported Seedance result must eventually be
normalized to the same contract.

```json
{
  "segment_id": "segment_...",
  "generation_plan_digest": "sha256:...",
  "provider_route_id": "wan/i2v",
  "output_path": "/absolute/path/segment.mp4",
  "output_sha256": "sha256:...",
  "width": 720,
  "height": 1280,
  "fps": 30,
  "frame_count": 150,
  "duration_seconds": 5.0,
  "audio_present": false,
  "approval_digest": "sha256:...",
  "ledger_path": "/absolute/path/provider-ledger.json",
  "valid": true
}
```

The composer does not trust this metadata blindly. It reads the MP4 again and
verifies the SHA-256, dimensions, FPS, frames, duration, audio state, provider
route, segment ID, and GenerationPlan digest.

### SegmentArtifactManifest

A complete episode requires exactly one artifact for every plan segment. Extra,
missing, duplicate, stale, or cross-plan artifacts are rejected.

The manifest itself is canonicalized and SHA-256 bound.

## Single production CLI

### Zero-call preflight

```bash
python -m src.apps.jp_drama.workflows.run_production_episode \
  --prepared-input output/prepared_episode.json \
  --generation-plan output/generation_plan_episode.json \
  --asset-bundle output/approved_asset_bundle.json \
  --stage preflight \
  --report output/production_preflight.json
```

Preflight validates:

- PreparedEpisode and GenerationPlan digest binding
- source episode ID
- FPS identity
- planning and route readiness
- complete full-episode asset readiness
- zero provider calls

`paid_execution_enabled` is always `false` in PR22.

### Exact zero-call composition

```bash
python -m src.apps.jp_drama.workflows.run_production_episode \
  --prepared-input output/prepared_episode.json \
  --generation-plan output/generation_plan_episode.json \
  --segment-artifacts output/segment_artifacts.json \
  --stage compose \
  --output output/episode.mp4 \
  --work-dir output/episode-work \
  --report output/episode-compose-report.json
```

For each segment, the composer:

1. verifies the artifact and source MP4
2. checks that the provider clip contains the complete `used_start_frame` to
   `used_end_frame` editorial window
3. trims by exact frames
4. adds deterministic silent stereo when the source has no audio
5. normalizes to the requested vertical dimensions and plan FPS
6. validates exact segment frame count
7. concatenates in immutable GenerationPlan order
8. validates final frames, duration, streams, dimensions, FPS, SHA-256, and
   black-frame duration

Composition never calls a provider.

### Paid render

```bash
python -m src.apps.jp_drama.workflows.run_production_episode \
  --prepared-input ... \
  --generation-plan ... \
  --stage render
```

This currently fails closed with:

```text
common_provider_dispatcher_not_implemented
```

No credentials are loaded and no provider request is submitted.

## Scope retained from the old Draft full-episode runner

The safe parts of the old PR19 design are retained conceptually:

- exact frame windows
- immutable segment order
- final media validation
- zero-call compose mode
- hash-bound inputs
- no automatic provider fallback

The stale paid Wan loop is not copied. Provider dispatch will be added only
after Wan, H3, and imported Seedance outputs all produce the same
`SegmentArtifact`.

## Tests

The focused CI proves:

- manifest content is digest-bound
- changed artifact paths invalidate the manifest
- the paid production stage makes zero calls and fails closed
- synthetic provider MP4s compose into an exact 45-second / 1350-frame episode
- stale MP4 SHA-256 values are rejected
- the legacy renderer still supports zero-call preflight
- the legacy paid entry remains blocked

## Next PR

PR23 will import the merged three-episode `一房の葡萄_generation_plan.yaml` and
produce three PreparedEpisode / GenerationPlan pairs while preserving:

- series ID and episode numbers
- source title, author, and rights status
- all 15 production segments
- dialogue speakers and inner monologue
- character, location, and prop IDs
- paint and grape state transitions
- 24 FPS and exact 50-second episode timelines
