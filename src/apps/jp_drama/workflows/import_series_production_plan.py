"""Import a multi-episode storyboard plan with zero provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from pydantic import ValidationError

from ..assets.bundle import prepared_content_digest
from ..rendering.minimax_h3_adapter import build_h3_first_provider_registry
from ..rendering.minimax_h3_config import MiniMaxH3ProviderConfig
from ..rendering.provider_config import LiveProviderConfig
from ..series_plan import (
    SUPPORTED_ROUTES,
    SeriesEpisodeArtifact,
    SeriesPlanError,
    SeriesProductionManifest,
    build_episode_asset_bundle,
    build_prepared_episode,
    canonical_digest,
    compile_episode_generation_plan,
    load_series_inputs,
)


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_ROUTE = 3
_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import a strict multi-episode generation plan plus asset catalogue, "
            "then write PreparedEpisode, H3/Wan/Seedance GenerationPlans, pending "
            "AssetBundles, and one series manifest without provider calls."
        )
    )
    parser.add_argument("--series-plan", required=True, type=Path)
    parser.add_argument("--asset-catalog", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--live-provider-config", required=True, type=Path)
    parser.add_argument("--minimax-h3-config", required=True, type=Path)
    parser.add_argument(
        "--source-repository",
        default="saienjoy0/Storyboard-Generator",
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument(
        "--episode",
        action="append",
        dest="episodes",
        help="Episode ID to import; repeat or omit for every episode",
    )
    parser.add_argument(
        "--routes",
        nargs="+",
        choices=tuple(SUPPORTED_ROUTES),
        default=list(SUPPORTED_ROUTES),
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--require-route-ready", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _sha256(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _atomic_write(path: Path, content: str) -> None:
    path = path.resolve()
    if path.exists():
        raise FileExistsError(f"staging output unexpectedly exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _json_content(model) -> str:
    if hasattr(model, "to_canonical_json"):
        content = model.to_canonical_json()
    else:
        content = json.dumps(
            model.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    return content if content.endswith("\n") else content + "\n"


def _relative_output_path(path: Path, output_root: Path) -> str:
    return path.resolve().relative_to(output_root.resolve()).as_posix()


def _portable_source_path(path: Path) -> str:
    parts = path.parts
    if "projects" in parts:
        index = parts.index("projects")
        return Path(*parts[index:]).as_posix()
    return path.name


def _publish_directory(
    staging_dir: Path,
    output_dir: Path,
    *,
    overwrite: bool,
) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    backup = output_dir.with_name(f".{output_dir.name}.backup-{os.getpid()}")
    if backup.exists():
        shutil.rmtree(backup)
    had_previous = output_dir.exists()
    if had_previous:
        if not output_dir.is_dir():
            raise FileExistsError(f"output path is not a directory: {output_dir}")
        if any(output_dir.iterdir()) and not overwrite:
            raise FileExistsError(
                f"output directory is not empty: {output_dir}; use --overwrite"
            )
        os.replace(output_dir, backup)
    try:
        os.replace(staging_dir, output_dir)
    except Exception:
        if had_previous and backup.exists() and not output_dir.exists():
            os.replace(backup, output_dir)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not _COMMIT_PATTERN.fullmatch(args.source_commit):
        print("input error: --source-commit must be a full 40-character SHA", file=sys.stderr)
        return EXIT_INPUT
    try:
        series_plan_path = args.series_plan.resolve()
        catalog_path = args.asset_catalog.resolve()
        source_plan, catalog = load_series_inputs(series_plan_path, catalog_path)
        live_config = LiveProviderConfig.load(args.live_provider_config)
        h3_config = MiniMaxH3ProviderConfig.load(args.minimax_h3_config)
        registry = build_h3_first_provider_registry(live_config, h3_config)
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        SeriesPlanError,
    ) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    requested_episodes = args.episodes or [item.episode_id for item in source_plan.episodes]
    if len(requested_episodes) != len(set(requested_episodes)):
        print("input error: --episode values must be unique", file=sys.stderr)
        return EXIT_INPUT
    known_episodes = {item.episode_id for item in source_plan.episodes}
    unknown = sorted(set(requested_episodes) - known_episodes)
    if unknown:
        print(f"input error: unknown episodes: {unknown}", file=sys.stderr)
        return EXIT_INPUT

    final_output_dir = args.output_dir.resolve()
    if final_output_dir.exists():
        if not final_output_dir.is_dir():
            print(
                f"input error: output path is not a directory: {final_output_dir}",
                file=sys.stderr,
            )
            return EXIT_INPUT
        if any(final_output_dir.iterdir()) and not args.overwrite:
            print(
                f"input error: output directory is not empty: {final_output_dir}; "
                "use --overwrite",
                file=sys.stderr,
            )
            return EXIT_INPUT

    staging_dir = final_output_dir.with_name(
        f".{final_output_dir.name}.staging-{os.getpid()}"
    )
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=False)

    episode_artifacts: list[SeriesEpisodeArtifact] = []
    route_blockers: list[str] = []
    try:
        for episode_id in requested_episodes:
            prepared = build_prepared_episode(source_plan, catalog, episode_id)
            prepared_path = staging_dir / episode_id / "prepared_episode.json"
            _atomic_write(prepared_path, _json_content(prepared))
            plan_files: dict[str, str] = {}
            plan_digests: dict[str, str] = {}
            bundle_files: dict[str, str] = {}
            bundle_digests: dict[str, str] = {}

            for route_alias in args.routes:
                route_id = SUPPORTED_ROUTES[route_alias]
                generation_plan = compile_episode_generation_plan(
                    source_plan,
                    catalog,
                    prepared,
                    episode_id,
                    route_id=route_id,
                    registry=registry,
                )
                bundle = build_episode_asset_bundle(prepared, generation_plan)
                route_dir = staging_dir / episode_id / route_alias
                plan_path = route_dir / "generation_plan_episode.json"
                bundle_path = route_dir / "asset_bundle_pending.json"
                _atomic_write(plan_path, _json_content(generation_plan))
                _atomic_write(bundle_path, _json_content(bundle))
                plan_files[route_alias] = _relative_output_path(plan_path, staging_dir)
                plan_digests[route_alias] = generation_plan.content_digest
                bundle_files[route_alias] = _relative_output_path(
                    bundle_path,
                    staging_dir,
                )
                bundle_digests[route_alias] = bundle.content_digest
                if not generation_plan.readiness_report.execution_route_ready:
                    route_blockers.append(
                        f"{episode_id}/{route_alias}:"
                        + ",".join(
                            item.code for item in generation_plan.readiness_report.errors
                        )
                    )

            episode_artifacts.append(
                SeriesEpisodeArtifact(
                    episode_id=episode_id,
                    episode_number=int(episode_id[1:]),
                    prepared_episode_file=_relative_output_path(
                        prepared_path,
                        staging_dir,
                    ),
                    prepared_episode_digest=prepared_content_digest(prepared),
                    generation_plan_files=plan_files,
                    generation_plan_digests=plan_digests,
                    asset_bundle_files=bundle_files,
                    asset_bundle_digests=bundle_digests,
                )
            )

        segment_count = sum(
            len(item.segments)
            for item in source_plan.episodes
            if item.episode_id in requested_episodes
        )
        series_digest = canonical_digest(
            source_plan.model_dump(mode="json", exclude_none=True),
            catalog.model_dump(mode="json", exclude_none=True),
        )
        manifest = SeriesProductionManifest.build_with_digest(
            project_id=source_plan.project_id,
            series_id=source_plan.project_id,
            title=source_plan.title,
            source_title=source_plan.source.title,
            source_author=source_plan.source.author,
            rights_status="public_domain",
            source_repository=args.source_repository,
            source_commit=args.source_commit,
            series_plan_file=_portable_source_path(args.series_plan),
            series_plan_sha256=_sha256(series_plan_path),
            asset_catalog_file=_portable_source_path(args.asset_catalog),
            asset_catalog_sha256=_sha256(catalog_path),
            series_content_digest=series_digest,
            episode_count=len(requested_episodes),
            segment_count=segment_count,
            timeline_fps=source_plan.production.timeline_fps,
            episode_frame_count=source_plan.production.episode_frame_count,
            total_frame_count=(
                len(requested_episodes) * source_plan.production.episode_frame_count
            ),
            episodes=episode_artifacts,
            acceptance_criteria=source_plan.acceptance_criteria,
            manual_review_required=source_plan.manual_review_required,
            external_api_calls=0,
        )
        manifest_path = staging_dir / "series_manifest.json"
        _atomic_write(manifest_path, manifest.to_canonical_json())
        summary = {
            "valid": not route_blockers,
            "series_manifest": "series_manifest.json",
            "series_manifest_digest": manifest.content_digest,
            "series_content_digest": manifest.series_content_digest,
            "episode_count": manifest.episode_count,
            "segment_count": manifest.segment_count,
            "routes": args.routes,
            "route_blockers": route_blockers,
            "external_api_calls": 0,
        }
        _atomic_write(
            staging_dir / "import_report.json",
            json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        if args.require_route_ready and route_blockers:
            shutil.rmtree(staging_dir)
            print(
                "route error: one or more provider routes are not ready: "
                + "; ".join(route_blockers),
                file=sys.stderr,
            )
            return EXIT_ROUTE
        _publish_directory(
            staging_dir,
            final_output_dir,
            overwrite=args.overwrite,
        )
    except (
        OSError,
        ValidationError,
        ValueError,
        FileExistsError,
        SeriesPlanError,
    ) as exc:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        print(f"series import error: {exc}", file=sys.stderr)
        return EXIT_NOT_READY

    final_manifest_path = final_output_dir / "series_manifest.json"
    if args.print_report:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(
            f"Series: {source_plan.title}\n"
            f"Episodes: {manifest.episode_count}\n"
            f"Segments: {manifest.segment_count}\n"
            f"Routes: {', '.join(args.routes)}\n"
            f"Manifest: {final_manifest_path}\n"
            "External API calls: 0"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
