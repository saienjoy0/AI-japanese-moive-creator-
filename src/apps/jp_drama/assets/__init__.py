"""Approved asset, voice identity, and H3 publication support."""

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
from .publication import (
    H3_ASSET_PUBLICATION_SCHEMA_VERSION,
    H3AssetPublicationError,
    H3AssetPublicationPreflight,
    H3PublishedAssetManifest,
    OSSH3AssetPublisher,
    PublishedH3Asset,
    build_h3_asset_publication_preflight_for_episode as build_h3_asset_publication_preflight,
    materialize_h3_canary_asset_manifest,
    publish_h3_assets,
)

__all__ = [
    "ASSET_BUNDLE_SCHEMA_VERSION",
    "H3_ASSET_PUBLICATION_SCHEMA_VERSION",
    "ApprovedAssetBundle",
    "ApprovedReferenceAsset",
    "AssetBundleError",
    "AssetReadinessIssue",
    "AssetReadinessReport",
    "H3AssetPublicationError",
    "H3AssetPublicationPreflight",
    "H3PublishedAssetManifest",
    "OSSH3AssetPublisher",
    "PublishedH3Asset",
    "VoiceIdentityProfile",
    "apply_asset_approvals",
    "assess_asset_readiness",
    "build_h3_asset_publication_preflight",
    "build_pending_asset_bundle",
    "load_bindings",
    "load_bundle",
    "materialize_h3_canary_asset_manifest",
    "prepared_content_digest",
    "publish_h3_assets",
    "write_bundle",
]
