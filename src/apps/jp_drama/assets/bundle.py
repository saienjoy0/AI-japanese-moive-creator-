"""Build, approve, and verify reference asset bundles without provider calls."""

from __future__ import annotations

import json
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..preparation.models import PreparedEpisode
from ..rendering.approval import (
    ApprovalError,
    load_and_verify_approval,
    png_dimensions,
)
from ..rendering.ffmpeg import file_sha256
from .models import (
    ApprovedAssetBundle,
    ApprovedReferenceAsset,
    AssetReadinessIssue,
    AssetReadinessReport,
    AssetReadinessStage,
    VoiceIdentityProfile,
)


class AssetBundleError(RuntimeError):
    """An asset bundle cannot be built, approved, or verified."""


_ROLE_MAP = {
    "character": "character_master",
    "location": "location_master",
    "prop": "prop_master",
    "first_frame": "first_frame",
}


def prepared_content_digest(prepared: PreparedEpisode) -> str:
    import hashlib

    canonical = prepared.to_canonical_json(indent=None).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def build_pending_asset_bundle(
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
) -> ApprovedAssetBundle:
    if plan.source_prepared_episode_digest != prepared_content_digest(prepared):
        raise AssetBundleError(
            "generation plan does not belong to the supplied PreparedEpisode"
        )

    assets = [
        ApprovedReferenceAsset(
            asset_id=requirement.asset_id,
            role=_ROLE_MAP[requirement.role],
            subject_id=requirement.subject_id,
            continuity_group_id=requirement.continuity_group_id,
            required_for_segment_ids=sorted(requirement.required_for_segment_ids),
        )
        for requirement in sorted(
            plan.reference_asset_requirements,
            key=lambda item: item.asset_id,
        )
    ]

    speakers = sorted(
        {
            item.speaker_character_id
            for segment in plan.segments
            for item in segment.dialogue_slices
        }
    )
    source_by_seed = {
        item.seed_id: item.source_character_id for item in prepared.character_seeds
    }
    voices = [
        VoiceIdentityProfile(
            profile_id=f"voice_{character_seed_id}",
            character_seed_id=character_seed_id,
            source_character_id=source_by_seed.get(
                character_seed_id,
                character_seed_id,
            ),
            provider="qwen3",
        )
        for character_seed_id in speakers
    ]

    return ApprovedAssetBundle.build_with_digest(
        bundle_id=f"assets_{plan.generation_plan_episode_id}",
        source_episode_id=plan.source_episode_id,
        source_prepared_episode_digest=plan.source_prepared_episode_digest,
        generation_plan_digest=plan.content_digest,
        assets=assets,
        voice_profiles=voices,
    )


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
    except (wave.Error, OSError) as exc:
        raise AssetBundleError(f"invalid WAV asset {path}: {exc}") from exc
    if frames <= 0 or rate <= 0:
        raise AssetBundleError(f"WAV asset is empty or invalid: {path}")
    return frames / rate


def _approve_asset(
    asset: ApprovedReferenceAsset,
    binding: dict[str, Any],
    *,
    approved_by: str,
    approved_at: datetime,
) -> ApprovedReferenceAsset:
    path = Path(str(binding.get("path", ""))).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise AssetBundleError(f"asset does not exist or is empty: {path}")

    verified_against = sorted(
        set(str(item) for item in binding.get("verified_against_asset_ids", []))
    )
    generated_by = str(binding.get("generated_by", "manual"))
    operation_id = str(binding.get("operation_id", f"manual:{asset.asset_id}"))

    if asset.role == "voice_reference":
        duration = _wav_duration(path)
        return asset.model_copy(
            update={
                "approval_status": "approved",
                "asset_path": str(path),
                "asset_sha256": file_sha256(path),
                "mime_type": "audio/wav",
                "duration_seconds": duration,
                "verified_against_asset_ids": verified_against,
                "generated_by": generated_by,
                "operation_id": operation_id,
                "approved_at": approved_at,
                "approved_by": approved_by,
                "rejection_reason": None,
            }
        )

    if asset.role == "first_frame":
        manifest_path = Path(str(binding.get("approval_manifest_path", ""))).resolve()
        try:
            manifest, verified_path = load_and_verify_approval(
                manifest_path,
                expected_shot_id=asset.subject_id,
            )
        except ApprovalError as exc:
            raise AssetBundleError(str(exc)) from exc
        if verified_path != path:
            raise AssetBundleError(
                f"first-frame binding path {path} differs from approval asset "
                f"{verified_path}"
            )
        return asset.model_copy(
            update={
                "approval_status": "approved",
                "asset_path": str(path),
                "asset_sha256": manifest.asset_sha256,
                "mime_type": manifest.mime_type,
                "width": manifest.width,
                "height": manifest.height,
                "approval_manifest_path": str(manifest_path),
                "verified_against_asset_ids": verified_against,
                "generated_by": manifest.generated_by,
                "operation_id": manifest.operation_id,
                "approved_at": approved_at,
                "approved_by": approved_by,
                "rejection_reason": None,
            }
        )

    try:
        width, height = png_dimensions(path)
    except ApprovalError as exc:
        raise AssetBundleError(str(exc)) from exc
    return asset.model_copy(
        update={
            "approval_status": "approved",
            "asset_path": str(path),
            "asset_sha256": file_sha256(path),
            "mime_type": "image/png",
            "width": width,
            "height": height,
            "verified_against_asset_ids": verified_against,
            "generated_by": generated_by,
            "operation_id": operation_id,
            "approved_at": approved_at,
            "approved_by": approved_by,
            "rejection_reason": None,
        }
    )


