# PR25: Operational H3 Assets and Common Segment Import

## Purpose

Close two production gaps without enabling automatic paid episode execution:

1. turn approved local PNG masters into the exact short-lived HTTPS manifest
   required by MiniMax H3;
2. validate and approve Wan, MiniMax H3, or manually generated Seedance MP4s as
   the common PR22 `SegmentArtifact` contract.

The resulting flow is:

```text
ApprovedAssetBundle
  -> zero-call H3 publication preflight
  -> explicit storage-upload approval digest
  -> private OSS object reuse/upload
  -> short-lived HTTPS signatures
  -> H3CanaryAssetManifest

Wan/H3/Seedance MP4
+ exact provider/operator evidence
  -> zero-call media and evidence preflight
  -> human approval digest
  -> SegmentArtifact
  -> SegmentArtifactManifest
  -> run_production_episode --stage compose
```

No image, video, TTS, H3, Wan, or Seedance provider request is made by this PR.
Tests use a fake object publisher and local FFmpeg fixtures only.

## H3 asset publication

### Preflight

The preflight requires:

- exact `PreparedEpisode`;
- H3 `GenerationPlanEpisode`;
- approved `ApprovedAssetBundle`;
- one H3 segment ID.

It reuses the existing asset-readiness gate and selects only the character,
location, and prop master IDs referenced by that segment. Every selected asset
must be:

- approved;
- a local PNG;
- present and non-empty;
- unchanged from its approved SHA-256;
- unchanged in width and height;
- within the H3 maximum of nine references.

The approved destination is deterministic:

```text
jp-drama/h3/<plan-digest>/<segment-id>/<asset-id>_<asset-sha>.png
```

Preflight writes a canonical digest and makes zero storage calls.

```bash
python -m src.apps.jp_drama.workflows.publish_h3_reference_assets \
  --stage preflight \
  --prepared-input output/E01/prepared_episode.json \
  --generation-plan output/E01/h3/generation_plan_episode.json \
  --asset-bundle output/E01/h3/asset_bundle_approved.json \
  --segment-id E01-G01 \
  --output-dir output/E01/h3/assets
```

### Publish

Publication is blocked unless both are supplied:

```text
--execute-upload
--approval-digest <exact preflight digest>
```

The existing private OSS utility is used through a strict adapter. Existing
objects at the approved content-addressed key are reused; missing objects are
uploaded once. Every object is verified as present and receives a short-lived
HTTPS URL.

```bash
python -m src.apps.jp_drama.workflows.publish_h3_reference_assets \
  --stage publish \
  --stored-preflight output/E01/h3/assets/E01-G01.h3-assets.preflight.json \
  --approval-digest sha256:... \
  --execute-upload \
  --output-dir output/E01/h3/assets
```

This stage may make OSS upload and signing calls, but still makes zero H3 or
video-provider calls. Secrets and signed URLs are not committed.

### Materialize

Materialization rechecks:

- preflight, plan, bundle, and segment digests;
- exact asset set and order;
- local file SHA and dimensions;
- HTTPS URL form;
- sufficient signed-URL lifetime.

It then writes the existing `H3CanaryAssetManifest` consumed by
`render_minimax_h3_segment_canary`.

```bash
python -m src.apps.jp_drama.workflows.publish_h3_reference_assets \
  --stage materialize \
  --stored-preflight output/E01/h3/assets/E01-G01.h3-assets.preflight.json \
  --published-manifest output/E01/h3/assets/E01-G01.h3-assets.published.json \
  --output-dir output/E01/h3/assets
```

Materialization makes zero storage and provider calls. It should run shortly
before H3 preflight because the URLs intentionally expire.

## Provider/operator segment import

### Evidence kinds

#### Wan Canary

Requires all three files:

- successful enriched render report;
- persistent Wan provider ledger;
- hash-bound approved first-frame manifest.

The importer verifies the report segment, route, plan digest when present,
output path, successful delegated exit, and render status. The ledger must
belong to the segment, contain only succeeded operations, and include a video
operation.

