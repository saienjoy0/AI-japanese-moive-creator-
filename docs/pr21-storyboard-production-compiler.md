# PR21 — Seedance Storyboard to Production Compiler

## 1. Purpose

Connect the already imported professional Seedance2 Storyboard Generator output to the existing Japanese-drama production contracts without re-inventing or re-directing its creative decisions.

```text
SeedanceStoryboardPackage
  ├─ C/S/P asset catalogue
  ├─ per-episode upload slots
  ├─ upstream-authored timed storyboard
  ├─ sound instructions
  ├─ ending frame
  └─ continuation instruction
        ↓
StoryboardProductionCompiler
        ├─ pending ApprovedAssetBundle per route and episode
        ├─ MiniMax H3 GenerationPlanEpisode
        ├─ Wan I2V GenerationPlanEpisode
        ├─ Seedance platform GenerationPlanEpisode
        └─ cross-episode continuation manifest
```

No LLM, image, video, speech, or paid-provider call occurs in this PR.

## 2. Four-expert decision

### Short-drama director

The upstream project already made the directing decisions. The compiler must preserve:

- exact timed-beat boundaries
- the full upstream visual-action text
- C/S/P upload order
- critical props
- sound text
- ending-frame text
- previous-video continuation

It must not replace the authored storyboard with mechanical camera cycling or dialogue-boundary duplication.

### Generative-video and prompt specialist

Provider differences belong only in provider compilers:

- H3 receives one multi-shot reference-video segment per upstream episode.
- Seedance receives one native multi-shot manual-platform segment, retaining the original prompt.
- Wan receives one I2V segment per upstream timed beat because its current production route is single-shot.

The upstream prose remains the source of truth. Provider compilers may relabel references, but may not invent new staging.

### Pipeline architect

The existing `ApprovedAssetBundle` remains the approval boundary. This PR creates pending bundles whose assets use existing roles:

- C → `character_master`
- S → `location_master`
- P → `prop_master`
- Wan shot start → `first_frame`

Every plan and bundle is digest-bound. Cross-episode dependencies that do not fit inside one `GenerationPlanEpisode` are stored in a deterministic production manifest.

### Quality and paid-execution safety specialist

Planning and paid execution stay separate:

- all generated assets are `pending`
- no fake file path, SHA, dimensions, or approval is invented
- no provider credential is read
- no submit/poll/download method is called
- route limits are checked in tests through the existing adapters
- a missing C/S/P reference is a compilation error
- a continuation source without a previous episode is a compilation error

## 3. Current position

### Already complete

- normal Japanese script ingestion
- deterministic EpisodePackage and PreparedEpisode pipelines
- adaptive GenerationPlan contracts
- approved reference-asset and voice bundles
- H3 charge-safe one-segment execution
- Wan planning and existing executor boundary
- manual Seedance platform route
- native Seedance2 Storyboard Generator Markdown import

### Missing before this PR

- no compiler from `SeedanceStoryboardPackage` to `GenerationPlanEpisode`
- no C/S/P-to-Asset-Bundle conversion
- no provider-specific preservation of the upstream storyboard
- no continuation mapping from `将@视频1延长Xs`
- Seedance adapter still declared multi-shot and continuation unsupported
- provider-neutral request conversion ignored video-reference IDs

## 4. Goal state

Given the checked-in `雨の新店長` fixture, one command produces:

```text
production_manifest.json
minimax_h3/E01/generation_plan_episode.json
minimax_h3/E01/asset_bundle.json
minimax_h3/E02/generation_plan_episode.json
minimax_h3/E02/asset_bundle.json
wan_i2v/E01/generation_plan_episode.json
wan_i2v/E01/asset_bundle.json
wan_i2v/E02/generation_plan_episode.json
wan_i2v/E02/asset_bundle.json
seedance_platform/E01/generation_plan_episode.json
seedance_platform/E01/asset_bundle.json
seedance_platform/E02/generation_plan_episode.json
seedance_platform/E02/asset_bundle.json
```

The outputs must prove:

- `P01` remains a required prop in E02
- all five upstream beats remain five editorial shots for H3 and Seedance
- Wan produces five single-editorial-shot segments
- H3 and Seedance E02 carry an E01 video-reference dependency
- Wan E02 carries an E01 ending-frame dependency
- every pending bundle uses the same requirement IDs as its plan
- all route requests validate without paid calls

## 5. Provider mapping

### MiniMax H3

Route: `minimax/h3-reference-av`

- one segment per upstream episode
- all C/S/P upload assets become reference inputs
- continuation becomes `video_reference`
- multi-shot timeline is preserved
- upstream sound remains native-audio prompt text

### Seedance

Route: `seedance/platform`

- one segment per upstream episode
- original `raw_prompt` is retained as the timed prompt
- C/S/P order follows the upload table
- continuation becomes a previous-video reference
- execution remains manual and operator-gated

### Wan

Route: `wan/i2v`

- one segment per upstream timeline beat
- each segment has exactly one editorial shot
- every segment requires a `first_frame`
- the first frame is planned from approved C/S/P assets
- later segment first frames depend on the previous segment ending frame
- next-episode first frame depends on the previous episode ending frame when continuation is requested
- sound is retained as planning metadata, but this PR does not invent structured TTS dialogue

## 6. Data integrity rules

- source package digest is retained as the source digest
- output IDs are deterministic across repeated runs
- each episode must contain at least one character and one scene upload
- no upload slot may refer to an absent C/S/P asset
- H3 and Seedance may use at most nine image references
- Wan timed beats must remain between provider duration limits
- output plans cover the exact upstream episode duration
- requirements and pending-bundle assets must be one-to-one
- no approved status may be emitted without real media evidence

## 7. Non-goals

- no image generation
- no paid video generation
- no automatic H3-to-Wan fallback
- no dialogue extraction from free-form upstream prose
- no subtitle generation
- no BGM or final episode assembly
- no modification of the upstream creative Skill

## 8. Acceptance tests

1. deterministic compilation produces identical digests
2. H3 plan validates through `MiniMaxH3Adapter`
3. Seedance plan validates through `SeedancePlatformAdapter`
4. every Wan segment validates through `Wan27PlanningAdapter`
5. C/S/P roles map to pending Asset Bundle roles
6. `P01` is required in E02
7. H3/Seedance retain five authored shots
8. Wan creates five one-shot segments
9. continuation dependencies are provider-correct
10. no external provider method is called in tests or CI
