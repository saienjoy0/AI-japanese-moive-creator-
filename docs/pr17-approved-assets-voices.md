# PR17 — Approved Assets and Voice Identities

## Goal

Convert GenerationPlan reference requirements into hash-bound, human-approved production assets before any paid provider stage.

```text
PreparedEpisode + GenerationPlan
  -> pending ApprovedAssetBundle
  -> master images / first-frame manifests / voice identities
  -> stage-aware readiness gate
  -> paid keyframe or render stage
```

## Assets

The bundle tracks:

- character master images
- location master images
- prop master images
- one first-frame image per Wan segment
- optional voice-reference WAV files

Every approved file stores an absolute path, SHA-256, MIME type, dimensions or duration, approver, approval time, and operation provenance.

First-frame assets reuse the PR8 `ApprovedKeyframeManifest`. The manifest, PNG dimensions, 9:16 ratio, file hash, target segment, and operation identity are all revalidated when the bundle is checked.

## Continuity lineage

Each first frame records the approved character, location, and prop master assets against which it was reviewed. A render is blocked when the first-frame lineage omits any master required by that segment.

## Voice identity

Every character that speaks receives one `VoiceIdentityProfile` with:

- provider
- voice ID
- language
- speaking rate
- pronunciation dictionary
- optional reference-audio asset

Different characters may not share the same approved provider/voice ID unless the profile explicitly permits sharing. The default is to reject sharing.

## Stage gates

- `preflight`: reports missing assets as warnings; no provider call is possible
- `keyframe` / `approve`: character, location, and prop masters must be approved
- `render`: all masters, the segment first frame, complete first-frame lineage, and speaker voices must be approved
- `full_episode`: the same checks apply to every segment in the plan

## Commands

Create pending slots:

```bash
python -m src.apps.jp_drama.workflows.prepare_asset_bundle \
  --prepared-input prepared_episode.json \
  --generation-plan generation_plan_episode.json \
  --output assets-pending.json
```

Apply reviewed files and voice bindings:

```bash
python -m src.apps.jp_drama.workflows.approve_asset_bundle \
  --input assets-pending.json \
  --bindings approval-bindings.json \
  --output assets-approved.json
```

Validate before a paid stage:

```bash
python -m src.apps.jp_drama.workflows.validate_asset_bundle \
  --bundle assets-approved.json \
  --prepared-input prepared_episode.json \
  --generation-plan generation_plan_episode.json \
  --stage full_episode
```

## Safety

- no image, video, TTS, or audio provider requests
- approved media is generated only inside CI output and is never committed
- a changed file hash invalidates readiness
- a mismatched plan or PreparedEpisode digest invalidates readiness
- pending or duplicate character voices invalidate paid render readiness
