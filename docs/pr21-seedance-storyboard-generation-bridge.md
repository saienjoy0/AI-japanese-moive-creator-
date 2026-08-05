# PR21 — Seedance Storyboard to Asset Bundle and Provider Generation Plans

## Decision

PR20 already introduced the correct creative interchange contract:
`SeedanceStoryboardPackage`. This phase does **not** add another storyboard JSON
schema and does not send the imported Markdown back through the normal Japanese
script LLM ingestion path.

```text
Pinned upstream Markdown
  -> PR20 SeedanceStoryboardPackage
  -> deterministic bridge
     -> PreparedEpisode identity
     -> H3 GenerationPlanEpisode
     -> Wan GenerationPlanEpisode
     -> manual Seedance GenerationPlanEpisode
     -> existing pending ApprovedAssetBundle per plan
```

The bridge is deliberately thin. The upstream storyboard owns creative timing,
C/S/P prompts, upload slots, sound, ending frames, and continuation language.
The existing Japanese-drama production layer owns provider capability checks,
asset approval, pricing, ledgers, paid authorization, execution, trimming, and
final validation.

## Why this phase is required

PR20 ends at `seedance_storyboard_package.json` and an operator Markdown
manifest. The existing `build_pending_asset_bundle()` accepts only a
`PreparedEpisode` plus a `GenerationPlanEpisode`; H3, Wan, and Seedance adapters
also consume provider-neutral generation specifications rather than native
upstream Markdown.

Without this bridge:

- C/S/P prompts cannot become approved master-asset slots;
- H3 cannot receive the complete timed multi-shot reference prompt;
- Wan cannot split the storyboard at proven semantic timeline boundaries;
- the manual Seedance route cannot be represented in the same readiness and
  cost reports;
- the approved asset and paid-execution gates cannot bind back to the imported
  storyboard digest.

## Provider-specific compilation

### MiniMax H3

- one upstream 15-second episode becomes one H3 generation segment;
- every upstream timed beat becomes an `EditorialShot` inside that segment;
- the original raw timed prompt is retained verbatim;
- C/S/P upload assets become H3 reference-image slots;
- upstream sound becomes native-audio intent when present;
- the current H3 capability contract validates multi-shot reference AV.

### Wan 2.7

- the bridge splits only at explicit upstream timeline boundaries;
- each timed beat becomes one single-shot Wan segment;
- every segment receives a required approved first-frame slot;
- C/S/P assets remain shared continuity masters;
- upstream sound is reported as requiring later audio postproduction;
- no fixed-time or LLM-invented boundary is introduced.

### Seedance platform

- the original raw prompt remains the authoritative operator payload;
- the plan contains one manual operator operation per upstream episode;
- the internal `EditorialShot` is collapsed to one operation so the bridge does
  not falsely change the registered `multi_shot=false` platform capability;
- the complete timed prompt remains inside `PromptBundle.timed_shot_prompt`;
- price remains explicitly unknown and no browser automation is added.

## Asset Bundle reuse

The bridge reuses the existing hash-bound `ApprovedAssetBundle` contract.

- `Cxx` -> `character_master`
- `Sxx` -> `location_master`
- `Pxx` -> `prop_master`
- Wan segment -> `first_frame`

Each generated pending bundle is bound to both:

- the canonical bridge `PreparedEpisode` digest;
- the provider-specific `GenerationPlanEpisode` digest.

Changing the storyboard package, plan, or approved file therefore invalidates
readiness through the existing gates.

## Continuation boundary

The upstream `将@视频1延长15s` instruction is retained in the raw prompt and
mapped to `transition_in=continuation` plus an explicit readiness warning.

This PR does not weaken the current approved-asset schema by pretending that a
previous video is an image master. A later focused extension must add a
hash-bound approved video-reference role before continuation can be submitted
automatically. Until then the instruction remains visible and cannot be lost.

## CLI

```bash
python -m src.apps.jp_drama.workflows.prepare_seedance_storyboard_generation \
  --input output/jp_drama/seedance-storyboard/seedance_storyboard_package.json \
  --output-dir output/jp_drama/seedance-storyboard-production \
  --routes h3 wan seedance \
  --live-provider-config examples/jp_drama/dashscope_live_providers.json \
  --minimax-h3-config examples/jp_drama/minimax_h3_live_provider.json \
  --print-report
```

Outputs:

```text
seedance-storyboard-production/
├── bridge_report.json
├── summary.md
├── E01/
│   ├── prepared_episode.json
│   ├── h3/
│   │   ├── generation_plan_episode.json
│   │   └── asset_bundle_pending.json
│   ├── wan/
│   │   ├── generation_plan_episode.json
│   │   └── asset_bundle_pending.json
│   └── seedance/
│       ├── generation_plan_episode.json
│       └── asset_bundle_pending.json
└── E02/...
```

The complete directory is staged and then published as one replacement. The
same package, episode, route, and configuration produce deterministic IDs and
digests.

## Safety

- no LLM request;
- no image, video, TTS, or audio generation;
- no provider credential read by the bridge;
- no provider submission, polling, or download;
- provider cost estimates use existing dated configurations only;
- Seedance remains manual;
- fallback execution remains disabled;
- media quality remains `false` until a separately approved Canary.

## Completion evidence

The existing two-episode fixture must prove:

- H3: one 15-second segment with five timed editorial shots;
- Wan: five single-shot segments and five first-frame requirements;
- Seedance: one manual operator segment retaining the full raw prompt;
- C01, C02, S01, and the episode prop become pending approved masters;
- E02 continuation text and transition survive;
- all artifacts bind to source and plan digests;
- external provider calls remain zero.

## Next production step

After this bridge passes CI, use one real project package—starting with
`一房の葡萄`—to create and approve the C/S/P masters, then run one explicitly
authorized H3 or Wan segment Canary. Full-episode paid execution remains behind
the existing approval and budget gates.
