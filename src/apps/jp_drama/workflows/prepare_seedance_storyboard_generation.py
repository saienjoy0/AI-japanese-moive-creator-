"""Prepare Asset Bundles and H3/Wan/Seedance plans from an imported storyboard."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from ..assets.bundle import prepared_content_digest
from ..rendering.minimax_h3_adapter import build_h3_first_provider_registry
from ..rendering.minimax_h3_config import MiniMaxH3ProviderConfig
from ..rendering.provider_config import LiveProviderConfig
from ..rendering.provider_registry import (
    ProviderRegistry,
    SeedancePlatformAdapter,
    Wan27ImagePlanningAdapter,
    Wan27PlanningAdapter,
)
from ..seedance_storyboard import (
    SUPPORTED_ROUTES,
    SeedanceStoryboardBridgeError,
    SeedanceStoryboardPackage,
    build_storyboard_asset_bundle,
    build_storyboard_prepared_episode,
    compile_storyboard_generation_plan,
)


ROUTE_ORDER = ("h3", "wan", "seedance")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert an imported SeedanceStoryboardPackage into the existing "
            "PreparedEpisode, GenerationPlanEpisode, and pending Asset Bundle "
            "contracts without provider submissions."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--episode",
        action="append",
        dest="episodes",
        help="Episode ID such as E01. Repeat to select several; default is all.",
    )
    parser.add_argument(
        "--routes",
        nargs="+",
        choices=ROUTE_ORDER,
        default=list(ROUTE_ORDER),
    )
    parser.add_argument("--live-provider-config", type=Path)
    parser.add_argument("--minimax-h3-config", type=Path)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        package = SeedanceStoryboardPackage.model_validate_json(
            args.input.read_text(encoding="utf-8")
        )
        selected_episodes = _selected_episodes(package, args.episodes)
        selected_routes = _selected_routes(args.routes)
        registry = _build_registry(
            selected_routes,
            live_provider_config=args.live_provider_config,
            minimax_h3_config=args.minimax_h3_config,
        )
        summary = _write_bridge_artifacts(
            package,
            selected_episodes,
            selected_routes,
            registry,
            args.output_dir,
            fps=args.fps,
            overwrite=args.overwrite,
        )
    except (
        OSError,
        ValueError,
        ValidationError,
        SeedanceStoryboardBridgeError,
    ) as exc:
        print(f"storyboard generation bridge error: {exc}", file=sys.stderr)
        return 1

    if args.print_report:
        print(summary, end="")
    return 0


def _selected_episodes(
    package: SeedanceStoryboardPackage,
    requested: list[str] | None,
) -> list[str]:
    available = [item.episode_id for item in package.episodes]
    if not requested:
        return available
    if len(requested) != len(set(requested)):
        raise ValueError("--episode values must be unique")
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise ValueError(f"unknown episode IDs: {unknown}")
    return sorted(requested, key=lambda value: int(value[1:]))


def _selected_routes(requested: list[str]) -> list[str]:
    if len(requested) != len(set(requested)):
        raise ValueError("--routes values must be unique")
    return [item for item in ROUTE_ORDER if item in requested]


def _build_registry(
    routes: list[str],
    *,
    live_provider_config: Path | None,
    minimax_h3_config: Path | None,
) -> ProviderRegistry:
    needs_wan = "wan" in routes
    needs_h3 = "h3" in routes
    live_config = None
    if needs_wan or needs_h3:
        if live_provider_config is None:
            raise ValueError(
                "--live-provider-config is required for H3 or Wan planning"
            )
        live_config = LiveProviderConfig.load(live_provider_config)

    if needs_h3:
        if minimax_h3_config is None:
            raise ValueError("--minimax-h3-config is required for H3 planning")
        h3_config = MiniMaxH3ProviderConfig.load(minimax_h3_config)
        return build_h3_first_provider_registry(live_config, h3_config)

    registry = ProviderRegistry()
    if needs_wan:
        assert live_config is not None
        registry.register(Wan27ImagePlanningAdapter(live_config))
        registry.register(Wan27PlanningAdapter(live_config))
    if "seedance" in routes:
        registry.register(SeedancePlatformAdapter())
    return registry


def _write_bridge_artifacts(
    package: SeedanceStoryboardPackage,
    episode_ids: list[str],
    route_aliases: list[str],
    registry: ProviderRegistry,
    output_dir: Path,
    *,
    fps: int,
    overwrite: bool,
) -> str:
    if fps not in {24, 25, 30, 60}:
        raise ValueError("--fps must be one of 24, 25, 30, or 60")
    destination = output_dir.resolve()
    if destination.exists() and not overwrite:
        raise ValueError(f"output directory already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.stage-",
            dir=destination.parent,
        )
    )

    report: dict[str, object] = {
        "schema_version": "seedance-storyboard-generation-bridge/1.0",
        "project_title": package.project_title,
        "source_package_digest": package.content_digest,
        "episodes": {},
        "external_api_calls": 0,
    }
    summary_lines = [
        f"# {package.project_title} — Storyboard Production Bridge",
        "",
        f"- source package: `{package.content_digest}`",
        f"- episodes: {', '.join(episode_ids)}",
        f"- routes: {', '.join(route_aliases)}",
        "- provider calls: `0`",
        "",
    ]

    try:
        episode_report = report["episodes"]
        assert isinstance(episode_report, dict)
        for episode_id in episode_ids:
            episode_root = stage / episode_id
            episode_root.mkdir(parents=True, exist_ok=True)
            prepared = build_storyboard_prepared_episode(
                package,
                episode_id,
                fps=fps,
            )
            prepared_digest = prepared_content_digest(prepared)
            (episode_root / "prepared_episode.json").write_text(
                prepared.to_canonical_json() + "\n",
                encoding="utf-8",
            )
            route_report: dict[str, object] = {}
            for alias in route_aliases:
                route_id = SUPPORTED_ROUTES[alias]
                plan = compile_storyboard_generation_plan(
                    package,
                    prepared,
                    episode_id,
                    route_id=route_id,
                    registry=registry,
                )
                bundle = build_storyboard_asset_bundle(prepared, plan)
                route_root = episode_root / alias
                route_root.mkdir(parents=True, exist_ok=True)
                (route_root / "generation_plan_episode.json").write_text(
                    plan.to_canonical_json() + "\n",
                    encoding="utf-8",
                )
                (route_root / "asset_bundle_pending.json").write_text(
                    bundle.to_canonical_json() + "\n",
                    encoding="utf-8",
                )
                route_report[alias] = {
                    "route_id": route_id,
                    "generation_plan_digest": plan.content_digest,
                    "asset_bundle_digest": bundle.content_digest,
                    "segments": len(plan.segments),
                    "pending_assets": len(bundle.assets),
                    "planning_ready": plan.readiness_report.planning_ready,
                    "execution_route_ready": (
                        plan.readiness_report.execution_route_ready
                    ),
                    "readiness_error_codes": [
                        item.code for item in plan.readiness_report.errors
                    ],
                    "unknown_cost_components": (
                        plan.cost_plan.unknown_cost_components
                    ),
                }
                summary_lines.extend(
                    [
                        f"## {episode_id} / {alias}",
                        "",
                        f"- route: `{route_id}`",
                        f"- segments: `{len(plan.segments)}`",
                        f"- pending assets: `{len(bundle.assets)}`",
                        f"- planning ready: `{str(plan.readiness_report.planning_ready).lower()}`",
                        f"- route ready: `{str(plan.readiness_report.execution_route_ready).lower()}`",
                        "",
                    ]
                )
            episode_report[episode_id] = {
                "prepared_episode_digest": prepared_digest,
                "routes": route_report,
            }

        (stage / "bridge_report.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        summary = "\n".join(summary_lines).rstrip() + "\n"
        (stage / "summary.md").write_text(summary, encoding="utf-8")

        backup = destination.with_name(f".{destination.name}.backup")
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            destination.replace(backup)
        try:
            stage.replace(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return summary
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
