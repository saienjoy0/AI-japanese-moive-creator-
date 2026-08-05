# PR15 — Semantic ActionBeat and Wan Single-Shot Planning

## Goal

Remove the remaining fixed-time segmentation blockers by requiring explicit semantic action boundaries in the normal Japanese script contract.

```text
Japanese script
  -> story Beats
  -> semantic ActionBeats
  -> one deterministic EpisodePackage Shot per ActionBeat
  -> PreparedEpisode
  -> Wan provider units containing one EditorialShot each
```

## ActionBeat boundary evidence

An ActionBeat must use one of:

- scene change
- speaker change
- completed physical action
- character reaction
- camera reframe

A boundary may not be invented only because a fixed number of seconds elapsed.

## Dialogue invariant

Every parent Beat dialogue line is referenced by one-based `dialogue_indexes` and must be assigned to exactly one ActionBeat. Missing and duplicate assignments are validation errors and receive at most one LLM repair attempt through the existing ingestion service.

## Duration invariant

The LLM proposes relative ActionBeat durations between 0.5 and 15 seconds. The deterministic compiler rescales the complete list to the exact episode duration in integer milliseconds, using stable largest-remainder allocation.

The compiler rejects output when any scaled ActionBeat falls outside 0.5–15 seconds.

## Wan rule

The Wan profile sets `max_internal_editorial_shots=1`. Dialogue boundaries may create several provider units, but every unit contains exactly one EditorialShot. Short units use provider handles rather than being merged into a multi-shot request.

## Safety

- no image, video, TTS, or audio provider calls
- no credentials in CI
- no generated media committed
- no experimental multi-shot flag
- no change to Seedance capability declarations

## Completion evidence

The normal 45-second fixture must produce:

- 3 story Beats
- 9 semantic ActionBeats
- 9 EpisodePackage Shots totaling exactly 45 seconds
- a route-ready Wan GenerationPlan
- one EditorialShot in every GenerationSegment
- exact dialogue coverage
- zero `insufficient_segmentation_evidence`
- zero `route_multi_shot_not_migrated`
- zero provider calls