#### MiniMax H3 Canary

Requires:

- successful H3 render/resume report;
- H3 one-POST ledger in `validated` state;
- H3 request approval manifest.

The ledger must prove exactly one submission and one external API call. Its
final MP4 path and SHA-256 must match the imported file.

#### Seedance official platform

Manual Seedance output cannot claim an automated ledger. It requires meaningful
operator notes identifying that the official-platform output was reviewed.
The later import approval records the human reviewer separately.

### Media preflight

For every route, the importer reads the MP4 itself and verifies:

- exactly one video stream;
- vertical 9:16 within a small tolerance;
- readable width, height, FPS, frames, and duration;
- SHA-256;
- complete coverage of the planned `used_end_frame`;
- reasonable maximum provider handle duration;
- required final audio for native/external-audio segments;
- black-frame duration no greater than the configured threshold.

```bash
python -m src.apps.jp_drama.workflows.import_provider_segment \
  --stage preflight \
  --generation-plan output/E01/seedance/generation_plan_episode.json \
  --segment-id E01-G01 \
  --input output/provider/E01-G01.mp4 \
  --evidence-kind seedance_operator \
  --operator-notes "Official platform output reviewed for identity and timing" \
  --output-dir output/provider/imports
```

### Human approval

Approval requires the stored preflight, its exact digest, and reviewer name.
The entire media/evidence inspection is repeated. A changed MP4, report, ledger,
approval manifest, plan, or operator note invalidates the approval.

```bash
python -m src.apps.jp_drama.workflows.import_provider_segment \
  --stage approve \
  ...same media and evidence arguments... \
  --stored-preflight output/provider/imports/E01-G01.import.preflight.json \
  --preflight-digest sha256:... \
  --approved-by operator-reviewer \
  --output-dir output/provider/imports
```

Outputs:

```text
E01-G01.import.approval.json
E01-G01.segment_artifact.json
```

The common `SegmentArtifact` includes the plan and route, MP4 path/SHA/media
facts, import approval digest, reviewer, and automated ledger path where one
exists.

## Artifact manifest

One episode requires artifacts in exact GenerationPlan order.

```bash
python -m src.apps.jp_drama.workflows.assemble_segment_artifacts \
  --generation-plan output/E01/seedance/generation_plan_episode.json \
  --artifact output/imports/E01-G01.segment_artifact.json \
  --artifact output/imports/E01-G02.segment_artifact.json \
  --artifact output/imports/E01-G03.segment_artifact.json \
  --artifact output/imports/E01-G04.segment_artifact.json \
  --artifact output/imports/E01-G05.segment_artifact.json \
  --output output/E01/segment_artifacts.json
```

Missing, extra, reordered, cross-plan, or wrong-route artifacts are rejected.
The resulting manifest is ready for the zero-call exact-frame composer added in
PR22.

## Safety properties

- H3 publication targets are content-addressed and pre-approved.
- Storage upload is opt-in and digest-gated.
- Existing OSS objects are reused instead of uploaded twice.
- Signed URLs must be HTTPS and retain a minimum lifetime.
- No provider MP4 is trusted from metadata alone.
- Automated outputs require their original report, ledger, and request/first-
  frame approval.
- Manual output cannot impersonate automated evidence.
- Import approval is invalidated by any changed bytes or evidence.
- Every valid SegmentArtifact requires its own approval digest.
- No automatic provider fallback or retry is introduced.

## Remaining production gap

Wan first-frame generation can technically pass reference images through the
existing Wan 2.7 adapter, but the current canary executor still builds the first
frame from text only. The next PR must pass the exact approved
character/location/prop masters into `ref_image_paths`, bind the resulting
first-frame approval lineage to those master IDs, and then return the completed
Wan MP4 through this SegmentArtifact importer.

After that, the common provider dispatcher may connect approval-gated H3 and
Wan executions to full-episode production. Paid execution remains fail-closed
until that work is complete.
