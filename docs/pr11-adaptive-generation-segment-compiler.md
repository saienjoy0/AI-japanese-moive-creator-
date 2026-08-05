# PR11 Adaptive Generation Segment Compiler v0.2

## 1. Purpose

Compile the existing `PreparedEpisode` into deterministic, provider-aware,
variable-length generation segments without submitting any paid generation job.

```text
PreparedEpisode
  -> explicit editorial boundary extraction
  -> complexity-aware grouping
  -> GenerationPlanEpisode
  -> PR9 provider-neutral ShotGenerationSpec bridge
```

PR11 is a planning compiler. It does not claim that a real image, video, voice,
lip-sync result, or full episode has been validated.

## 2. Corrections from v0.1

### 2.1 Name and readiness states

The output is `GenerationPlanEpisode`, not `GenerationReadyEpisode`.
Readiness is separated into:

- `planning_ready`: the deterministic segmentation contract is internally valid
- `execution_route_ready`: the selected registered adapter can execute every unit
- `media_quality_validated`: always false in PR11

This prevents an offline contract test from being presented as real-media proof.

### 2.2 Reuse PR9 provider contracts

PR11 does not create a second provider capability model. It reuses:

- `ProviderCapabilities`
- `ProviderCapabilitiesRequired`
- `ShotGenerationSpec`
- `ProviderRegistry`
- existing Wan, Seedance, and mock adapters

PR11 adds only `SegmentationPolicy`, which stores operational preferences such as
stable target ranges and internal editorial-shot limits. Official model ability
and project operating policy are not the same thing.

### 2.3 Frame-native timeline

The canonical editorial timeline uses integer frames:

```text
timeline_fps
editorial_start_frame
editorial_end_frame
editorial_frame_count
used_start_frame
used_end_frame
```

Seconds are derived for provider requests and reports. This guarantees exact
episode duration, contiguous segments, deterministic trim handles, and dialogue
ranges that cannot drift due to float accumulation.

### 2.4 No invented action boundaries

Current `PreparedEpisode` data contains storyboard-frame boundaries, dialogue
ranges, camera data, character/location/prop references, and continuity notes.
It does not contain a complete semantic event graph for every gesture, entrance,
expression change, or prop-state transition.

PR11 therefore uses only explicit evidence:

- StoryboardFrame boundaries
- dialogue start and end frames
- location, character-set, prop-set, and route continuity
- source beat and shot references
- explicit camera and audio fields

When a complex interval is longer than policy permits and has no explicit safe
boundary, the compiler reports `insufficient_segmentation_evidence`. It does not
split every five seconds and does not infer a new action from prose.

## 3. Core contracts

### GenerationSegment

A provider generation call unit. It contains one or more `EditorialShot` items.
The source editorial duration and requested provider duration are separate.

Example:

```text
editorial: 7.0 seconds / 210 frames
provider request: 8 seconds / 240 frames
used range: frames 15-225
```

Dialogue must fit completely inside the used editorial range.

### EditorialShot

An explicit sub-range inside a generation segment. In PR11 v1, editorial shots
are created from dialogue and storyboard boundaries rather than free-form NLP.

### ContinuityContract

Locks structured character appearance, location description, prop presence,
lighting/time-of-day information, and reference-asset IDs across related
segments.

### PromptBundle

Contains narrative, visual, motion, camera, timed-shot, dialogue, audio, and
negative constraints. Provider-specific language translation remains an adapter
responsibility.

## 4. Segmentation algorithm

1. Strictly load `PreparedEpisode`.
2. Stop only for structural blockers such as broken mappings or a cyclic source graph.
3. Convert each source frame to the episode frame timeline.
4. Add explicit boundaries at frame start/end and dialogue start/end.
5. Score complexity deterministically from structured fields.
6. Select the policy duration band.
7. Greedily group adjacent explicit units without exceeding provider maximum,
   policy maximum, or internal editorial-shot count.