def apply_asset_approvals(
    bundle: ApprovedAssetBundle,
    bindings: dict[str, Any],
    *,
    approved_at: datetime | None = None,
) -> ApprovedAssetBundle:
    approver = str(bindings.get("approved_by", "")).strip()
    if not approver:
        raise AssetBundleError("bindings require approved_by")
    timestamp = approved_at or datetime.now(timezone.utc)
    asset_bindings = bindings.get("assets", {})
    voice_bindings = bindings.get("voices", {})
    if not isinstance(asset_bindings, dict) or not isinstance(voice_bindings, dict):
        raise AssetBundleError("assets and voices bindings must be objects")

    updated_assets = []
    for asset in bundle.assets:
        binding = asset_bindings.get(asset.asset_id)
        if binding is None:
            updated_assets.append(asset)
            continue
        if not isinstance(binding, dict):
            raise AssetBundleError(f"asset binding must be an object: {asset.asset_id}")
        updated_assets.append(
            _approve_asset(
                asset,
                binding,
                approved_by=approver,
                approved_at=timestamp,
            )
        )

    updated_voices = []
    for profile in bundle.voice_profiles:
        binding = voice_bindings.get(profile.source_character_id)
        if binding is None:
            binding = voice_bindings.get(profile.character_seed_id)
        if binding is None:
            updated_voices.append(profile)
            continue
        if not isinstance(binding, dict):
            raise AssetBundleError(
                f"voice binding must be an object: {profile.source_character_id}"
            )
        provider = str(binding.get("provider", profile.provider)).strip()
        voice_id = str(binding.get("voice_id", "")).strip()
        if not provider or not voice_id:
            raise AssetBundleError(
                f"voice binding requires provider and voice_id: {profile.source_character_id}"
            )
        updated_voices.append(
            profile.model_copy(
                update={
                    "provider": provider,
                    "voice_id": voice_id,
                    "language": str(binding.get("language", profile.language)),
                    "speaking_rate": float(
                        binding.get("speaking_rate", profile.speaking_rate)
                    ),
                    "pronunciation_dictionary": dict(
                        binding.get(
                            "pronunciation_dictionary",
                            profile.pronunciation_dictionary,
                        )
                    ),
                    "reference_audio_asset_id": binding.get(
                        "reference_audio_asset_id",
                        profile.reference_audio_asset_id,
                    ),
                    "allow_shared_voice": bool(
                        binding.get("allow_shared_voice", False)
                    ),
                    "approval_status": "approved",
                    "approved_at": timestamp,
                    "approved_by": approver,
                    "rejection_reason": None,
                }
            )
        )

    return ApprovedAssetBundle.build_with_digest(
        bundle_id=bundle.bundle_id,
        source_episode_id=bundle.source_episode_id,
        source_prepared_episode_digest=bundle.source_prepared_episode_digest,
        generation_plan_digest=bundle.generation_plan_digest,
        assets=updated_assets,
        voice_profiles=updated_voices,
    )


def _verify_asset_file(asset: ApprovedReferenceAsset) -> str | None:
    if asset.approval_status != "approved":
        return "asset is not approved"
    if not asset.asset_path or not asset.asset_sha256:
        return "approved asset metadata is incomplete"
    path = Path(asset.asset_path).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        return "approved asset file is missing or empty"
    if file_sha256(path) != asset.asset_sha256:
        return "approved asset hash changed after approval"

    try:
        if asset.mime_type == "image/png":
            width, height = png_dimensions(path)
            if (width, height) != (asset.width, asset.height):
                return "approved image dimensions changed after approval"
            if asset.role == "first_frame":
                if not asset.approval_manifest_path:
                    return "approved first frame has no approval manifest"
                manifest, verified = load_and_verify_approval(
                    asset.approval_manifest_path,
                    expected_shot_id=asset.subject_id,
                )
                if verified != path or manifest.asset_sha256 != asset.asset_sha256:
                    return "first-frame approval manifest no longer matches the asset"
        elif asset.mime_type == "audio/wav":
            duration = _wav_duration(path)
            if asset.duration_seconds is None or abs(duration - asset.duration_seconds) > 0.01:
                return "approved audio duration changed after approval"
        else:
            return "approved asset has an unsupported MIME type"
    except (ApprovalError, AssetBundleError) as exc:
        return str(exc)
    return None


