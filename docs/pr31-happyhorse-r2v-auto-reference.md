# PR31 Minimal HappyHorse 1.1 R2V Canary

## Decision

The next useful project step is one approval-gated `E01-G01` R2V Canary.

The project already has:

- three episodes and fifteen `GenerationSegment` records,
- seventeen human-approved master images,
- ordered segment reference IDs,
- `WanMasterReferenceManifest` validation,
- persistent provider ledgers and task-ID resume,
- provider MP4 inspection and episode composition.

The missing evidence is whether HappyHorse 1.1 R2V can preserve the approved
characters, classroom, props, Yokohama-memory location, motion order, and
Japanese native audio in one ten-second clip.

This PR therefore does not implement a full-episode dispatcher.

## Superseded work

The former four-I2V-clip approach in PR #31 was closed without merging.

It required additional keyframes and four provider tasks before the project had
proved that one R2V task was usable. That was the wrong order.

## Architecture

```text
PreparedEpisode
+ existing Wan GenerationPlan
+ approved AssetBundle
        |
        v
build_wan_master_reference_manifest()
        |
        v
ordered approved C / S / P images (1..9)
        |
        v
existing HappyHorse Canary --input-mode references
        |
        v
HappyHorse11R2VModel
        |
        v
persistent ledger / task ID / resume / MP4 validation
```

## Why the dedicated adapter is used

`src/models/wanx.py` can submit generic HappyHorse tasks, but its generic
HappyHorse path does not resume from a saved provider task ID.

The production Canary must keep the PR30 safety contract:

- save task ID before polling,
- do not issue another paid POST during resume,
- bind the ledger to the exact reviewed request,
- reuse an already verified output.

Therefore R2V is added beside `HappyHorse11I2VModel` in
`src/apps/jp_drama/rendering/happyhorse11.py`.

## Scope

Changed files:

```text
src/apps/jp_drama/rendering/happyhorse11.py
src/apps/jp_drama/workflows/render_happyhorse_segment_canary.py
tests/test_jp_drama_happyhorse_official_canary.py
docs/pr31-happyhorse-r2v-auto-reference.md
```

No new GitHub Actions workflow is added. The existing official HappyHorse
workflow already watches the implementation and test paths.

## R2V request

```json
{
  "model": "happyhorse-1.1-r2v",
  "input": {
    "prompt": "[Image 1] ...",
    "media": [
      {"type": "reference_image", "url": "oss://..."}
    ]
  },
  "parameters": {
    "resolution": "720P",
    "ratio": "9:16",
    "duration": 10,
    "watermark": false,
    "seed": 12345
  }
}
```

Constraints:

- one to nine ordered reference images,
- only vertical `9:16` in this Canary,
- duration from three to fifteen seconds,
- 720P or 1080P,
- duplicate and empty provider media inputs rejected.

## Input modes

### `first_frame`

The existing PR30 I2V behavior remains the default and is regression-tested.

### `references`

The Canary:

1. loads the existing Wan GenerationPlan,
2. builds the existing approved master-reference manifest,
3. keeps the GenerationPlan order,
4. adds one `[Image N]` description per reference,
5. submits `happyhorse-1.1-r2v`,
6. records provider IDs before polling,
7. resumes from the saved task without another POST.

## E01-G01 gate

For `E01-G01`, the manifest must contain subject IDs:

```text
C01
S01
P03
P04
S05
```

The code does not hardcode concrete asset IDs. The GenerationPlan remains the
source of truth.

## Request binding

The ledger source digest is the exact request fingerprint, including:

- protocol,
- model,
- GenerationPlan digest,
- AssetBundle digest,
- segment ID,
- prompt,
- ordered asset IDs,
- ordered asset SHA-256 values,
- resolution,
- ratio,
- duration,
- seed.

Temporary provider URLs are deliberately excluded.

## Native audio

The R2V Canary does not require a fixed voice ID.

Voice identity uncertainty is reported as a warning. Missing or changed master
assets remain hard errors.

The returned MP4 must contain an audio stream. Human review decides whether the
provider-native Japanese voice and lip synchronization are acceptable.

## Output validation

A successful MP4 must have:

- exactly one video stream,
- vertical 9:16 dimensions,
- at least the requested duration within tolerance,
- an audio stream,
- no more than 0.25 seconds of black frames,
- a stable SHA-256 recorded in the ledger.

## Zero-call implementation boundary

Normal tests and CI:

- have no provider credentials,
- make zero paid provider calls,
- use fake POST and poll responses,
- verify task-ID persistence order,
- verify resume makes zero POST requests.

Actual `--stage render` remains an operator-only later action.

## Acceptance tests

- I2V payload and no-ratio behavior remain unchanged.
- R2V uses ordered `reference_image` media.
- Zero and ten references fail before submission.
- R2V is fixed to 9:16.
- Task ID is saved before polling.
- Resume performs no second POST.
- The reference prompt binds every image in order.
- E01-G01 rejects a manifest without S05.
- Reference-order changes produce a different request fingerprint.
- Native voice warnings never relax asset failures.

## Not included

- paid API execution,
- SegmentArtifact importer changes,
- ProviderRegistry route changes,
- full-episode dispatch,
- external TTS,
- voice auto-casting,
- automatic fallback,
- additional keyframes,
- four-clip assembly.

## Next decision after CI

After this PR is merged, run exactly one explicitly authorized E01-G01 R2V
Canary.

- If visual and native-audio quality are acceptable, add the minimal HappyHorse
  evidence kind to the existing SegmentArtifact importer.
- If quality is unacceptable, do not expand R2V; retain the existing H3 or I2V
  path.
