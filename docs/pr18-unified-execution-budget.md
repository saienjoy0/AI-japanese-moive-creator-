# PR18 — Unified Provider Execution Budget

## Goal

Replace disconnected JPY draft estimates and ad hoc Canary arithmetic with one provider-bound CNY budget that covers the complete paid lifecycle.

```text
GenerationPlan + Provider price snapshot + ApprovedAssetBundle + Provider Ledgers
  -> ExecutionBudgetPlan
  -> committed exposure
  -> remaining exposure
  -> hard call and CNY gates
```

## Operations

Each selected segment receives explicit operations for:

- first-frame image
- video generation
- TTS when dialogue is present

The price snapshot comes only from the selected live provider configuration.

## Asset behavior

An approved first frame satisfies the image operation with zero remaining call and zero remaining cost. If the historical image submission exists in a Provider Ledger, the operation is recorded as committed instead.

## Ledger behavior

Submitted, unknown, running, succeeded, and failed provider records all consume one immutable submission. Their operation ID and estimated cost become committed exposure. The remaining budget never resubmits the same component.

Multiple committed operations for the same segment/component are rejected because they indicate duplicate paid submissions.

## TTS

TTS is no longer an unknown GenerationPlan component at the paid boundary. The execution budget calculates exact estimated CNY from the current provider price per 10,000 characters and the actual segment dialogue text length.

## Hard gates

Payment is approved only when:

- every component has known pricing
- committed plus remaining calls fit the hard call limit
- committed plus remaining cost and reserves fit the hard CNY limit

The draft EpisodePackage JPY budget is not used for provider submission authorization.

## Safety

- no provider call is made while calculating the budget
- no automatic retry reserve is added unless explicitly requested
- no automatic fallback provider is included
- ledgers remain immutable records of already consumed submissions
- CI uses fixture plans and synthetic ledger records only
