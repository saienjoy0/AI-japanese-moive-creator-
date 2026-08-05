"""Validated public entry point for the zero-call preproduction builder.

The implementation remains in ``_builder_impl``.  This facade owns the
cross-contract loader so logical episode identity is checked by series ID and
episode number rather than by the PreparedEpisode's internal package ID.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ..assets.models import ApprovedAssetBundle
from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode
from ..rendering.ffmpeg import file_sha256
from ..series_plan import (
    SeriesPlanError,
    SeriesProductionManifest,
    load_series_inputs,
)
from . import _builder_impl as _impl


PreproductionPackageError = _impl.PreproductionPackageError


def _load_production(
    root: Path,
    source_series_plan: Path,
    source_asset_catalog: Path,
) -> _impl.LoadedProduction:
    try:
        manifest = SeriesProductionManifest.model_validate_json(
            (root / "series_manifest.json").read_text(encoding="utf-8")
        )
        source_plan, catalog = load_series_inputs(
            source_series_plan,
            source_asset_catalog,
        )
    except (OSError, ValidationError, SeriesPlanError) as exc:
        raise PreproductionPackageError(str(exc)) from exc

    if file_sha256(source_series_plan) != manifest.series_plan_sha256:
        raise PreproductionPackageError("source series plan SHA does not match manifest")
    if file_sha256(source_asset_catalog) != manifest.asset_catalog_sha256:
        raise PreproductionPackageError("source asset catalogue SHA does not match manifest")
    if source_plan.project_id != manifest.project_id or catalog.project_id != manifest.project_id:
        raise PreproductionPackageError("source project ID does not match imported manifest")
    if len(source_plan.episodes) != manifest.episode_count:
        raise PreproductionPackageError("source episode count does not match manifest")
    source_segment_count = sum(len(item.segments) for item in source_plan.episodes)
    if source_segment_count != manifest.segment_count:
        raise PreproductionPackageError("source segment count does not match manifest")

    episodes: list[_impl.LoadedEpisode] = []
    for artifact in manifest.episodes:
        prepared_path = _impl._safe_path(root, artifact.prepared_episode_file)
        prepared = PreparedEpisode.model_validate_json(
            prepared_path.read_text(encoding="utf-8")
        )
        if prepared.project_draft.episode_number != artifact.episode_number:
            raise PreproductionPackageError(
                "prepared episode number does not match manifest artifact"
            )
        if prepared.project_draft.series_id != manifest.series_id:
            raise PreproductionPackageError("prepared series ID does not match manifest")

        routes: dict[str, _impl.LoadedRoute] = {}
        if set(artifact.generation_plan_files) != set(_impl.ROUTES):
            raise PreproductionPackageError(
                f"{artifact.episode_id} must contain exactly {_impl.ROUTES} routes"
            )
        if set(artifact.asset_bundle_files) != set(_impl.ROUTES):
            raise PreproductionPackageError(
                f"{artifact.episode_id} must contain one AssetBundle per route"
            )

        for route in _impl.ROUTES:
            plan_relative = artifact.generation_plan_files[route]
            bundle_relative = artifact.asset_bundle_files[route]
            plan = GenerationPlanEpisode.model_validate_json(
                _impl._safe_path(root, plan_relative).read_text(encoding="utf-8")
            )
            bundle = ApprovedAssetBundle.model_validate_json(
                _impl._safe_path(root, bundle_relative).read_text(encoding="utf-8")
            )
            if plan.content_digest != artifact.generation_plan_digests[route]:
                raise PreproductionPackageError(
                    f"{artifact.episode_id}/{route} plan digest mismatch"
                )
            if bundle.content_digest != artifact.asset_bundle_digests[route]:
                raise PreproductionPackageError(
                    f"{artifact.episode_id}/{route} bundle digest mismatch"
                )
            if bundle.generation_plan_digest != plan.content_digest:
                raise PreproductionPackageError(
                    f"{artifact.episode_id}/{route} bundle belongs to another plan"
                )
            if plan.source_episode_id != prepared.episode_id:
                raise PreproductionPackageError(
                    f"{artifact.episode_id}/{route} plan belongs to another PreparedEpisode"
                )
            if bundle.source_episode_id != prepared.episode_id:
                raise PreproductionPackageError(
                    f"{artifact.episode_id}/{route} bundle belongs to another PreparedEpisode"
                )
            routes[route] = _impl.LoadedRoute(
                episode_id=artifact.episode_id,
                route=route,
                plan_relative=plan_relative,
                bundle_relative=bundle_relative,
                plan=plan,
                bundle=bundle,
            )
        episodes.append(
            _impl.LoadedEpisode(
                episode_id=artifact.episode_id,
                prepared_relative=artifact.prepared_episode_file,
                prepared=prepared,
                routes=routes,
            )
        )

    return _impl.LoadedProduction(
        root=root,
        manifest=manifest,
        source_plan=source_plan,
        catalog=catalog,
        episodes=episodes,
    )


# Bind the validated loader once.  The implementation's remaining helpers stay
# unchanged and continue to be covered by the real three-episode integration test.
_impl._load_production = _load_production


def build_preproduction_package(**kwargs):
    return _impl.build_preproduction_package(**kwargs)


__all__ = ["PreproductionPackageError", "build_preproduction_package"]
