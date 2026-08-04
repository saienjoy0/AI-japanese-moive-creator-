"""Deterministic, offline compiler from EpisodePackage to PreparedEpisode."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..domain import EpisodePackage, RenderStrategy
from .budget_gate import build_budget_snapshot
from .lumenx_adapter import build_lumenx_drafts
from .models import (
    ModelCatalog,
    ModelProfile,
    PreparedEpisode,
    ReadinessIssue,
    RenderGraph,
    RenderIntent,
    RenderTaskNode,
    StrategyCostQuote,
)
from .readiness import build_readiness_report
from .strategy_resolver import resolve_strategies


def load_model_catalog(path: str | Path | None = None) -> ModelCatalog:
    if path is None:
        return default_model_catalog()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ModelCatalog.model_validate(payload)


def default_model_catalog() -> ModelCatalog:
    return ModelCatalog(
        catalog_version="1.0.0",
        profiles=[
            ModelProfile(
                provider="provider-a",
                model="model-v1",
                supported_strategies=[
                    RenderStrategy.VIDEO_PLUS_TTS,
                    RenderStrategy.STILL_MOTION,
                ],
                capabilities=[
                    "video_generation",
                    "tts",
                    "image_generation",
                    "still_motion",
                ],
                fallback_costs=[
                    StrategyCostQuote(
                        strategy=RenderStrategy.VIDEO_PLUS_TTS,
                        estimated_primary_cost=400,
                        reserved_retry_cost=150,
                    ),
                    StrategyCostQuote(
                        strategy=RenderStrategy.STILL_MOTION,
                        estimated_primary_cost=250,
                        reserved_retry_cost=100,
                    ),
                ],
            ),
            ModelProfile(
                provider="provider-b",
                model="model-av",
                supported_strategies=[
                    RenderStrategy.NATIVE_AV,
                    RenderStrategy.VIDEO_PLUS_TTS,
                ],
                capabilities=[
                    "native_av",
                    "exact_dialogue",
                    "video_generation",
                    "tts",
                ],
                fallback_costs=[
                    StrategyCostQuote(
                        strategy=RenderStrategy.NATIVE_AV,
                        estimated_primary_cost=700,
                        reserved_retry_cost=250,
                    ),
                    StrategyCostQuote(
                        strategy=RenderStrategy.VIDEO_PLUS_TTS,
                        estimated_primary_cost=600,
                        reserved_retry_cost=200,
                    ),
                ],
            ),
        ],
    )


def compile_episode(
    package: EpisodePackage,
    *,
    catalog: ModelCatalog | None = None,
    strict: bool = False,
    source_payload: dict[str, Any] | None = None,
) -> PreparedEpisode:
    selected_catalog = catalog or default_model_catalog()
    source_digest = _source_digest(package, source_payload)

    intents, resolution_issues = resolve_strategies(package, selected_catalog)
    (
        project,
        characters,
        locations,
        props,
        frames,
        mapping,
    ) = build_lumenx_drafts(package, intents)

    graph, graph_issues = _build_render_graph(package.episode.episode_id, intents)
    budget, budget_issues = build_budget_snapshot(package.cost_plan, intents)
    report = build_readiness_report(
        package,
        intents,
        budget,
        mapping,
        [*resolution_issues, *graph_issues, *budget_issues],
        strict=strict,
    )

    return PreparedEpisode(
        source_digest=source_digest,
        package_id=package.package_id,
        episode_id=package.episode.episode_id,
        project_draft=project,
        character_seeds=characters,
        location_seeds=locations,
        prop_seeds=props,
        storyboard_frame_drafts=frames,
        render_intents=intents,
        render_graph=graph,
        budget_snapshot=budget,
        mapping_trace=mapping,
        readiness_report=report,
    )


def _source_digest(
    package: EpisodePackage,
    source_payload: dict[str, Any] | None,
) -> str:
    if source_payload is None:
        payload = package.model_dump(
            mode="json",
            exclude_none=True,
            exclude_computed_fields=True,
        )
        payload.pop("created_at", None)
        source = payload.get("source")
        if isinstance(source, dict):
            source.pop("captured_at", None)
    else:
        payload = source_payload

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _build_render_graph(
    episode_id: str,
    intents: list[RenderIntent],
) -> tuple[RenderGraph, list[ReadinessIssue]]:
    nodes: list[RenderTaskNode] = []
    issues: list[ReadinessIssue] = []
    for intent in intents:
        nodes.extend(_nodes_for_intent(episode_id, intent))
    try:
        return RenderGraph(nodes=nodes), issues
    except ValueError as exc:
        issues.append(
            ReadinessIssue(
                code="render_graph_invalid",
                severity="error",
                message=str(exc),
            )
        )
        return RenderGraph(nodes=[]), issues


def _nodes_for_intent(episode_id: str, intent: RenderIntent) -> list[RenderTaskNode]:
    prefix = f"{episode_id}_{intent.shot_id}"

    def task_id(task_type: str) -> str:
        return f"{prefix}_{task_type}"

    def node(
        task_type: str,
        depends_on: list[str],
        *,
        external: bool,
        provider: bool,
    ) -> RenderTaskNode:
        return RenderTaskNode(
            task_id=task_id(task_type),
            shot_id=intent.shot_id,
            task_type=task_type,
            depends_on=[task_id(dependency) for dependency in depends_on],
            external_api_required=external,
            provider_required=provider,
        )

    strategy = intent.resolved_strategy
    if strategy is RenderStrategy.NATIVE_AV:
        return [
            node("generate_native_av", [], external=True, provider=True),
            node("generate_subtitles", [], external=False, provider=False),
            node(
                "finalize_shot",
                ["generate_native_av", "generate_subtitles"],
                external=False,
                provider=False,
            ),
        ]
    if strategy is RenderStrategy.VIDEO_PLUS_TTS:
        return [
            node("generate_video", [], external=True, provider=True),
            node("generate_tts", [], external=True, provider=True),
            node("generate_subtitles", [], external=False, provider=False),
            node(
                "mux_audio_video",
                ["generate_video", "generate_tts", "generate_subtitles"],
                external=False,
                provider=False,
            ),
            node("finalize_shot", ["mux_audio_video"], external=False, provider=False),
        ]
    if strategy is RenderStrategy.SILENT_VIDEO:
        return [
            node("generate_video", [], external=True, provider=True),
            node("finalize_shot", ["generate_video"], external=False, provider=False),
        ]
    if strategy is RenderStrategy.STILL_MOTION:
        has_tts = "generate_tts" in intent.tasks
        base = [
            node("generate_image", [], external=True, provider=True),
            node("apply_still_motion", ["generate_image"], external=False, provider=False),
        ]
        if not has_tts:
            return [
                *base,
                node("finalize_shot", ["apply_still_motion"], external=False, provider=False),
            ]
        return [
            *base,
            node("generate_tts", [], external=True, provider=True),
            node("generate_subtitles", [], external=False, provider=False),
            node(
                "mux_audio_video",
                ["apply_still_motion", "generate_tts", "generate_subtitles"],
                external=False,
                provider=False,
            ),
            node("finalize_shot", ["mux_audio_video"], external=False, provider=False),
        ]
    raise ValueError(f"unsupported resolved strategy: {strategy.value}")
