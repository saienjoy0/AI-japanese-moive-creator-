"""Create machine-readable and human-readable preparation readiness reports."""

from __future__ import annotations

from ..domain import EpisodePackage
from .models import (
    BudgetSnapshot,
    MappingTrace,
    ReadinessIssue,
    ReadinessReport,
    RenderIntent,
)


def build_readiness_report(
    package: EpisodePackage,
    intents: list[RenderIntent],
    budget: BudgetSnapshot,
    mapping: MappingTrace,
    issues: list[ReadinessIssue],
    *,
    strict: bool,
) -> ReadinessReport:
    all_issues = list(issues)
    all_issues.extend(_content_warnings(package))

    if mapping.mapping_coverage < 1.0:
        all_issues.append(
            ReadinessIssue(
                code="mapping_incomplete",
                severity="error",
                message=f"mapping coverage is {mapping.mapping_coverage:.1%}",
            )
        )

    total_intents = len(package.shot_plan.shots)
    if len(intents) != total_intents:
        all_issues.append(
            ReadinessIssue(
                code="render_intents_unresolved",
                severity="error",
                message=f"resolved {len(intents)} of {total_intents} render intents",
            )
        )

    errors = sorted(
        (issue for issue in all_issues if issue.severity == "error"),
        key=_issue_key,
    )
    warnings = sorted(
        (issue for issue in all_issues if issue.severity == "warning"),
        key=_issue_key,
    )
    generation_ready = not errors and (not strict or not warnings)

    return ReadinessReport(
        package_id=package.package_id,
        episode_id=package.episode.episode_id,
        duration_seconds=package.shot_plan.total_duration_seconds,
        shot_count=len(package.shot_plan.shots),
        character_count=len(package.episode.character_ids),
        location_count=len(package.episode.locations),
        prop_count=len(package.episode.props),
        mapping_coverage=mapping.mapping_coverage,
        resolved_render_intents=len(intents),
        total_render_intents=total_intents,
        budget_limit=budget.budget_limit,
        estimated_total=budget.estimated_total,
        currency=budget.currency,
        external_api_calls=0,
        generation_ready=generation_ready,
        errors=errors,
        warnings=warnings,
    )


def render_summary(report: ReadinessReport) -> str:
    status = "YES" if report.generation_ready else "NO"
    lines = [
        f"Package: {report.package_id}",
        f"Episode: {report.episode_id}",
        f"Duration: {report.duration_seconds:.1f} sec",
        f"Shots: {report.shot_count}",
        f"Characters: {report.character_count}",
        f"Locations: {report.location_count}",
        f"Props: {report.prop_count}",
        "",
        f"Mapping coverage: {report.mapping_coverage:.0%}",
        (
            "Render strategies resolved: "
            f"{report.resolved_render_intents}/{report.total_render_intents}"
        ),
        (
            f"Budget: {report.currency.value} {report.estimated_total} / "
            f"{report.currency.value} {report.budget_limit}"
        ),
        "External API calls: 0",
        f"Generation ready: {status}",
    ]
    if report.errors:
        lines.extend(["", "Errors:"])
        lines.extend(f"- [{issue.code}] {issue.message}" for issue in report.errors)
    if report.warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- [{issue.code}] {issue.message}" for issue in report.warnings)
    return "\n".join(lines) + "\n"


def _content_warnings(package: EpisodePackage) -> list[ReadinessIssue]:
    warnings: list[ReadinessIssue] = []
    character_by_id = {
        character.character_id: character for character in package.adaptation.characters
    }
    for character_id in package.episode.character_ids:
        character = character_by_id[character_id]
        if not character.visual_notes:
            warnings.append(
                ReadinessIssue(
                    code="character_visual_continuity_missing",
                    severity="warning",
                    message=f"character {character_id} has no visual continuity notes",
                    field="adaptation.characters.visual_notes",
                )
            )

    estimates = {estimate.shot_id: estimate for estimate in package.cost_plan.shot_estimates}
    for shot in package.shot_plan.shots:
        if len(shot.visual_description) < 15:
            warnings.append(
                ReadinessIssue(
                    code="visual_prompt_too_short",
                    severity="warning",
                    message="visual description is too short for a stable draft",
                    shot_id=shot.shot_id,
                    field="shot_plan.shots.visual_description",
                )
            )
        if not shot.audio.ambience:
            warnings.append(
                ReadinessIssue(
                    code="ambience_missing",
                    severity="warning",
                    message="shot ambience is not specified",
                    shot_id=shot.shot_id,
                    field="shot_plan.shots.audio.ambience",
                )
            )
        if not shot.audio.bgm_cue:
            warnings.append(
                ReadinessIssue(
                    code="bgm_policy_missing",
                    severity="warning",
                    message="shot BGM direction is not specified",
                    shot_id=shot.shot_id,
                    field="shot_plan.shots.audio.bgm_cue",
                )
            )
        estimate = estimates[shot.shot_id]
        if estimate.fallback_strategy is None:
            warnings.append(
                ReadinessIssue(
                    code="fallback_missing",
                    severity="warning",
                    message="no explicit fallback strategy is declared",
                    shot_id=shot.shot_id,
                    field="cost_plan.shot_estimates.fallback_strategy",
                )
            )
        if estimate.max_attempts > 1 and estimate.reserved_retry_cost == 0:
            warnings.append(
                ReadinessIssue(
                    code="retry_budget_missing",
                    severity="warning",
                    message="retries are allowed but no retry budget is reserved",
                    shot_id=shot.shot_id,
                    field="cost_plan.shot_estimates.reserved_retry_cost",
                )
            )
        dialogue_seconds = sum(cue.end_seconds - cue.start_seconds for cue in shot.dialogue)
        if dialogue_seconds > shot.duration_seconds * 0.8:
            warnings.append(
                ReadinessIssue(
                    code="dialogue_density_high",
                    severity="warning",
                    message="dialogue timing occupies more than 80% of the shot",
                    shot_id=shot.shot_id,
                    field="shot_plan.shots.dialogue",
                )
            )

    for prop in package.episode.props:
        if not prop.visual_notes:
            warnings.append(
                ReadinessIssue(
                    code="prop_visual_description_missing",
                    severity="warning",
                    message=f"prop {prop.prop_id} has no visual notes",
                    field="episode.props.visual_notes",
                )
            )
    return warnings


def _issue_key(issue: ReadinessIssue) -> tuple[str, str, str]:
    return issue.code, issue.shot_id or "", issue.message
