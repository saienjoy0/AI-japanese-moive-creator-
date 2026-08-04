# PR9 Design: Wan and Seedance Visual Provider Core

## Scope

PR9 adds a provider-neutral planning contract for **visual generation** without
replacing the proven PR8 Wan 2.7 executor.

The plan is intentionally narrow. It selects and records the provider route for:

- `generate_image`
- `generate_video`
- `generate_native_av`

External tasks that remain on the existing execution path, such as
`generate_tts`, are recorded as delegated tasks instead of being silently
omitted.

Included routes:

- `mock/video`: deterministic zero-cost CI route
- `seedance/platform`: manual official-platform route
- `wan/image`: Wan 2.7 image planning backed by PR8 pricing
- `wan/i2v`: Wan 2.7 image-to-video planning backed by PR8 pricing

## Why routing remains shot-based

The current render graph has at most one visual-generation operation per shot:

- `native_av` -> `generate_native_av`
- `video_plus_tts` -> `generate_video` plus delegated TTS
- `silent_video` -> `generate_video`
- `still_motion` -> `generate_image` plus optional delegated TTS

Therefore `route_by_shot` is sufficient for PR9. Task- or modality-specific
overrides are deferred until a shot genuinely needs multiple independent visual
providers.

## Execution plan

`ExecutionPlan.plan_scope` is fixed to `visual_generation`.

Visual tasks are stored in `tasks` with:

- route ID
- request fingerprint
- provider-neutral generation specification
- cost estimate
- optional fallback route
- a mandatory approval requirement for fallback

Delegated external tasks are stored in `delegated_tasks` with:

- task ID
- shot ID
- task type
- existing executor ID
- delegation reason

For example, Qwen3 TTS is recorded as `existing/qwen3-tts`.

The plan digest excludes `created_at`, allowing a saved plan to be loaded and
verified before execution.

## Fallback contract

PR9 records only one ordered fallback candidate. It does not execute fallback.

`fallback_requires_approval` is fixed to `true`; profiles cannot disable it.
Automatic provider switching remains prohibited until a later executor PR adds
a concrete approval record and execution transition.

## Seedance boundary

`seedance/platform` is a manual route and returns `awaiting_operator`.
Browser automation is not used.

Only the capabilities handled by the current manual request contract are
declared:

- text-to-video
- image-to-video
- reference-to-video
- first/last frame
- native audio

Continuation, driving audio, reference voice, multi-shot generation, and video
editing remain disabled until a reproducible job package or Ark API integration
implements them.

## Wan boundary

`wan/image` and `wan/i2v` reuse PR8 configuration and price snapshots.

Submission, polling, download, duplicate-charge protection, approved keyframes,
TTS, subtitles, muxing, and final MP4 creation remain delegated to
`Wan27LiveTaskExecutor`.

PR9 performs no paid provider calls.

## Example profiles

Seedance-first:

```json
{
  "profile_id": "seedance-first",
  "routing_mode": "ordered_fallback",
  "route_priority": [
    "seedance/platform",
    "wan/i2v",
    "wan/image"
  ]
}
```

Wan-first:

```json
{
  "profile_id": "wan-first",
  "routing_mode": "ordered_fallback",
  "route_priority": [
    "wan/i2v",
    "wan/image",
    "seedance/platform"
  ]
}
```

Capability validation skips incompatible routes for each visual task.

## Validation

The focused contract tests cover:

- visual tasks versus delegated TTS
- Wan video plus Qwen TTS planning
- Seedance native-AV planning
- Wan still-image planning
- Seedance-to-Wan fallback metadata
- mandatory fallback approval
- known and unknown cost gates
- deterministic digest after JSON reload
- zero paid credentials and zero generated media in CI

## Follow-up

After this focused PR9 is merged, PR10 changes to normal Japanese script
ingestion:

```text
TXT / MD / pasted Japanese script
  -> LLM StructuredScriptDraft
  -> deterministic EpisodePackage compiler
  -> existing domain validation
  -> EpisodePackage
```

Seedance job-package export, result import, and Ark API integration move behind
script ingestion because normal script input is the highest-priority product
gap.
