# PR14 Phase 0 — PR12/PR13 Integration Repair

## Purpose

Build the real-script Wan Canary on top of the hardened PR11 planning layer instead of the older merged PR11 state.

This branch starts from PR12 head `21246b5f04ebc688df6ee920da7d6c40b1fed608`, so it includes:

- corrected native-AV call accounting
- transactional nine-artifact publication
- capability-aware first-frame requirements
- stricter conditional 15-second rules
- explicit missing-reference warnings

## Pipeline

```text
Japanese MD script
  -> EpisodePackage
  -> PreparedEpisode
  -> GenerationPlanEpisode
  -> CandidateSelectionDecision
  -> one Wan-compatible single EditorialShot
  -> isolated PreparedEpisode
  -> PR8 approval / ledger / resume Canary
```

## Changes from the old PR13 path

- removes the production `--allow-experimental-multi-shot` escape hatch
- selects only `wan/i2v` segments containing one parent shot and one EditorialShot
- rejects unsupported audio strategies, invalid trim windows, duration overflow, and segment-scoped readiness errors
- moves candidate selection from inline CI Python into product code
- rebuilds MappingTrace instead of mutating old shot mappings heuristically
- reports the real credential-presence state even when budget blocks before submission
- always writes an enriched report when the delegated PR8 Canary fails
- emits one CNY execution-budget snapshot for the selected paid boundary

## Deliberate remaining blockers

This phase does not claim full-episode execution. The complete normal-script generation plan may still contain unresolved long non-dialogue intervals. Those errors remain in the generation artifacts.

Reference images, character masters, location masters, voice profiles, real-media quality validation, and full-episode assembly remain later phases.

## No-paid-call validation

CI must:

1. compile all Japanese-drama modules
2. run integrated PR8/PR11/PR12 focused tests
3. ingest a normal Japanese MD script
4. preserve the full plan and its blockers
5. auto-select one formal Wan single-shot candidate
6. run preflight without credentials
7. verify zero provider calls and no generated MP4
8. upload the full contract artifacts
