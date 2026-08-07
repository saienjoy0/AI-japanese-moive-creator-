---
name: jp-drama-segment-production
description: Evidence-based workflow for preparing, preflighting, running, and reporting one Japanese-drama video segment without confusing image generation, preflight, or planned work with a completed paid video render.
---

# Japanese Drama Segment Production Workflow

Use this workflow when the user asks to prepare, register, preflight, generate, review, or continue a Japanese-drama segment such as `E01-G04`, including HappyHorse, Wan, MiniMax H3, or continuity-frame work.

## Reuse Existing Project Contracts

Do not create replacement approval, ledger, budget, artifact, or continuity systems. Use the repository's existing:

- approved keyframe manifest and SHA binding;
- ApprovedAssetBundle and reference lineage checks;
- zero-call preflight stage;
- provider fingerprint reservation;
- persistent Provider Ledger;
- one-submission and CNY budget limits;
- render report and media validation;
- SegmentArtifact contract;
- automatic end-frame extraction and metadata.

## Minimal Execution Order

1. Inspect the current segment's GenerationPlan, required master assets, transition mode, and previous verified artifact.
2. Reuse an existing successful segment workflow as the implementation pattern. Do not invent a parallel route when the existing Canary/hosted route can be adapted.
3. Register the approved keyframe through the existing approval and asset-bundle path.
4. Run contract tests and zero-call preflight before any paid provider submission.
5. Confirm the exact request fingerprint, references, duration, ratio, model, call ceiling, and cost ceiling.
6. Start the paid workflow only through its explicit owner-only trigger and only once.
7. Read the Action run, reservation record, Provider Ledger, provider task identity, render report, uploaded artifact, MP4, and extracted end frame before reporting completion.
8. Stop after the requested segment. Do not generate the next segment without explicit authorization.

## Evidence Rules

Never advance the reported state beyond the available evidence.

- An image-generation tool call proves only that an image was generated. It does not prove that a video provider was called.
- A branch proves only that a branch exists. It does not prove that files were committed or a workflow exists.
- A commit proves only that repository content changed. It does not prove that CI, preflight, or a paid render ran.
- A successful zero-call preflight proves readiness only. It does not prove provider submission.
- A fingerprint reservation proves that the request was reserved. It does not by itself prove that the provider accepted a task.
- A provider task ID or a Provider Ledger record in a submission-consuming state is required before saying that the paid video API was called.
- A successful render report and verified MP4 artifact are required before saying that video generation succeeded.
- A verified extracted end-frame PNG and metadata are required before saying that continuity was handed to the next segment.
- A local or sandbox link may be presented only after the exact file path has been confirmed to exist and be non-empty in the active runtime.

## Required Status Vocabulary

Use one of these factual stages in progress reports:

- `KEYFRAME_DRAFTED`
- `KEYFRAME_APPROVED`
- `REGISTERED`
- `PREFLIGHT_PASSED`
- `PAID_RESERVED`
- `PROVIDER_SUBMITTED`
- `PROVIDER_RUNNING`
- `MEDIA_VERIFIED`
- `HUMAN_ACCEPTED`

Do not use `MEDIA_VERIFIED` unless all of the following have been inspected:

- Action `run_id`;
- Provider Ledger submission count and operation state;
- provider `task_id` when the provider supplies one;
- `render.json` or the route's equivalent valid report;
- named Actions artifact;
- non-empty MP4 whose SHA and media properties match the report;
- required end-frame PNG and metadata when continuity extraction is part of the workflow.

## Mandatory Report Fields

For execution-related updates, report only verified values and include:

```text
state:
run_id:
provider task_id:
provider submissions:
render valid:
artifact name:
MP4 verified:
end frame verified:
paid video API called: yes/no/unknown
```

Use `unknown` rather than guessing. Clearly distinguish image-generation calls from paid video-generation calls.

## Anti-Misreporting Gate

Before writing phrases such as "generated", "render succeeded", "API was called", "artifact is ready", or "the next continuity frame was extracted", verify the corresponding evidence above. If evidence is incomplete, state the highest proven stage and the missing evidence instead.

Never fabricate or infer:

- a run ID;
- task or request ID;
- provider submission count;
- artifact name;
- MP4 path or download link;
- output SHA;
- end-frame path;
- API cost or billing event.

## Avoiding Unnecessary Detours

- Prefer the shortest existing repository-native path.
- Do not design a new framework while executing one segment.
- Do not repeatedly rescan unrelated files after the relevant workflow, approval, ledger, and artifact contracts are identified.
- Do not generate multiple resized or encoded copies of an approved image unless an existing repository contract explicitly requires them.
- If the available connector cannot complete a binary-file write, stop at the exact blocked stage and report the limitation; do not represent planned downstream steps as completed.
