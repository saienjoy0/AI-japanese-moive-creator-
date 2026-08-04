"""Resolve declared render strategies without selecting providers or models implicitly."""

from __future__ import annotations

from decimal import Decimal

from ..domain import EpisodePackage, RenderStrategy, Shot, ShotCostEstimate
from .models import ModelCatalog, ReadinessIssue, RenderIntent


_REQUIRED_CAPABILITIES: dict[RenderStrategy, list[str]] = {
    RenderStrategy.NATIVE_AV: ["native_av", "exact_dialogue"],
    RenderStrategy.VIDEO_PLUS_TTS: ["video_generation", "tts"],
    RenderStrategy.SILENT_VIDEO: ["video_generation"],
    RenderStrategy.STILL_MOTION: ["image_generation", "still_motion"],
}

_TASKS: dict[RenderStrategy, list[str]] = {
    RenderStrategy.NATIVE_AV: [
        "generate_native_av",
        "generate_subtitles",
        "finalize_shot",
    ],
    RenderStrategy.VIDEO_PLUS_TTS: [
        "generate_video",
        "generate_tts",
        "generate_subtitles",
        "mux_audio_video",
        "finalize_shot",
    ],
    RenderStrategy.SILENT_VIDEO: ["generate_video", "finalize_shot"],
    RenderStrategy.STILL_MOTION: [
        "generate_image",
        "apply_still_motion",
        "finalize_shot",
    ],
}


def required_capabilities(shot: Shot, strategy: RenderStrategy) -> list[str]:
    capabilities = list(_REQUIRED_CAPABILITIES.get(strategy, []))
    if strategy is RenderStrategy.STILL_MOTION and shot.dialogue:
        capabilities.append("tts")
    return capabilities


def task_names(shot: Shot, strategy: RenderStrategy) -> list[str]:
    tasks = list(_TASKS[strategy])
    if strategy is RenderStrategy.STILL_MOTION and shot.dialogue:
        tasks = [
            "generate_image",
            "apply_still_motion",
            "generate_tts",
            "generate_subtitles",
            "mux_audio_video",
            "finalize_shot",
        ]
    return tasks


def resolve_strategies(
    package: EpisodePackage,
    catalog: ModelCatalog,
) -> tuple[list[RenderIntent], list[ReadinessIssue]]:
    estimates = {estimate.shot_id: estimate for estimate in package.cost_plan.shot_estimates}
    intents: list[RenderIntent] = []
    issues: list[ReadinessIssue] = []

    for shot in package.shot_plan.shots:
        estimate = estimates[shot.shot_id]
        intent, shot_issues = _resolve_shot(shot, estimate, catalog)
        issues.extend(shot_issues)
        if intent is not None:
            intents.append(intent)
    return intents, issues


def _resolve_shot(
    shot: Shot,
    estimate: ShotCostEstimate,
    catalog: ModelCatalog,
) -> tuple[RenderIntent | None, list[ReadinessIssue]]:
    issues: list[ReadinessIssue] = []
    requested = shot.render_strategy

    if requested is RenderStrategy.EXISTING_ASSET:
        return None, [
            _error(
                "unsupported_strategy",
                "existing_asset is not implemented by the PR4 preparation compiler",
                shot.shot_id,
            )
        ]

    if estimate.render_strategy is not requested:
        issues.append(
            _error(
                "cost_strategy_mismatch",
                "shot render strategy and cost estimate render strategy do not match",
                shot.shot_id,
            )
        )
        return None, issues

    if requested is RenderStrategy.SILENT_VIDEO and shot.dialogue:
        issues.append(
            _error(
                "silent_video_has_dialogue",
                "silent_video cannot be used for a shot containing spoken dialogue",
                shot.shot_id,
            )
        )
        return None, issues

    profile = catalog.get(estimate.provider, estimate.model)
    if profile is None:
        issues.append(
            _error(
                "model_not_found",
                f"model catalog has no exact entry for {estimate.provider}/{estimate.model}",
                shot.shot_id,
            )
        )
        return None, issues

    required = required_capabilities(shot, requested)
    if profile.supports(requested, required):
        return (
            _build_intent(
                shot=shot,
                estimate=estimate,
                resolved=requested,
                required=required,
                primary=estimate.estimated_primary_cost,
                retry=estimate.reserved_retry_cost,
                fallback_applied=False,
                reason=None,
            ),
            issues,
        )

    fallback = estimate.fallback_strategy
    if fallback is None:
        issues.append(
            _error(
                "model_capability_missing",
                f"{estimate.provider}/{estimate.model} lacks capabilities for {requested.value}",
                shot.shot_id,
            )
        )
        return None, issues

    if fallback is RenderStrategy.EXISTING_ASSET:
        issues.append(
            _error(
                "unsupported_fallback_strategy",
                "existing_asset cannot be used as a PR4 fallback",
                shot.shot_id,
            )
        )
        return None, issues

    if fallback is RenderStrategy.SILENT_VIDEO and shot.dialogue:
        issues.append(
            _error(
                "fallback_silent_video_has_dialogue",
                "declared silent_video fallback conflicts with spoken dialogue",
                shot.shot_id,
            )
        )
        return None, issues

    fallback_required = required_capabilities(shot, fallback)
    if not profile.supports(fallback, fallback_required):
        issues.append(
            _error(
                "fallback_capability_missing",
                f"declared fallback {fallback.value} is not supported by the same model",
                shot.shot_id,
            )
        )
        return None, issues

    quote = profile.fallback_cost(fallback)
    if quote is None:
        issues.append(
            _error(
                "fallback_cost_missing",
                f"catalog has no cost quote for declared fallback {fallback.value}",
                shot.shot_id,
            )
        )
        return None, issues

    reason = (
        f"requested {requested.value} was unsupported by {estimate.provider}/{estimate.model}; "
        f"applied explicitly declared fallback {fallback.value}"
    )
    return (
        _build_intent(
            shot=shot,
            estimate=estimate,
            resolved=fallback,
            required=fallback_required,
            primary=quote.estimated_primary_cost,
            retry=quote.reserved_retry_cost,
            fallback_applied=True,
            reason=reason,
        ),
        issues,
    )


def _build_intent(
    *,
    shot: Shot,
    estimate: ShotCostEstimate,
    resolved: RenderStrategy,
    required: list[str],
    primary: Decimal,
    retry: Decimal,
    fallback_applied: bool,
    reason: str | None,
) -> RenderIntent:
    return RenderIntent(
        intent_id=f"{shot.shot_id}_render",
        shot_id=shot.shot_id,
        requested_strategy=shot.render_strategy,
        resolved_strategy=resolved,
        provider=estimate.provider,
        model=estimate.model,
        model_capabilities_required=required,
        tasks=task_names(shot, resolved),
        fallback_strategy=estimate.fallback_strategy,
        fallback_applied=fallback_applied,
        resolution_reason=reason,
        estimated_primary_cost=primary,
        reserved_retry_cost=retry,
        estimated_total_cost=primary + retry,
    )


def _error(code: str, message: str, shot_id: str) -> ReadinessIssue:
    return ReadinessIssue(code=code, severity="error", message=message, shot_id=shot_id)
