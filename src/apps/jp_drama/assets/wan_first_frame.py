"""Register and revalidate Wan first frames against approved master images."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode
from ..rendering.approval import ApprovalError, load_and_verify_approval
from .models import ApprovedAssetBundle, ApprovedReferenceAsset
from .wan_references import (
    WanMasterReferenceManifest,
    verify_wan_master_reference_manifest,
)


class WanFirstFrameError(RuntimeError):
    """A Wan first frame cannot be registered or reused safely."""


def register_wan_first_frame(
    bundle: ApprovedAssetBundle,
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    master_manifest: WanMasterReferenceManifest,
    *,
    segment_id: str,
    approval_manifest_path: str | Path,
    approved_by: str,
    approved_at: datetime | None = None,
) -> ApprovedAssetBundle:
    approver = approved_by.strip()
    if not approver:
        raise WanFirstFrameError("approved_by is required")
    try:
        verified_master = verify_wan_master_reference_manifest(
            master_manifest,
            prepared,
            plan,
            bundle,
            segment_id=segment_id,
        )
        approval, keyframe = load_and_verify_approval(
            approval_manifest_path,
            expected_shot_id=segment_id,
            expected_master_reference_manifest_digest=verified_master.content_digest,
            expected_master_reference_asset_ids=verified_master.asset_ids,
            expected_master_reference_asset_hashes=verified_master.asset_hashes,
        )
    except (ApprovalError, RuntimeError) as exc:
        raise WanFirstFrameError(str(exc)) from exc

    matches = _matching_first_frames(bundle, segment_id)
    if len(matches) != 1:
        raise WanFirstFrameError(
            f"expected exactly one first-frame slot for {segment_id}, got {len(matches)}"
        )
    target = matches[0]
    timestamp = approved_at or datetime.now(timezone.utc)
    updated_target = target.model_copy(
        update={
            "approval_status": "approved",
            "asset_path": str(keyframe),
            "asset_sha256": approval.asset_sha256,
            "mime_type": approval.mime_type,
            "width": approval.width,
            "height": approval.height,
            "approval_manifest_path": str(Path(approval_manifest_path).resolve()),
            "verified_against_asset_ids": verified_master.asset_ids,
            "generated_by": approval.generated_by,
            "operation_id": approval.operation_id,
            "approved_at": timestamp,
            "approved_by": approver,
            "rejection_reason": None,
        }
    )
    assets = [
        updated_target if item.asset_id == target.asset_id else item
        for item in bundle.assets
    ]
    return ApprovedAssetBundle.build_with_digest(
        bundle_id=bundle.bundle_id,
        source_episode_id=bundle.source_episode_id,
        source_prepared_episode_digest=bundle.source_prepared_episode_digest,
        generation_plan_digest=bundle.generation_plan_digest,
        assets=assets,
        voice_profiles=bundle.voice_profiles,
    )


def verify_wan_first_frame_ready(
    bundle: ApprovedAssetBundle,
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    master_manifest: WanMasterReferenceManifest,
    *,
    segment_id: str,
) -> tuple[ApprovedReferenceAsset, Path]:
    try:
        verified_master = verify_wan_master_reference_manifest(
            master_manifest,
            prepared,
            plan,
            bundle,
            segment_id=segment_id,
        )
    except RuntimeError as exc:
        raise WanFirstFrameError(str(exc)) from exc

    matches = _matching_first_frames(bundle, segment_id)
    if len(matches) != 1:
        raise WanFirstFrameError(
            f"expected exactly one first-frame slot for {segment_id}, got {len(matches)}"
        )
    asset = matches[0]
    if asset.approval_status != "approved":
        raise WanFirstFrameError(f"first frame is not approved for {segment_id}")
    if not asset.approval_manifest_path:
        raise WanFirstFrameError("approved first frame has no approval manifest")
    try:
        approval, keyframe = load_and_verify_approval(
            asset.approval_manifest_path,
            expected_shot_id=segment_id,
            expected_master_reference_manifest_digest=verified_master.content_digest,
            expected_master_reference_asset_ids=verified_master.asset_ids,
            expected_master_reference_asset_hashes=verified_master.asset_hashes,
        )
    except ApprovalError as exc:
        raise WanFirstFrameError(str(exc)) from exc
    if asset.asset_path is None or Path(asset.asset_path).resolve() != keyframe:
        raise WanFirstFrameError("AssetBundle first-frame path no longer matches approval")
    if asset.asset_sha256 != approval.asset_sha256:
        raise WanFirstFrameError("AssetBundle first-frame hash no longer matches approval")
    if asset.verified_against_asset_ids != verified_master.asset_ids:
        raise WanFirstFrameError("AssetBundle first-frame master lineage does not match")
    return asset, keyframe


def _matching_first_frames(
    bundle: ApprovedAssetBundle,
    segment_id: str,
) -> list[ApprovedReferenceAsset]:
    return [
        item
        for item in bundle.assets
        if item.role == "first_frame"
        and item.subject_id == segment_id
        and segment_id in item.required_for_segment_ids
    ]
