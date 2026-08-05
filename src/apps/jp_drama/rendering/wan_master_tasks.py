"""Wan 2.7 executor that requires approved master images for new keyframes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..assets.wan_references import WanMasterReferenceManifest
from ..preparation.models import PreparedEpisode
from .canary_tasks import Wan27LiveTaskExecutor
from .ffmpeg import file_sha256
from .mock_tasks import TaskContext
from .provider_config import LiveProviderConfig, ProviderConfigurationError
from .provider_ledger import CanaryProviderLedger, CanaryProviderLedgerStore


class WanMasterReferenceLiveTaskExecutor(Wan27LiveTaskExecutor):
    """Fail closed unless a new Wan keyframe has approved master references."""

    def __init__(
        self,
        config: LiveProviderConfig,
        *,
        master_references: dict[str, WanMasterReferenceManifest] | None = None,
        image_model: Any | None = None,
        video_model: Any | None = None,
        tts_processor: Any | None = None,
        require_credentials: bool = True,
        api_call_limit: int | None = None,
        approved_keyframes: dict[str, str | Path] | None = None,
        ledger_store: CanaryProviderLedgerStore | None = None,
        ledger: CanaryProviderLedger | None = None,
    ) -> None:
        self.master_references = dict(master_references or {})
        for shot_id, manifest in self.master_references.items():
            if shot_id != manifest.segment_id:
                raise ValueError(
                    f"master reference key {shot_id} does not match manifest "
                    f"{manifest.segment_id}"
                )
            self._verify_manifest_files(manifest)
        super().__init__(
            config,
            image_model=image_model,
            video_model=video_model,
            tts_processor=tts_processor,
            require_credentials=require_credentials,
            api_call_limit=api_call_limit,
            approved_keyframes=approved_keyframes,
            ledger_store=ledger_store,
            ledger=ledger,
        )

    @property
    def execution_profile(self) -> str:
        lineage = {
            shot_id: {
                "manifest": manifest.content_digest,
                "assets": [
                    [item.asset_id, item.asset_sha256]
                    for item in manifest.references
                ],
            }
            for shot_id, manifest in sorted(self.master_references.items())
        }
        payload = json.dumps(
            {
                "base": super().execution_profile,
                "master_references": lineage,
                "protocol": "wan27-approved-master-references-v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"live:wan27-master:sha256:{hashlib.sha256(payload).hexdigest()}"

    @property
    def provider_manifest(self) -> dict[str, str]:
        manifest = dict(super().provider_manifest)
        manifest["master_reference_protocol"] = (
            "wan27-approved-master-references-v1"
        )
        manifest["master_reference_shots"] = str(len(self.master_references))
        return manifest

    @staticmethod
    def ledger_source_digest(
        prepared: PreparedEpisode,
        manifest: WanMasterReferenceManifest,
    ) -> str:
        payload = json.dumps(
            {
                "prepared_source_digest": prepared.source_digest,
                "master_reference_manifest_digest": manifest.content_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def generate_canary_keyframe(
        self,
        prepared: PreparedEpisode,
        *,
        shot_id: str,
        output: str | Path,
    ) -> Path:
        manifest = self._require_manifest(shot_id)
        frame = next(
            (
                item
                for item in prepared.storyboard_frame_drafts
                if item.source_shot_id == shot_id
            ),
            None,
        )
        node = next(
            (
                item
                for item in prepared.render_graph.nodes
                if item.shot_id == shot_id
                and item.task_type
                in {"generate_video", "generate_native_av", "generate_image"}
            ),
            None,
        )
        if frame is None or node is None:
            raise ProviderConfigurationError(
                f"shot cannot generate a keyframe: {shot_id}"
            )
        self._ensure_clients()
        destination = Path(output).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        context = TaskContext(
            prepared=prepared,
            frame=frame,
            node=node,
            work_dir=destination.parent,
            dependency_outputs=[],
        )
        provider = self.config.dashscope
        self._provider_call(
            self.image_model.generate,
            self._visual_prompt(context),
            str(destination),
            model_name=provider.image_model,
            size=provider.image_size,
            seed=self._seed(context, "canary-keyframe"),
            watermark=provider.watermark,
            ref_image_paths=manifest.asset_paths,
            _operation_id=self.keyframe_operation_id(node.task_id, manifest),
            _stage="keyframe",
            _operation_type="image",
            _model=provider.image_model,
            _estimated_cost_cny=provider.estimate_image_cost_cny(),
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ProviderConfigurationError(
                f"provider returned no usable keyframe: {destination}"
            )
        return destination

    def _copy_or_generate_keyframe(
        self,
        context: TaskContext,
        output: Path,
        *,
        purpose: str,
    ) -> Path:
        approved = self.approved_keyframes.get(context.frame.source_shot_id)
        if approved is not None:
            return super()._copy_or_generate_keyframe(
                context,
                output,
                purpose=purpose,
            )
        manifest = self._require_manifest(context.frame.source_shot_id)
        self._ensure_clients()
        provider = self.config.dashscope
        self._provider_call(
            self.image_model.generate,
            self._visual_prompt(context),
            str(output),
            model_name=provider.image_model,
            size=provider.image_size,
            seed=self._seed(context, purpose),
            watermark=provider.watermark,
            ref_image_paths=manifest.asset_paths,
            _operation_id=self.keyframe_operation_id(
                context.node.task_id,
                manifest,
                purpose=purpose,
            ),
            _stage="render",
            _operation_type="image",
            _model=provider.image_model,
            _estimated_cost_cny=provider.estimate_image_cost_cny(),
        )
        return output

    @staticmethod
    def keyframe_operation_id(
        task_id: str,
        manifest: WanMasterReferenceManifest,
        *,
        purpose: str = "canary-keyframe",
    ) -> str:
        return (
            f"{task_id}:{purpose}:masters-"
            f"{manifest.content_digest.split(':', 1)[1][:16]}"
        )

    def _require_manifest(self, shot_id: str) -> WanMasterReferenceManifest:
        manifest = self.master_references.get(shot_id)
        if manifest is None:
            raise ProviderConfigurationError(
                "Wan keyframe generation requires an approved "
                f"WanMasterReferenceManifest for {shot_id}; no provider request submitted"
            )
        self._verify_manifest_files(manifest)
        return manifest

    @staticmethod
    def _verify_manifest_files(manifest: WanMasterReferenceManifest) -> None:
        for item in manifest.references:
            path = Path(item.asset_path).resolve()
            if not path.is_file() or path.stat().st_size == 0:
                raise ProviderConfigurationError(
                    f"Wan master reference is missing: {path}"
                )
            if file_sha256(path) != item.asset_sha256:
                raise ProviderConfigurationError(
                    f"Wan master reference hash changed: {item.asset_id}"
                )
