"""Atomic serialization for PR11 generation-plan artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import GenerationPlanEpisode


OUTPUT_FILENAMES = (
    "generation_plan_episode.json",
    "generation_segments.json",
    "editorial_shots.json",
    "continuity_contracts.json",
    "reference_asset_requirements.json",
    "generation_render_graph.json",
    "generation_cost_plan.json",
    "generation_readiness_report.json",
    "summary.txt",
)


def write_generation_artifacts(
    plan: GenerationPlanEpisode,
    output_dir: str | Path,
    *,
    overwrite: bool = False,
) -> dict[str, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {name: destination / name for name in OUTPUT_FILENAMES}
    if not overwrite and any(path.exists() for path in paths.values()):
        raise OSError("generation output already exists; pass --overwrite to replace it")

    editorial_shots = [
        shot.model_dump(mode="json", exclude_none=True)
        for segment in plan.segments
        for shot in segment.editorial_shots
    ]
    payloads = {
        "generation_plan_episode.json": plan.to_canonical_json(indent=2) + "\n",
        "generation_segments.json": _json(
            [item.model_dump(mode="json", exclude_none=True) for item in plan.segments]
        ),
        "editorial_shots.json": _json(editorial_shots),
        "continuity_contracts.json": _json(
            [item.model_dump(mode="json", exclude_none=True) for item in plan.continuity_contracts]
        ),
        "reference_asset_requirements.json": _json(
            [
                item.model_dump(mode="json", exclude_none=True)
                for item in plan.reference_asset_requirements
            ]
        ),
        "generation_render_graph.json": _json(
            plan.render_graph.model_dump(mode="json", exclude_none=True)
        ),
        "generation_cost_plan.json": _json(
            plan.cost_plan.model_dump(mode="json", exclude_none=True)
        ),
        "generation_readiness_report.json": _json(
            plan.readiness_report.model_dump(mode="json", exclude_none=True)
        ),
        "summary.txt": render_generation_summary(plan),
    }
    for name, content in payloads.items():
        _atomic_write(paths[name], content)
    return paths


def render_generation_summary(plan: GenerationPlanEpisode) -> str:
    report = plan.readiness_report
    durations = ", ".join(
        f"{item.editorial_duration_seconds}s->{item.requested_duration_seconds}s"
        for item in plan.segments
    )
    lines = [
        f"generation_plan_episode_id: {plan.generation_plan_episode_id}",
        f"provider_route_id: {plan.provider_route_id}",
        f"segments: {len(plan.segments)}",
        f"target_frames: {plan.target_frame_count}@{plan.timeline_fps}fps",
        f"segment_durations: {durations}",
        f"planning_ready: {str(report.planning_ready).lower()}",
        f"execution_route_ready: {str(report.execution_route_ready).lower()}",
        "media_quality_validated: false",
        f"expected_external_calls: {plan.cost_plan.expected_calls}",
        f"hard_maximum_calls: {plan.cost_plan.hard_maximum_calls}",
        f"errors: {len(report.errors)}",
        f"warnings: {len(report.warnings)}",
        f"content_digest: {plan.content_digest}",
    ]
    return "\n".join(lines) + "\n"


def _json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
