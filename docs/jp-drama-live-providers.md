# Live Provider Integration — Historical PR7 Contract

> **Production safety notice (PR22)**
>
> `src.apps.jp_drama.workflows.render_episode` is now a zero-call legacy
> preflight command only. Its previous paid full-episode path is disabled.
> Do not use this historical document as a paid-render runbook.
>
> The current production entry is:
>
> ```bash
> python -m src.apps.jp_drama.workflows.run_production_episode \
>   --prepared-input output/prepared_episode.json \
>   --generation-plan output/generation_plan_episode.json \
>   --asset-bundle output/approved_asset_bundle.json \
>   --stage preflight
> ```
>
> Paid full-episode dispatch remains fail-closed until Wan, MiniMax H3, and
> operator-imported Seedance results all produce the common, approval-bound
> `SegmentArtifact` contract. Existing approved single-segment Canary commands
> remain the only live provider execution paths.

## Historical purpose

PR7 replaced the generation-only mocks from PR6 with the real provider adapters
already present in LumenX while retaining the original RenderGraph engine.

```text
PreparedEpisode
  -> LumenX persistence
  -> dependency-ordered RenderGraph
  -> DashScope image generation
  -> DashScope image-to-video generation
  -> Japanese TTS
  -> subtitles / mux / BGM / cut finalization
  -> ordered 9:16 MP4
```

This implementation remains useful as provider and media integration code, but
its original all-episode CLI no longer satisfies the later GenerationPlan,
ApprovedAssetBundle, ExecutionBudget, approval, and persistent-ledger safety
contracts.

## Reused LumenX adapters

| Modality | Existing LumenX adapter | Historical default |
|---|---|---|
| image | `src.models.image.WanxImageModel` | `wan2.7-image-pro` |
| video | `src.models.wanx.WanxModel` | `wan2.7-i2v` |
| TTS | `src.audio.tts.TTSProcessor` | `qwen3-tts-flash` |
| Japanese voice | Qwen3 voice registry | `Ono Anna` |

The adapters continue to own provider-specific request creation, uploads,
polling, result download, and media transport where they are invoked by a
current approval-gated workflow.

## Allowed legacy preflight

The old command may still inspect a PreparedEpisode and provider configuration
without making a provider call:

```bash
python -m src.apps.jp_drama.workflows.render_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/episode.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --preflight \
  --print-report
```

Its report contains:

```text
legacy_entry=true
paid_execution_enabled=false
external_api_calls=0
```

Any invocation without `--preflight` stops before credentials are required and
returns `legacy_full_episode_entry_disabled`.

## Current live execution routes

### Wan single-segment Canary

Use the GenerationPlan and ApprovedAssetBundle-gated Wan segment workflow. It
requires an approved first frame, a persistent provider ledger, and an approved
execution budget before video submission.

```bash
python -m src.apps.jp_drama.workflows.render_generation_segment_canary \
  --prepared-input output/prepared_episode.json \
  --generation-plan output/generation_plan_episode.json \
  --asset-bundle output/approved_asset_bundle.json \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --segment-id auto \
  --stage preflight \
  --output output/canary.mp4
```

### MiniMax H3 single-segment Canary

The H3 workflow requires an exact public-HTTPS reference-asset manifest,
request fingerprint approval, authoritative cost gate, and persistent one-POST
ledger.

```bash
python -m src.apps.jp_drama.workflows.render_minimax_h3_segment_canary \
  --prepared-input output/prepared_episode.json \
  --generation-plan output/generation_plan_episode.json \
  --segment-id SEGMENT_ID \
  --assets output/h3_asset_manifest.json \
  --config examples/jp_drama/minimax_h3_live_provider.json \
  --stage preflight \
  --output output/h3-canary.mp4
```

### Seedance official platform

`seedance/platform` remains a manual route. The application prepares a timed
operator prompt but does not automate the browser or download the result.
A later importer must validate the returned MP4 and create a `SegmentArtifact`
before episode composition.

## Provider configuration

The DashScope provider config is strict JSON. Unknown fields are rejected. Only
the environment-variable name is persisted; the secret value must never appear
in state, reports, fingerprints, logs, or LumenX projects.

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

## Historical task replacement

| RenderGraph task | Historical PR7 behavior |
|---|---|
| `generate_image` | call configured image model |
| `generate_video` | generate a keyframe, then image-to-video |
| `generate_native_av` | generate keyframe + video + timed TTS |
| `generate_tts` | synthesize each dialogue cue at cue timing |
| `generate_subtitles` | local ASS generation |
| `apply_still_motion` | local FFmpeg motion |
| `mux_audio_video` | local voice/SFX/subtitle mux |
| `finalize_shot` | local normalization and quiet BGM |

The legacy normalizer could repeat a short provider clip to fill a long
editorial shot. That behavior is retained only for historical compatibility and
must not be used as the final production-quality strategy. Current production
planning uses provider-native GenerationSegments and exact editorial trim
windows instead.

## Current production completion rule

A complete episode may be composed only after every planned segment has one
validated artifact containing:

- exact segment ID and GenerationPlan digest;
- provider route;
- MP4 path and SHA-256;
- dimensions, FPS, frame count, duration, and audio state;
- an approval digest;
- provider ledger or operator-import provenance where applicable.

`run_production_episode --stage compose` re-reads every MP4 and rejects stale,
missing, extra, cross-plan, unapproved, or media-incompatible artifacts. It then
trims by exact frames, concatenates in immutable plan order, and validates the
final vertical MP4 with zero provider calls.

See `docs/pr22-unified-production-entry.md` for the active production contract.
