# PR9 Design: Wan and Seedance Multi-Provider Core

## Scope

PR9 adds provider-neutral planning contracts without changing the existing PR8
Wan 2.7 paid execution path.

Included routes:

- `mock/video`: deterministic zero-cost CI route
- `seedance/platform`: manual official-platform route
- `wan/i2v`: planning adapter backed by PR8 pricing and validation

## Invariants

- `PreparedEpisode` remains provider-neutral input.
- Provider and model selection are frozen in an immutable `ExecutionPlan`.
- Paid-provider fallback remains approval-gated.
- `seedance/platform` returns `awaiting_operator`; browser automation is not used.
- Wan live submission, polling, and download remain delegated to the proven PR8
  executor until the executor migration is completed.
- CI uses the mock route and performs zero paid provider calls.

## Audio strategies

- `native_av`
- `driving_audio`
- `external_audio_post`
- `silent`

A strategy is validated against route capabilities before an execution plan is
created.

## Provider capability model

The common contract records:

- text/image/reference-to-video support
- first/last-frame and continuation support
- native audio and driving audio support
- reference limits
- duration limits
- aspect ratios and resolutions
- automatic or manual execution mode

## Execution planning

The planner compiles external generation tasks from `PreparedEpisode` into a
stable plan containing:

- route ID
- request fingerprint
- provider-neutral generation specification
- cost estimate
- optional fallback route
- mandatory fallback approval flag

The plan digest excludes `created_at`, allowing deterministic comparison across
repeated planning runs.

## Follow-up

PR10 will export reproducible Seedance platform job packages and import the
resulting MP4 files. PR11 will add the Ark API after the platform workflow is
stable.
