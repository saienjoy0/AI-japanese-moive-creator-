# PR6 Design: Provider-Free End-to-End Mock Rendering

## Purpose

PR6 proves the complete Japanese short-drama production path before any paid
provider is connected.

```text
EpisodePackage
  -> PreparedEpisode
  -> LumenX Script persistence
  -> dependency-ordered RenderGraph execution
  -> local mock image / video / voice / subtitle assets
  -> per-shot finalization
  -> ordered concatenation
  -> one 9:16 MP4
```

All generation remains local. Provider and external API call counts stay at
zero.

## Completion command

```bash
python -m src.apps.jp_drama.workflows.render_mock_episode \
  --input output/jp_drama/prepared/prepared_episode.json \
  --output output/jp_drama/mock_episode.mp4
```

The command derives a restart-safe work directory beside the output:

```text
output/jp_drama/.mock_episode_work/
```

It contains the LumenX project store, task outputs, execution state, concat
manifest, and validation report.

## Persistence boundary

The CLI first calls the PR5 `LumenXProjectStore`. By default it writes a
self-contained store inside the render work directory:

```text
.mock_episode_work/lumenx/projects.json
.mock_episode_work/lumenx/persistence_index.json
```

This keeps the one-command smoke render isolated while still exercising the
real PR5 adapter, validation, conflict rules, idempotency, and read-back path.
`--projects-file` and `--index-file` can point to another LumenX store.

## RenderGraph execution

`RenderGraphRunner` performs a stable topological traversal. When several tasks
are ready, it selects the earliest node from the compiler-produced graph. Since
PR4 emits nodes cut by cut, a cut is finalized before execution moves to the
next cut.

Each task records:

- task ID, shot ID, and task type
- pending, running, succeeded, or failed status
- attempt count
- deterministic input fingerprint
- relative output paths
- last failure message

Each shot records:

- shot order
- member task IDs
- current shot status
- finalized MP4 path

## Restart and retry behavior

State is atomically replaced after every transition in:

```text
.mock_episode_work/render_state.json
```

If execution stops:

1. succeeded tasks with existing non-empty outputs are reused;
2. a failed or interrupted task becomes retryable;
3. only that task and dependent downstream tasks are invalidated;
4. completed earlier cuts remain untouched;
5. the same command resumes from the first incomplete task.

`--reset` removes the work directory and final output before starting again.
An existing state with another source digest, project ID, graph fingerprint, or
output destination is rejected instead of being silently reused.

## Mock task implementations

| RenderGraph task | PR6 local implementation |
|---|---|
| `generate_image` | deterministic PPM illustration made with Python |
| `generate_video` | deterministic image plus FFmpeg motion |
| `generate_native_av` | motion video plus synthetic local audio |
| `generate_tts` | timed synthetic voice tones in WAV |
| `generate_subtitles` | Japanese ASS subtitles |
| `apply_still_motion` | FFmpeg zoom motion from a mock image |
| `mux_audio_video` | voice, simple SFX, subtitles, and video mux |
| `finalize_shot` | normalized H.264/AAC cut with a quiet mock BGM |

The generated media is intentionally schematic. Its job is to exercise file
contracts, timing, ordering, audio presence, subtitle composition, retries, and
final export—not visual quality.

## Final composition

Finalized cuts use the same dimensions, frame rate, H.264 profile, AAC sample
rate, and channel layout. They are concatenated in `StoryboardFrameDraft.order`
using an FFmpeg concat manifest.

The final output is validated with FFprobe and FFmpeg black detection.

## Validation contract

PR6 rejects the output unless all of the following are true:

- at least one video stream exists;
- at least one audio stream exists;
- dimensions are exactly 9:16;
- frame rate matches the PreparedEpisode project draft;
- duration is within 0.75 seconds of the target;
- black frames do not occupy 80% or more of the video;
- at least one subtitle artifact was generated;
- all graph tasks succeeded;
- final shot order matches storyboard order;
- external API calls equal zero.

The report is written to:

```text
.mock_episode_work/validation_report.json
```

## Determinism

Mock colors, tones, motion, task order, subtitle files, graph fingerprints, and
composition fingerprints derive only from PreparedEpisode fields and stable
hashes. A second invocation with the same input reuses all successful assets
and leaves the final MP4 unchanged.

## Failure injection

Tests may set:

```bash
JP_DRAMA_FAIL_TASK_ID=<exact task ID>
```

The selected task fails before producing output. Removing the variable and
running the same command verifies restart behavior without redoing completed
cuts.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | valid MP4 generated or safely reused |
| 1 | invalid or unreadable PreparedEpisode |
| 2 | PreparedEpisode is not generation-ready |
| 3 | LumenX persistence failure or conflict |
| 4 | graph execution, task, state, or FFmpeg failure |
| 5 | final media validation failure |

## PR7 boundary

PR7 replaces only generation handlers with real image, video, native AV, and
TTS provider adapters. It should retain:

- `RenderGraphRunner` dependency ordering;
- persisted task and shot state;
- restart and retry rules;
- per-shot artifact contracts;
- finalization and concatenation;
- output validation;
- zero-cost mock handlers for tests and fallback CI.
