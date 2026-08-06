# PR31: HappyHorse 1.1 R2V Auto Reference Canary

## Purpose

Add a minimal, approval-gated HappyHorse 1.1 R2V execution route for the
Japanese-drama pipeline without making additional first-frame keyframes.

The route reuses the existing:

- `PreparedEpisode` and `GenerationPlanEpisode` contracts;
- approved `AssetBundle` master images;
- provider task ledger and immediate task-ID persistence;
- restart-safe polling and download;
- MP4 validation and later `SegmentArtifact` import.

The first production target is `E01-G01` from 『一房の葡萄』.

## Flow

```text
approved source plan + approved asset bundle
  -> derive HappyHorse R2V plan and bundle
  -> select ordered C/S/P references from reference_asset_ids
  -> verify approval, local bytes, SHA-256, PNG dimensions, count <= 9
  -> build [Image N]-bound prompt
  -> bind model/region/endpoint/price/ratio/prompt/assets into approval digest
  -> preflight with zero provider calls
  -> explicit render approval
  -> publish temporary provider media references
  -> submit at most one paid task
  -> save task ID before polling
  -> poll/resume/download
  -> validate vertical MP4, duration, audio strategy and black frames
```

## Added modules

### `assets/reference_resolution.py`

Separates stable selection evidence from temporary provider URLs.

- `ReferenceSelectionManifest` is the approval-bound, immutable selection.
- `PublishedReferenceManifest` contains runtime URLs and an operational lease.
- references are taken deterministically from `GenerationSegment.reference_asset_ids`;
- duplicate IDs, duplicate SHA values, missing files, modified files and more
  than nine references fail closed;
- native audio does not require a fixed voice ID;
- external TTS requires an approved `VoiceIdentityProfile`.

### `generation/happyhorse_r2v.py`

Derives a provider-specific R2V plan and bundle from an existing approved plan.
It removes first-frame requirements, preserves character/location/prop order,
binds the route to `dashscope/happyhorse-1.1-r2v`, records one priced video task
per segment, and leaves the original plan unchanged.

### `production/reference_prompt.py`

Builds generic R2V prompts with contiguous `[Image 1] ... [Image N]` bindings.
The generic builder uses editorial shots; a separate creative override may
supply a more detailed timeline without embedding title-specific logic in code.

### `rendering/happyhorse11.py`

Introduces `HappyHorse11AsyncTransport` shared by separate exact I2V and R2V
contracts. R2V accepts one to nine ordered images and explicitly sends
`ratio=9:16`. Existing I2V remains first-frame-only and is covered by regression
tests.

### `rendering/happyhorse_r2v_contract.py`

The approval manifest binds:

- model and route;
- plan, bundle, selection and prompt digests;
- ordered asset IDs and SHA-256 values;
- region, endpoint origin and workspace hashes;
- resolution, `ratio=9:16`, duration, seed and watermark;
- audio strategy;
- price snapshot and quoted CNY cost;
- exactly one billable provider task.

Temporary URLs and credentials are deliberately excluded from this digest.

### `workflows/render_happyhorse_r2v_segment_canary.py`

Stages:

- `preflight`: validates and writes the exact approval manifest; provider calls = 0.
- `render`: requires the exact approval digest and can create at most one task.
- `resume`: uses only a stored provider task ID and never creates another task.

A non-succeeded task older than the 23-hour guard becomes
`expired_unrecoverable`. The workflow never retries or resubmits automatically.

## E01-G01 contract

`assets/jp_drama/one_bunch_of_grapes/creative_overrides/E01-G01.r2v.json`
retains the approved ten-second structure:

1. 0.0–2.0s: unfinished muddy harbor painting;
2. 2.0–4.0s: vivid Yokohama harbor memory;
3. 4.0–6.5s: cheap colors become muddy gray;
4. 6.5–10.0s: the boy becomes disappointed and looks to the next desk.

The source GenerationPlan must explicitly include `S05`. The resolver does not
contain an `E01-G01` hard-coded asset insertion.

## Safety properties

- no paid provider call in normal CI;
- no first-frame requirement for R2V;
- no automatic reference pruning;
- no duplicate media selection;
- no approval reuse after prompt, asset, endpoint, region or price changes;
- no second POST after a task ID is known;
- no automatic retry after ambiguous failure or task expiry;
- output must be close to vertical 9:16 and pass duration/audio/black-frame checks.

## Tests

Focused CI runs:

```bash
python -m pytest -q \
  tests/test_jp_drama_happyhorse_official_canary.py \
  tests/test_jp_drama_happyhorse11_r2v.py \
  tests/test_jp_drama_happyhorse_r2v_contracts.py
```

It also rebuilds and validates the canonical model catalog, uploads generated
backend/frontend catalog artifacts, compiles all new modules, validates the
committed E01-G01 override, and asserts that provider credentials are absent.

## Not included

- paid E01-G01 generation;
- automatic voice casting;
- full 15-segment execution;
- automatic fallback or retry;
- automatic two/four-way segmentation;
- automatic extra keyframe creation;
- PR creation or merge.

The next production action after CI and review is a separately authorized,
one-task E01-G01 paid canary.
