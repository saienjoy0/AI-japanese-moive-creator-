"""Approved asset, voice identity, Wan references, and optional H3 publication.

The core asset package must remain importable without eagerly loading the H3
publication stack.  H3 publication symbols are resolved lazily so Wan and
HappyHorse entry points do not enter the H3/segment-canary import cycle.
"""

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
from .wan_first_frame import (
    WanFirstFrameError,
    register_wan_first_frame,
    verify_wan_first_frame_ready,
)
from .wan_references import (
    WAN_MASTER_REFERENCE_SCHEMA_VERSION,
    WanMasterReference,
    WanMasterReferenceError,
    WanMasterReferenceManifest,
    build_wan_master_reference_manifest,
    load_wan_master_reference_manifest,
    verify_wan_master_reference_manifest,
    write_wan_master_reference_manifest,
)

_H3_EXPORTS = {
    "H3_ASSET_PUBLICATION_SCHEMA_VERSION",
    "H3AssetPublicationError",
    "H3AssetPublicationPreflight",
    "H3PublishedAssetManifest",
    "OSSH3AssetPublisher",
    "PublishedH3Asset",
    "materialize_h3_canary_asset_manifest",
    "publish_h3_assets",
}


def __getattr__(name: str):
    if name == "build_h3_asset_publication_preflight":
        from .publication import (
            build_h3_asset_publication_preflight_for_episode,
        )

        return build_h3_asset_publication_preflight_for_episode
    if name in _H3_EXPORTS:
        from . import publication

        return getattr(publication, name)
    raise AttributeError(name)


__all__ = [
    "ASSET_BUNDLE_SCHEMA_VERSION",
    "H3_ASSET_PUBLICATION_SCHEMA_VERSION",
    "WAN_MASTER_REFERENCE_SCHEMA_VERSION",
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
    "WanFirstFrameError",
    "WanMasterReference",
    "WanMasterReferenceError",
    "WanMasterReferenceManifest",
    "apply_asset_approvals",
    "assess_asset_readiness",
    "build_h3_asset_publication_preflight",
    "build_pending_asset_bundle",
    "build_wan_master_reference_manifest",
    "load_bindings",
    "load_bundle",
    "load_wan_master_reference_manifest",
    "materialize_h3_canary_asset_manifest",
    "prepared_content_digest",
    "publish_h3_assets",
    "register_wan_first_frame",
    "verify_wan_first_frame_ready",
    "verify_wan_master_reference_manifest",
    "write_bundle",
    "write_wan_master_reference_manifest",
]