def _selected_segments(
    plan: GenerationPlanEpisode,
    segment_ids: list[str] | None,
) -> list[GenerationSegment]:
    if segment_ids is None:
        return list(plan.segments)
    requested = set(segment_ids)
    selected = [item for item in plan.segments if item.segment_id in requested]
    found = {item.segment_id for item in selected}
    missing = sorted(requested - found)
    if missing:
        raise AssetBundleError(f"unknown segment IDs: {missing}")
    return selected


def assess_asset_readiness(
    bundle: ApprovedAssetBundle,
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    *,
    stage: AssetReadinessStage,
    segment_ids: list[str] | None = None,
) -> AssetReadinessReport:
    errors: list[AssetReadinessIssue] = []
    warnings: list[AssetReadinessIssue] = []

    if bundle.generation_plan_digest != plan.content_digest:
        errors.append(
            AssetReadinessIssue(
                code="asset_bundle_plan_mismatch",
                severity="error",
                message="asset bundle does not belong to the supplied GenerationPlan",
            )
        )
    if bundle.source_prepared_episode_digest != prepared_content_digest(prepared):
        errors.append(
            AssetReadinessIssue(
                code="asset_bundle_prepared_mismatch",
                severity="error",
                message="asset bundle does not belong to the supplied PreparedEpisode",
            )
        )

    selected = _selected_segments(plan, segment_ids)
    selected_ids = {item.segment_id for item in selected}
    required_assets = [
        item
        for item in bundle.assets
        if selected_ids & set(item.required_for_segment_ids)
    ]
    asset_by_id = {item.asset_id: item for item in bundle.assets}

    if stage in {"preflight"}:
        mandatory_roles: set[str] = set()
    elif stage in {"keyframe", "approve"}:
        mandatory_roles = {"character_master", "location_master", "prop_master"}
    else:
        mandatory_roles = {
            "character_master",
            "location_master",
            "prop_master",
            "first_frame",
        }

    for asset in required_assets:
        problem = _verify_asset_file(asset)
        mandatory = asset.role in mandatory_roles
        target = errors if mandatory else warnings
        if problem:
            target.append(
                AssetReadinessIssue(
                    code=(
                        "required_asset_not_ready"
                        if mandatory
                        else "asset_not_ready"
                    ),
                    severity="error" if mandatory else "warning",
                    message=problem,
                    asset_id=asset.asset_id,
                    segment_id=(
                        asset.required_for_segment_ids[0]
                        if len(asset.required_for_segment_ids) == 1
                        else None
                    ),
                )
            )

    for segment in selected:
        frame_assets = [
            item
            for item in required_assets
            if item.role == "first_frame"
            and segment.segment_id in item.required_for_segment_ids
        ]
        master_ids = {
            item.asset_id
            for item in required_assets
            if item.role in {"character_master", "location_master", "prop_master"}
            and segment.segment_id in item.required_for_segment_ids
        }
        if stage in {"render", "full_episode"}:
            if len(frame_assets) != 1:
                errors.append(
                    AssetReadinessIssue(
                        code="first_frame_requirement_invalid",
                        severity="error",
                        message="render requires exactly one first-frame asset",
                        segment_id=segment.segment_id,
                    )
                )
            elif frame_assets[0].approval_status == "approved":
                missing_lineage = master_ids - set(
                    frame_assets[0].verified_against_asset_ids
                )
                if missing_lineage:
                    errors.append(
                        AssetReadinessIssue(
                            code="first_frame_lineage_incomplete",
                            severity="error",
                            message=(
                                "approved first frame was not verified against all "
                                f"required master assets: {sorted(missing_lineage)}"
                            ),
                            asset_id=frame_assets[0].asset_id,
                            segment_id=segment.segment_id,
                        )
                    )

    speaker_ids = sorted(
        {
            dialogue.speaker_character_id
            for segment in selected
            for dialogue in segment.dialogue_slices
        }
    )
    voice_by_character = {
        item.character_seed_id: item for item in bundle.voice_profiles
    }
    if stage in {"render", "full_episode"}:
        for character_id in speaker_ids:
            profile = voice_by_character.get(character_id)
            if profile is None or profile.approval_status != "approved":
                errors.append(
                    AssetReadinessIssue(
                        code="voice_profile_not_ready",
                        severity="error",
                        message="speaker has no approved voice identity",
                        character_seed_id=character_id,
                    )
                )

    required_ids = sorted(item.asset_id for item in required_assets)
    return AssetReadinessReport(
        stage=stage,
        generation_plan_digest=plan.content_digest,
        bundle_digest=bundle.content_digest,
        selected_segment_ids=sorted(selected_ids),
        required_asset_ids=required_ids,
        required_voice_character_ids=speaker_ids,
        ready=not errors,
        errors=errors,
        warnings=warnings,
    )


def write_bundle(path: str | Path, bundle: ApprovedAssetBundle) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(bundle.to_canonical_json() + "\n", encoding="utf-8")


def load_bundle(path: str | Path) -> ApprovedAssetBundle:
    return ApprovedAssetBundle.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def load_bindings(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssetBundleError("approval bindings root must be an object")
    return payload
