# Japanese Short-Drama Domain Models

PR3 introduces a versioned JSON contract for the editorial stages that happen
before LumenX generates images, video, speech, subtitles, and the final MP4.

The new code is isolated in `src/apps/jp_drama/`. It does not replace or alter
the upstream `src/apps/comic_gen/models.py` contracts.

## Pipeline contract

```text
SourceRecord
  ↓
BeatSheet
  ↓
JapaneseAdaptation
  ↓
EpisodePlan
  ↓
ShotPlan
  ↓
CostPlan
```

`EpisodePackage` contains one instance of every stage and validates the links
between them.

## Stage responsibilities

### SourceRecord

Captures where the Chinese reference material came from, its language, rights
status, provenance notes, usage restrictions, and attached source assets. The
reference material is evidence for analysis, not a script that may be copied.

### BeatSheet

Stores the reusable dramatic structure: opening hook, setup, escalation,
reveal or reversal, payoff or cliffhanger, emotional promise, and source
evidence that must be transformed. Beat IDs remain stable across later stages.

### JapaneseAdaptation

Stores Japanese creative decisions separately from source analysis: title,
logline, audience, setting, adapted cast, beat transformations, cultural
changes, originality review, and elements that must not be copied. A
high-similarity adaptation cannot be marked approved.

### EpisodePlan

Defines episode-level production constraints: series and episode identity,
30–90 second duration, fixed 9:16 aspect ratio, frame rate, opening and closing
hooks, cast, locations, and props.

### ShotPlan

Defines contiguous shot order, duration, adapted beat reference, cast,
location, props, action, timed dialogue, camera intent, audio intent, render
strategy, and continuity notes. The shot total must match the episode target
within the declared tolerance.

Available render strategies are:

- `native_av`
- `video_plus_tts`
- `silent_video`
- `still_motion`
- `existing_asset`

### CostPlan

Creates a budget contract before paid generation starts: currency, hard budget,
provider and model per shot, primary cost, retry reserve, maximum attempts,
fallback strategy, and contingency. When `hard_stop=true`, an over-budget
package is invalid.

## Cross-stage validation

`EpisodePackage` rejects a package when:

- a stage points to another source, beat sheet, adaptation, or episode
- adaptation mappings do not cover all source beats
- a shot references an unknown adapted beat
- an episode references a character absent from the adaptation
- a shot references a character, location, or prop absent from the episode
- cost estimates do not cover every shot exactly once
- the shot duration total does not match the episode duration
- the hard budget is exceeded

These checks make workflow failures explicit before an image or video model is
called.

## Versioning and strictness

The first schema version is `1.0.0`. Breaking field or validation changes
require a schema-version increment. Unknown fields are rejected with
`extra="forbid"`; they are not silently discarded.

## Example and tests

The checked-in example is:

```text
examples/jp_drama/minimal_episode_package.json
```

Run the focused validation with:

```bash
python -m pytest -q tests/test_jp_drama_domain.py
```

The canonical JSON serializer excludes computed reporting fields, allowing its
output to be parsed back into the strict models without modification.

## Integration boundary with LumenX

This PR does not yet convert `Shot` into LumenX `StoryboardFrame` or call a
model provider. That adapter belongs in a later PR.

```text
EpisodePackage
  ↓ adapter
LumenX Project / Character / Scene / Prop / StoryboardFrame
  ↓ existing LumenX pipeline
generated assets and final media
```

Keeping this boundary explicit lets upstream LumenX models evolve without
forcing Japanese editorial data into the upstream schema.
