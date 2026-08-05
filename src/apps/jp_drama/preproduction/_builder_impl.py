"""Build a portable, auditable preproduction package without media generation."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..assets.models import ApprovedAssetBundle
from ..generation import select_safe_canary_candidate
from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode
from ..rendering.ffmpeg import file_sha256
from ..rendering.provider_config import LiveProviderConfig
from ..series_plan import (
    SeriesAssetCatalog,
    SeriesGenerationPlan,
    SeriesPlanError,
    SeriesProductionManifest,
    load_series_inputs,
)
from .models import (
    AssetCreationRequirement,
    BundleAssetBinding,
    CanaryEpisodeDecision,
    CanaryRecommendation,
    FirstFrameRequirement,
    PreproductionBlocker,
    PreproductionPackageManifest,
    ProviderRouteSummary,
    RouteCostSummary,
    VoiceCreationRequirement,
)


ROUTES = ("h3", "wan", "seedance")
ROLE_BY_KIND = {
    "character": "character_master",
    "scene": "location_master",
    "prop": "prop_master",
}


class PreproductionPackageError(RuntimeError):
    """The imported production contract cannot form a safe preproduction package."""


@dataclass(frozen=True)
class LoadedRoute:
    episode_id: str
    route: str
    plan_relative: str
    bundle_relative: str
    plan: GenerationPlanEpisode
    bundle: ApprovedAssetBundle


@dataclass(frozen=True)
class LoadedEpisode:
    episode_id: str
    prepared_relative: str
    prepared: PreparedEpisode
    routes: dict[str, LoadedRoute]


@dataclass(frozen=True)
class LoadedProduction:
    root: Path
    manifest: SeriesProductionManifest
    source_plan: SeriesGenerationPlan
    catalog: SeriesAssetCatalog
    episodes: list[LoadedEpisode]


def build_preproduction_package(
    *,
    series_output: str | Path,
    source_series_plan: str | Path,
    source_asset_catalog: str | Path,
    live_provider_config: str | Path,
    output_dir: str | Path,
    overwrite: bool = False,
) -> PreproductionPackageManifest:
    source_root = Path(series_output).resolve()
    destination = Path(output_dir).resolve()
    loaded = _load_production(
        source_root,
        Path(source_series_plan).resolve(),
        Path(source_asset_catalog).resolve(),
    )
    provider = LiveProviderConfig.load(live_provider_config)

    if destination.exists() and not overwrite:
        raise PreproductionPackageError(
            "output directory already exists; use overwrite to replace it"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=destination.parent,
        )
    )
    backup: Path | None = None
    try:
        _copy_contract_snapshot(loaded.root, staging / "production_contract")
        source_snapshot = staging / "source_contract"
        source_snapshot.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_series_plan, source_snapshot / "series_plan.yaml")
        shutil.copy2(source_asset_catalog, source_snapshot / "asset_catalog.yaml")

        route_summaries = _build_route_summaries(loaded)
        assets = _build_asset_requirements(loaded)
        voices = _build_voice_requirements(loaded)
        approval_commands = _write_approval_templates(staging, loaded, assets, voices)
        first_frames = _build_first_frame_plan(staging, loaded)
        canary = _build_canary_recommendation(loaded, provider)
        blockers = _build_blockers(assets, voices, first_frames)

        _write_json(staging / "asset_creation_checklist.json", [
            item.model_dump(mode="json", exclude_none=True) for item in assets
        ])
        _write_asset_markdown(staging / "asset_creation_checklist.md", assets)
        _write_json(staging / "voice_identity_checklist.json", [
            item.model_dump(mode="json", exclude_none=True) for item in voices
        ])
        _write_json(staging / "first_frame_plan.json", [
            item.model_dump(mode="json", exclude_none=True) for item in first_frames
        ])
        _write_json(staging / "provider_route_summary.json", [
            item.model_dump(mode="json", exclude_none=True) for item in route_summaries
        ])
        _write_json(
            staging / "canary_recommendation.json",
            canary.model_dump(mode="json", exclude_none=True),
        )
        _write_json(staging / "bundle_approval_commands.json", approval_commands)

        files = {
            "readme": "README.md",
            "asset_creation_checklist": "asset_creation_checklist.json",
            "asset_creation_checklist_markdown": "asset_creation_checklist.md",
            "voice_identity_checklist": "voice_identity_checklist.json",
            "first_frame_plan": "first_frame_plan.json",
            "provider_route_summary": "provider_route_summary.json",
            "canary_recommendation": "canary_recommendation.json",
            "bundle_approval_commands": "bundle_approval_commands.json",
            "approval_templates_directory": "approval_templates",
            "approved_bundles_directory": "approved_bundles",
            "wan_first_frames_directory": "wan_first_frames",
            "source_contract_directory": "source_contract",
            "production_contract_directory": "production_contract",
        }
        manifest = PreproductionPackageManifest.build_with_digest(
            package_id=f"preproduction_{loaded.manifest.series_id}",
            source_series_manifest_file="production_contract/series_manifest.json",
            source_series_manifest_digest=file_sha256(
                staging / "production_contract" / "series_manifest.json"
            ),
            source_series_content_digest=loaded.manifest.content_digest,
            source_repository=loaded.manifest.source_repository,
            source_commit=loaded.manifest.source_commit,
            title=loaded.manifest.title,
            episode_count=loaded.manifest.episode_count,
            segment_count=loaded.manifest.segment_count,
            base_master_asset_count=len(assets),
            variant_review_asset_count=sum(item.variant_review_required for item in assets),
            voice_identity_count=len(voices),
            first_frame_count=len(first_frames),
            provider_route_count=len(route_summaries),
            provider_plans_ready=all(
                item.planning_ready and item.execution_route_ready
                for item in route_summaries
            ),
            files=files,
            blockers=blockers,
            external_api_calls=0,
        )
        (staging / "preproduction_manifest.json").write_text(
            manifest.to_canonical_json(),
            encoding="utf-8",
        )
        _write_readme(
            staging / "README.md",
            manifest,
            assets,
            voices,
            first_frames,
            route_summaries,
            canary,
        )
        _verify_no_generated_media(staging)

        if destination.exists():
            backup = destination.with_name(f".{destination.name}.backup-{os.getpid()}")
            if backup.exists():
                shutil.rmtree(backup)
            destination.replace(backup)
        staging.replace(destination)
        if backup is not None and backup.exists():
            shutil.rmtree(backup)
        return manifest
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if backup is not None and backup.exists() and not destination.exists():
            backup.replace(destination)
        raise


def _load_production(
    root: Path,
    source_series_plan: Path,
    source_asset_catalog: Path,
) -> LoadedProduction:
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

    episodes: list[LoadedEpisode] = []
    for artifact in manifest.episodes:
        prepared_path = _safe_path(root, artifact.prepared_episode_file)
        prepared = PreparedEpisode.model_validate_json(
            prepared_path.read_text(encoding="utf-8")
        )
        if prepared.episode_id != artifact.episode_id:
            raise PreproductionPackageError("prepared episode ID does not match manifest")
        routes: dict[str, LoadedRoute] = {}
        if set(artifact.generation_plan_files) != set(ROUTES):
            raise PreproductionPackageError(
                f"{artifact.episode_id} must contain exactly {ROUTES} routes"
            )
        for route in ROUTES:
            plan_relative = artifact.generation_plan_files[route]
            bundle_relative = artifact.asset_bundle_files[route]
            plan = GenerationPlanEpisode.model_validate_json(
                _safe_path(root, plan_relative).read_text(encoding="utf-8")
            )
            bundle = ApprovedAssetBundle.model_validate_json(
                _safe_path(root, bundle_relative).read_text(encoding="utf-8")
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
            routes[route] = LoadedRoute(
                episode_id=artifact.episode_id,
                route=route,
                plan_relative=plan_relative,
                bundle_relative=bundle_relative,
                plan=plan,
                bundle=bundle,
            )
        episodes.append(
            LoadedEpisode(
                episode_id=artifact.episode_id,
                prepared_relative=artifact.prepared_episode_file,
                prepared=prepared,
                routes=routes,
            )
        )
    return LoadedProduction(
        root=root,
        manifest=manifest,
        source_plan=source_plan,
        catalog=catalog,
        episodes=episodes,
    )


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if root != path and root not in path.parents:
        raise PreproductionPackageError(f"contract path escapes source root: {relative}")
    if not path.is_file():
        raise PreproductionPackageError(f"contract file is missing: {relative}")
    return path


def _copy_contract_snapshot(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        if item.is_dir():
            continue
        relative = item.relative_to(source)
        if item.suffix.lower() in {".mp4", ".mov", ".png", ".jpg", ".jpeg", ".wav"}:
            raise PreproductionPackageError(
                f"generated media is not allowed in source contract: {relative}"
            )
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _production_path(relative: str) -> str:
    return str(Path("production_contract") / relative).replace("\\", "/")


def _build_route_summaries(loaded: LoadedProduction) -> list[ProviderRouteSummary]:
    result: list[ProviderRouteSummary] = []
    for episode in loaded.episodes:
        for route in ROUTES:
            item = episode.routes[route]
            plan = item.plan
            result.append(
                ProviderRouteSummary(
                    episode_id=episode.episode_id,
                    route=route,
                    provider_route_id=plan.provider_route_id,
                    generation_plan_file=_production_path(item.plan_relative),
                    generation_plan_digest=plan.content_digest,
                    pending_asset_bundle_file=_production_path(item.bundle_relative),
                    pending_asset_bundle_digest=item.bundle.content_digest,
                    segment_ids=[segment.segment_id for segment in plan.segments],
                    planning_ready=plan.readiness_report.planning_ready,
                    execution_route_ready=plan.readiness_report.execution_route_ready,
                    warnings=[
                        f"{issue.code}: {issue.message}"
                        for issue in plan.readiness_report.warnings
                    ],
                    errors=[
                        f"{issue.code}: {issue.message}"
                        for issue in plan.readiness_report.errors
                    ],
                    cost=RouteCostSummary(
                        reference_image_calls=plan.cost_plan.reference_image_calls,
                        video_calls=plan.cost_plan.video_calls,
                        tts_calls=plan.cost_plan.tts_calls,
                        native_audio_calls=plan.cost_plan.native_audio_calls,
                        expected_calls=plan.cost_plan.expected_calls,
                        hard_maximum_calls=plan.cost_plan.hard_maximum_calls,
                        totals_by_currency=plan.cost_plan.totals_by_currency,
                        unknown_cost_components=plan.cost_plan.unknown_cost_components,
                        pricing_snapshot_dates=plan.cost_plan.pricing_snapshot_dates,
                    ),
                )
            )
    return result


def _source_usage(plan: SeriesGenerationPlan) -> dict[str, list[str]]:
    usage: dict[str, set[str]] = defaultdict(set)
    for episode in plan.episodes:
        for segment in episode.segments:
            references = {
                *segment.location_ids,
                *segment.character_ids,
                *segment.background_character_ids,
                *segment.prop_ids,
                *(item.speaker for item in segment.dialogue),
            }
            for asset_id in references:
                usage[asset_id].add(segment.segment_id)
    return {key: sorted(values) for key, values in usage.items()}


def _build_asset_requirements(
    loaded: LoadedProduction,
) -> list[AssetCreationRequirement]:
    usage = _source_usage(loaded.source_plan)
    requirements: list[AssetCreationRequirement] = []
    for catalog_asset in sorted(loaded.catalog.assets, key=lambda item: item.asset_id):
        bindings: list[BundleAssetBinding] = []
        role = ROLE_BY_KIND[catalog_asset.kind]
        for episode in loaded.episodes:
            for route in ROUTES:
                loaded_route = episode.routes[route]
                for asset in loaded_route.bundle.assets:
                    if asset.role == role and asset.subject_id == catalog_asset.asset_id:
                        bindings.append(
                            BundleAssetBinding(
                                episode_id=episode.episode_id,
                                route=route,
                                bundle_file=_production_path(
                                    loaded_route.bundle_relative
                                ),
                                asset_id=asset.asset_id,
                            )
                        )
        if not bindings:
            raise PreproductionPackageError(
                f"catalogue asset {catalog_asset.asset_id} has no bundle bindings"
            )
        requirements.append(
            AssetCreationRequirement(
                source_asset_id=catalog_asset.asset_id,
                kind=catalog_asset.kind,
                approval_role=role,
                name=catalog_asset.name,
                description=catalog_asset.description,
                story_function=catalog_asset.story_function,
                prompt=catalog_asset.prompt,
                negative_prompt=catalog_asset.negative_prompt,
                used_episode_ids=sorted(catalog_asset.used_in_episode_ids),
                used_segment_ids=usage[catalog_asset.asset_id],
                instance_rules=catalog_asset.instance_rules,
                voice_identity_required=bool(
                    catalog_asset.voice_identity_required
                ),
                bundle_bindings=sorted(
                    bindings,
                    key=lambda item: (item.episode_id, item.route, item.asset_id),
                ),
                variant_review_required=bool(catalog_asset.instance_rules),
            )
        )
    return requirements


def _build_voice_requirements(
    loaded: LoadedProduction,
) -> list[VoiceCreationRequirement]:
    usage = _source_usage(loaded.source_plan)
    results: list[VoiceCreationRequirement] = []
    for catalog_asset in sorted(loaded.catalog.assets, key=lambda item: item.asset_id):
        if catalog_asset.kind != "character" or not catalog_asset.voice_identity_required:
            continue
        profiles = []
        bundle_files = []
        for episode in loaded.episodes:
            for route in ROUTES:
                loaded_route = episode.routes[route]
                matches = [
                    profile
                    for profile in loaded_route.bundle.voice_profiles
                    if profile.source_character_id == catalog_asset.asset_id
                ]
                profiles.extend(matches)
                if matches:
                    bundle_files.append(_production_path(loaded_route.bundle_relative))
        if not profiles:
            raise PreproductionPackageError(
                f"voice-required character {catalog_asset.asset_id} has no profiles"
            )
        providers = {item.provider for item in profiles}
        if len(providers) != 1:
            raise PreproductionPackageError(
                f"voice provider differs across bundles for {catalog_asset.asset_id}"
            )
        results.append(
            VoiceCreationRequirement(
                source_character_id=catalog_asset.asset_id,
                name=catalog_asset.name,
                provider=next(iter(providers)),
                used_episode_ids=sorted(catalog_asset.used_in_episode_ids),
                used_segment_ids=usage[catalog_asset.asset_id],
                bundle_files=sorted(set(bundle_files)),
                profile_ids=sorted({item.profile_id for item in profiles}),
            )
        )
    return results


def _write_approval_templates(
    root: Path,
    loaded: LoadedProduction,
    assets: list[AssetCreationRequirement],
    voices: list[VoiceCreationRequirement],
) -> list[dict[str, object]]:
    catalogue_by_id = {item.source_asset_id: item for item in assets}
    voice_by_id = {item.source_character_id: item for item in voices}
    commands: list[dict[str, object]] = []
    for episode in loaded.episodes:
        for route in ROUTES:
            loaded_route = episode.routes[route]
            template_relative = Path("approval_templates") / episode.episode_id / route / "bindings.template.json"
            output_relative = Path("approved_bundles") / episode.episode_id / route / "asset_bundle_masters_approved.json"
            asset_payload: dict[str, object] = {}
            for asset in loaded_route.bundle.assets:
                if asset.role not in {"character_master", "location_master", "prop_master"}:
                    continue
                requirement = catalogue_by_id[asset.subject_id]
                asset_payload[asset.asset_id] = {
                    "path": "",
                    "generated_by": "manual-or-approved-image-workflow",
                    "operation_id": f"pending:{episode.episode_id}:{route}:{asset.asset_id}",
                    "verified_against_asset_ids": [],
                    "source_asset_id": requirement.source_asset_id,
                    "name": requirement.name,
                    "prompt": requirement.prompt,
                    "negative_prompt": requirement.negative_prompt,
                    "instance_rules": requirement.instance_rules,
                    "episode_id": episode.episode_id,
                }
            voice_payload: dict[str, object] = {}
            for profile in loaded_route.bundle.voice_profiles:
                requirement = voice_by_id.get(profile.source_character_id)
                if requirement is None:
                    continue
                voice_payload[profile.source_character_id] = {
                    "provider": requirement.provider,
                    "voice_id": "",
                    "language": "ja-JP",
                    "speaking_rate": 1.0,
                    "pronunciation_dictionary": {},
                    "allow_shared_voice": False,
                    "name": requirement.name,
                }
            payload = {
                "approved_by": "",
                "assets": asset_payload,
                "voices": voice_payload,
                "_instructions": (
                    "Fill approved_by, every required path, and distinct voice_id values. "
                    "Remove no keys; extra descriptive fields are ignored by the approval workflow."
                ),
            }
            _write_json(root / template_relative, payload)
            command = (
                "python -m src.apps.jp_drama.workflows.approve_asset_bundle "
                f"--input \"${{PREPRODUCTION_ROOT}}/{_production_path(loaded_route.bundle_relative)}\" "
                f"--bindings \"${{PREPRODUCTION_ROOT}}/{template_relative.as_posix()}\" "
                f"--output \"${{PREPRODUCTION_ROOT}}/{output_relative.as_posix()}\" "
                "--print-summary"
            )
            commands.append(
                {
                    "episode_id": episode.episode_id,
                    "route": route,
                    "input_bundle": _production_path(loaded_route.bundle_relative),
                    "bindings_template": template_relative.as_posix(),
                    "output_bundle": output_relative.as_posix(),
                    "command": command,
                    "status": "blocked_until_paths_and_voices_are_filled",
                }
            )
    return commands


def _build_first_frame_plan(
    root: Path,
    loaded: LoadedProduction,
) -> list[FirstFrameRequirement]:
    results: list[FirstFrameRequirement] = []
    for episode in loaded.episodes:
        route = episode.routes["wan"]
        base_bundle = Path("approved_bundles") / episode.episode_id / "wan" / "asset_bundle_masters_approved.json"
        current_bundle = base_bundle
        for segment in route.plan.segments:
            segment_dir = Path("wan_first_frames") / episode.episode_id / segment.segment_id
            reference_manifest = segment_dir / "master_references.json"
            preflight_report = segment_dir / "keyframe_preflight.json"
            keyframe = segment_dir / "first_frame.png"
            approval = segment_dir / "first_frame.approval.json"
            registered = Path("approved_bundles") / episode.episode_id / "wan" / f"asset_bundle_through_{segment.segment_id}.json"
            prepared_file = _production_path(episode.prepared_relative)
            plan_file = _production_path(route.plan_relative)
            pending_bundle = _production_path(route.bundle_relative)
            master_ids = [
                item
                for item in segment.reference_asset_ids
                if item.startswith(("ref_char_", "ref_loc_", "ref_prop_"))
            ]
            common = (
                f"--prepared-input \"${{PREPRODUCTION_ROOT}}/{prepared_file}\" "
                f"--generation-plan \"${{PREPRODUCTION_ROOT}}/{plan_file}\" "
                f"--asset-bundle \"${{PREPRODUCTION_ROOT}}/{current_bundle.as_posix()}\" "
                f"--segment-id {segment.segment_id}"
            )
            prepare_command = (
                "python -m src.apps.jp_drama.workflows.prepare_wan_master_keyframe "
                f"{common} --manifest-output \"${{PREPRODUCTION_ROOT}}/{reference_manifest.as_posix()}\" "
                f"--report \"${{PREPRODUCTION_ROOT}}/{segment_dir.as_posix()}/master_preflight.json\""
            )
            keyframe_base = (
                "python -m src.apps.jp_drama.workflows.render_wan_master_keyframe "
                f"{common} "
                f"--master-reference-manifest \"${{PREPRODUCTION_ROOT}}/{reference_manifest.as_posix()}\" "
                "--providers \"${REPO_ROOT}/examples/jp_drama/dashscope_live_providers.json\" "
                f"--keyframe-output \"${{PREPRODUCTION_ROOT}}/{keyframe.as_posix()}\" "
                f"--approval-manifest \"${{PREPRODUCTION_ROOT}}/{approval.as_posix()}\""
            )
            preflight_command = (
                f"{keyframe_base} --stage preflight "
                f"--report \"${{PREPRODUCTION_ROOT}}/{preflight_report.as_posix()}\""
            )
            paid_command = (
                f"{keyframe_base} --stage keyframe --execute-paid "
                "--approval-digest <COPY_APPROVAL_DIGEST_FROM_PREFLIGHT> "
                f"--report \"${{PREPRODUCTION_ROOT}}/{segment_dir.as_posix()}/keyframe_result.json\""
            )
            approve_command = (
                f"{keyframe_base} --stage approve "
                f"--report \"${{PREPRODUCTION_ROOT}}/{segment_dir.as_posix()}/approval_result.json\""
            )
            register_command = (
                "python -m src.apps.jp_drama.workflows.register_wan_master_keyframe "
                f"{common} "
                f"--master-reference-manifest \"${{PREPRODUCTION_ROOT}}/{reference_manifest.as_posix()}\" "
                f"--approval-manifest \"${{PREPRODUCTION_ROOT}}/{approval.as_posix()}\" "
                "--approved-by <REVIEWER_NAME> "
                f"--output-bundle \"${{PREPRODUCTION_ROOT}}/{registered.as_posix()}\" "
                f"--report \"${{PREPRODUCTION_ROOT}}/{segment_dir.as_posix()}/registration.json\""
            )
            results.append(
                FirstFrameRequirement(
                    episode_id=episode.episode_id,
                    segment_id=segment.segment_id,
                    wan_plan_file=plan_file,
                    wan_pending_bundle_file=pending_bundle,
                    wan_approved_master_bundle_file=current_bundle.as_posix(),
                    master_reference_asset_ids=master_ids,
                    master_reference_manifest_file=reference_manifest.as_posix(),
                    preflight_report_file=preflight_report.as_posix(),
                    keyframe_file=keyframe.as_posix(),
                    keyframe_approval_file=approval.as_posix(),
                    registered_bundle_file=registered.as_posix(),
                    prepare_command=prepare_command,
                    keyframe_preflight_command=preflight_command,
                    keyframe_paid_command_template=paid_command,
                    keyframe_approve_command=approve_command,
                    register_command=register_command,
                    blocker_codes=[
                        "approved_master_assets_missing",
                        "human_keyframe_review_pending",
                    ],
                )
            )
            current_bundle = registered
    return results


def _build_canary_recommendation(
    loaded: LoadedProduction,
    provider: LiveProviderConfig,
) -> CanaryRecommendation:
    decisions: list[CanaryEpisodeDecision] = []
    selected_episode: str | None = None
    selected_segment: str | None = None
    for episode in loaded.episodes:
        decision = select_safe_canary_candidate(
            episode.routes["wan"].plan,
            provider_clip_seconds=provider.dashscope.provider_clip_seconds,
        )
        decisions.append(
            CanaryEpisodeDecision(
                episode_id=episode.episode_id,
                selection_policy_id=decision.selection_policy_id,
                selected_segment_id=decision.selected_segment_id,
                eligible_segment_ids=decision.eligible_segment_ids,
                rejected_segments=[
                    item.model_dump(mode="json")
                    for item in decision.rejected_segments
                ],
            )
        )
        if selected_segment is None and decision.selected_segment_id is not None:
            selected_episode = episode.episode_id
            selected_segment = decision.selected_segment_id
    return CanaryRecommendation(
        recommended_episode_id=selected_episode,
        recommended_segment_id=selected_segment,
        episode_decisions=decisions,
        recommendation_ready=selected_segment is not None,
        reason=(
            "The earliest deterministic Wan single-shot candidate is recommended; "
            "no provider call is made by this recommendation."
            if selected_segment is not None
            else "No segment currently satisfies the Wan single-shot Canary contract."
        ),
    )


def _build_blockers(
    assets: list[AssetCreationRequirement],
    voices: list[VoiceCreationRequirement],
    first_frames: list[FirstFrameRequirement],
) -> list[PreproductionBlocker]:
    return [
        PreproductionBlocker(
            code="master_assets_pending",
            scope="asset",
            message="Approved character, location, and prop PNG files are not yet bound.",
            item_ids=[item.source_asset_id for item in assets],
        ),
        PreproductionBlocker(
            code="voice_identities_pending",
            scope="voice",
            message="Distinct approved voice IDs are not yet assigned.",
            item_ids=[item.source_character_id for item in voices],
        ),
        PreproductionBlocker(
            code="first_frames_pending",
            scope="first_frame",
            message="Every Wan segment still requires a reviewed lineage-bound first frame.",
            item_ids=[item.segment_id for item in first_frames],
        ),
        PreproductionBlocker(
            code="provider_credentials_runtime_validation_pending",
            scope="provider",
            message="Provider credentials are intentionally not tested by the zero-call package builder.",
        ),
        PreproductionBlocker(
            code="paid_execution_approval_pending",
            scope="provider",
            message="Every paid keyframe or video request requires a fresh matching approval digest.",
        ),
        PreproductionBlocker(
            code="human_quality_review_pending",
            scope="series",
            message="Human review is required before any generated frame or segment is accepted.",
        ),
    ]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_asset_markdown(
    path: Path,
    assets: list[AssetCreationRequirement],
) -> None:
    lines = [
        "# 素材作成チェックリスト",
        "",
        "動画生成前に用意し、目視承認する基準画像です。`instance_rules`がある素材は、同一画像を使い回せるか個別に判断します。",
        "",
        "| ID | 種類 | 名称 | 使用話 | 使用セグメント数 | バリエーション確認 | 状態 |",
        "|---|---|---|---|---:|---|---|",
    ]
    for item in assets:
        lines.append(
            "| {id} | {kind} | {name} | {episodes} | {count} | {variant} | 未作成 |".format(
                id=item.source_asset_id,
                kind=item.kind,
                name=item.name.replace("|", "\\|"),
                episodes=", ".join(item.used_episode_ids),
                count=len(item.used_segment_ids),
                variant="要確認" if item.variant_review_required else "不要",
            )
        )
    lines.extend(
        [
            "",
            "各素材の完全なprompt、negative prompt、使用箇所、bundle紐付けは`asset_creation_checklist.json`を参照してください。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_readme(
    path: Path,
    manifest: PreproductionPackageManifest,
    assets: list[AssetCreationRequirement],
    voices: list[VoiceCreationRequirement],
    first_frames: list[FirstFrameRequirement],
    routes: list[ProviderRouteSummary],
    canary: CanaryRecommendation,
) -> None:
    lines = [
        f"# {manifest.title} 動画生成前パッケージ",
        "",
        "このディレクトリは、台本・三話構成・15セグメント・provider計画・素材承認スロットを固定した、動画生成直前のzero-call成果物です。",
        "",
        "## 現在の状態",
        "",
        f"- 話数: {manifest.episode_count}",
        f"- セグメント: {manifest.segment_count}",
        f"- 基準素材: {len(assets)}件（すべて未作成・未承認）",
        f"- バリエーション判断が必要な素材: {sum(item.variant_review_required for item in assets)}件",
        f"- 固定音声: {len(voices)}名分（未承認）",
        f"- Wan第一フレーム: {len(first_frames)}枚（未生成）",
        f"- provider計画: {len(routes)}件",
        "- 外部API呼び出し: 0",
        "- 画像・動画・音声ファイル: 0",
        "",
        "## 次に行う順番",
        "",
        "1. `asset_creation_checklist.md`とJSONを見て人物・背景・小道具画像を作成する。",
        "2. `approval_templates/`の各bindingへ承認済みPNGパス、承認者、固有voice IDを入力する。",
        "3. `bundle_approval_commands.json`のzero-callコマンドで各Asset Bundleを承認する。",
        "4. `first_frame_plan.json`の順番でWan参照Manifestとkeyframe preflightを作る。",
        "5. preflight digestと費用を人間が確認した後に限り、第一フレームを1枚生成する。",
        "6. 第一フレームを目視確認し、承認・Asset Bundle登録する。",
        "7. 最初のCanary動画を1セグメントだけ生成する。",
        "",
        "## 推奨Canary",
        "",
        f"- 話: {canary.recommended_episode_id or '未選定'}",
        f"- セグメント: {canary.recommended_segment_id or '未選定'}",
        f"- 選定可能: {'はい' if canary.recommendation_ready else 'いいえ'}",
        "",
        "## 重要な境界",
        "",
        "- `production_contract/`と`source_contract/`はハッシュ固定されたスナップショットです。直接編集しません。",
        "- `keyframe_paid_command_template`は自動実行しません。preflightに表示された最新approval digestが必要です。",
        "- 素材、計画、第一フレームのどれかが変わると以前の承認は再利用できません。",
        "- 自動retryと自動provider fallbackは禁止されています。",
        "",
        "## ファイル",
        "",
    ]
    for label, relative in manifest.files.items():
        lines.append(f"- `{label}`: `{relative}`")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _verify_no_generated_media(root: Path) -> None:
    forbidden = {".mp4", ".mov", ".png", ".jpg", ".jpeg", ".wav", ".mp3"}
    found = [
        str(item.relative_to(root))
        for item in root.rglob("*")
        if item.is_file() and item.suffix.lower() in forbidden
    ]
    if found:
        raise PreproductionPackageError(
            "preproduction package unexpectedly contains generated media: "
            + ", ".join(found)
        )