8. Merge a final under-minimum group only when provider and policy limits remain valid.
9. Quantize the provider request to integer seconds.
10. Allocate deterministic symmetric trim handles.
11. Create dialogue slices, prompt bundles, continuity contracts, and asset requirements.
12. Convert each segment to the existing PR9 `ShotGenerationSpec` contract.
13. Validate against the selected adapter and estimate known provider cost.
14. Build an acyclic segment render graph.
15. Write canonical JSON with deterministic IDs and digests.

## 5. Initial policy

```text
low:       8-12 seconds, target 10
medium:    7-10 seconds, target 8
high:      4-7 seconds, target 6
very_high: 2-5 seconds, target 4
```

A low-complexity segment may reach 15 seconds only when the provider supports it
and character count is within policy. These are project defaults, not provider
quality guarantees. PR12 Canary evidence will tune them.

## 6. Provider behavior

### Mock

The mock route validates the complete planning path with zero network calls and
supports multi-shot contracts for CI.

### Wan 2.7

The PR11 profile uses the registered `wan/i2v` capability contract and the
existing `wan/image` pricing adapter for first-frame estimates. Current PR9/PR8
execution remains delegated and is not silently replaced.

If the current adapter is registered without multi-shot execution, PR11 may
produce a valid plan while setting `execution_route_ready=false`. PR12 must
migrate and Canary-test that execution path before paid full-episode use.

### Seedance platform

The route remains manual and returns operator work rather than browser
automation. PR11 can export the plan, but a current capability restriction such
as `multi_shot=false` is reported honestly as a route-readiness error.

## 7. Cost model

The compiler reports exact call counts for:

- reference image generation
- video generation
- delegated TTS
- native-audio generation

Known adapter costs are totaled by currency. Currencies are not automatically
converted. Unknown components remain explicit. A hard budget cannot pass while
relevant cost components are unknown.

## 8. Render graph

```text
prepare_references
  -> generate_first_frame (I2V only)
  -> generate_video / generate_native_av
  -> generate_tts (external-audio path only)
  -> generate_subtitles
  -> mux_segment
  -> trim_segment
  -> validate_segment
  -> concat_episode
  -> validate_episode
```

The graph validates unique task IDs, known dependencies, and absence of cycles.

## 9. Outputs

```text
output/jp_drama/generation/
├── generation_plan_episode.json
├── generation_segments.json
├── editorial_shots.json
├── continuity_contracts.json
├── reference_asset_requirements.json
├── generation_render_graph.json
├── generation_cost_plan.json
├── generation_readiness_report.json
└── summary.txt
```

All writes are atomic. The same source, profile, and registered capabilities
produce byte-identical JSON and stable IDs.

## 10. CLI

Mock planning:

```bash
python -m src.apps.jp_drama.workflows.prepare_generation \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output-dir output/jp_drama/generation \
  --profile examples/jp_drama/generation/mock_profile.json \
  --print-report
```

Wan planning:

```bash
python -m src.apps.jp_drama.workflows.prepare_generation \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output-dir output/jp_drama/generation-wan \
  --profile examples/jp_drama/generation/wan27_profile.json \
  --live-provider-config examples/jp_drama/dashscope_live_providers.json \
  --print-report
```

The CLI never calls `submit`, `poll`, or `download`.

## 11. Acceptance criteria

- `PreparedEpisode -> GenerationPlanEpisode` works offline.
- The timeline is frame-exact and equals the source episode duration.
- Segment duration is variable rather than fixed to five seconds.
- Provider generation units and internal editorial shots are separate.
- Dialogue is assigned exactly once and never trimmed.
- Current PR9 capability and pricing contracts are reused.
- Continuity and reference requirements are structured.
- Costs are counted after segmentation and kept by currency.
- IDs, digests, order, and JSON are deterministic.
- CI has no provider credentials and makes zero external calls.
- Real-media quality is not claimed.

## 12. Next step

PR12 selects one `GenerationSegment`, creates or approves its first frame,
executes at most one video generation plus required audio, and records real
quality, cost, API, and restart-safety evidence before any full-episode run.
