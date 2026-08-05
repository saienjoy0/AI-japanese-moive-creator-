"""Approved reference asset and voice identity support."""

from .bundle import (
    AssetBundleError,
    apply_asset_approvals,
    assess_asset_readiness,
    build_pending_asset_bundle,
    load_bindings,
    load_bundle,
    prepared_content_digest,
    write_bundle,
)
from .models import (
    ASSET_BUNDLE_SCHEMA_VERSION,
    ApprovedAssetBundle,
    ApprovedReferenceAsset,
    AssetReadinessIssue,
    AssetReadinessReport,
    VoiceIdentityProfile,
)

__all__ = [
    "ASSET_BUNDLE_SCHEMA_VERSION",
    "ApprovedAssetBundle",
    "ApprovedReferenceAsset",
    "AssetBundleError",
    "AssetReadinessIssue",
    "AssetReadinessReport",
    "VoiceIdentityProfile",
    "apply_asset_approvals",
    "assess_asset_readiness",
    "build_pending_asset_bundle",
    "load_bindings",
    "load_bundle",
    "prepared_content_digest",
    "write_bundle",
]
