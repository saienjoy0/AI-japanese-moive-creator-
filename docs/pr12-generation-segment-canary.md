# PR12 Generation Segment Canary

## Purpose

Connect one PR11 `GenerationSegment` from a real script-derived generation plan to the proven PR8 Wan 2.7 Canary path.

```text
TXT / MD script
  -> EpisodePackage
  -> PreparedEpisode
  -> GenerationPlanEpisode
  -> selected GenerationSegment
  -> deterministic one-segment PreparedEpisode
  -> PR8 preflight / keyframe / approve / render
```

PR12 reuses the existing persistent provider ledger, cost ceiling, approval manifest, task-ID resume behavior, subtitles, muxing, and media validation. It does not create a second paid execution system.

## Safety rules

Before any provider submission, PR12 verifies:

- the generation plan digest belongs to the supplied `PreparedEpisode`
- the selected segment exists exactly once
- the current route is `wan/i2v`
- the segment has exactly one parent source shot
- native audio is not falsely routed through the current Wan I2V executor
- the requested segment duration does not exceed `provider_clip_seconds`
- a multi-editorial-shot segment requires `--allow-experimental-multi-shot`

A segment longer than the configured provider duration is rejected. It is never silently truncated to five seconds.

## Materialization

The bridge creates a deterministic one-segment `PreparedEpisode`:

- `source_shot_id` becomes the PR11 `segment_id`
- provider request duration is preserved
- PR11 visual, motion, and timed-shot prompts replace the wider source-shot prompt
- dialogue is offset by the segment's provider trim handle
- character, location, and prop seeds are reduced to the selected segment
- render task IDs are namespaced by the segment ID
- source identity includes the generation-plan digest and selected segment

The provider request clip still contains PR11's trim handles. PR12 reports `used_start_frame` and `used_end_frame`; final episode trimming remains a later full-run responsibility.

## CLI

Zero-cost preflight:

```bash
python -m src.apps.jp_drama.workflows.render_generation_segment_canary \
  --prepared-input output/jp_drama/prepared/prepared_episode.json \
  --generation-plan output/jp_drama/generation/generation_plan_episode.json \
  --segment-id SEGMENT_ID \
  --output output/jp_drama/canary/segment.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --stage preflight \
  --report output/jp_drama/canary/preflight.json \
  --print-report
```

Keyframe:

```bash
python -m src.apps.jp_drama.workflows.render_generation_segment_canary \
  --prepared-input output/jp_drama/prepared/prepared_episode.json \
  --generation-plan output/jp_drama/generation/generation_plan_episode.json \
  --segment-id SEGMENT_ID \
  --output output/jp_drama/canary/segment.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --stage keyframe
```

After visual review:

```bash
python -m src.apps.jp_drama.workflows.render_generation_segment_canary \
  --prepared-input output/jp_drama/prepared/prepared_episode.json \
  --generation-plan output/jp_drama/generation/generation_plan_episode.json \
  --segment-id SEGMENT_ID \
  --output output/jp_drama/canary/segment.mp4 \
  --providers examples/jp_drama/dashscope_live_providers.json \
  --stage approve
```

Then render with the same inputs and `--stage render`.

## Experimental multi-shot flag

The PR9 Wan route currently declares `multi_shot=false`. A segment containing multiple internal editorial shots is therefore blocked by default. `--allow-experimental-multi-shot` exists only to run an explicitly approved Canary and gather evidence; it does not upgrade the provider capability declaration.

## Completion boundary

PR12 proves the software bridge and zero-cost preflight contract. A real visual-quality claim requires an operator-owned API credential and an explicitly authorized paid keyframe/render run. Full-episode automatic rendering remains out of scope until the first segment evidence is accepted.
