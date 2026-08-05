from __future__ import annotations

import json
import shutil
import struct
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.apps.jp_drama.assets import (
    apply_asset_approvals,
    build_h3_asset_publication_preflight,
    materialize_h3_canary_asset_manifest,
    publish_h3_assets,
)
from src.apps.jp_drama.assets.publication import H3AssetPublicationError
from src.apps.jp_drama.production import SegmentArtifact
from src.apps.jp_drama.production.importer import (
    SegmentEvidence,
    SegmentImportError,
    approve_segment_import,
    build_artifact_manifest,
    inspect_segment_import,
    revalidate_segment_import,
)
from src.apps.jp_drama.rendering.ffmpeg import ffmpeg, file_sha256
from src.apps.jp_drama.rendering.minimax_h3_adapter import build_h3_first_provider_registry
from src.apps.jp_drama.rendering.minimax_h3_config import MiniMaxH3ProviderConfig
from src.apps.jp_drama.rendering.provider_config import LiveProviderConfig
from src.apps.jp_drama.seedance_storyboard import (
    SUPPORTED_ROUTES,
    build_storyboard_asset_bundle,
    build_storyboard_prepared_episode,
    compile_storyboard_generation_plan,
    load_project_directory,
    parse_project,
)
from src.apps.jp_drama.workflows.import_provider_segment import main as import_main


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "jp_drama" / "seedance_storyboard" / "upstream_fixture"
LIVE_CONFIG = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"
H3_CONFIG = ROOT / "examples" / "jp_drama" / "minimax_h3_live_provider.json"
FIXED_TIME = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)


def _package():
    return parse_project(load_project_directory(FIXTURE))


def _registry():
    return build_h3_first_provider_registry(
        LiveProviderConfig.load(LIVE_CONFIG),
        MiniMaxH3ProviderConfig.load(H3_CONFIG),
    )


def _route(route: str):
    package = _package()
    prepared = build_storyboard_prepared_episode(package, "E01")
    plan = compile_storyboard_generation_plan(
        package,
        prepared,
        "E01",
        route_id=SUPPORTED_ROUTES[route],
        registry=_registry(),
    )
    return prepared, plan


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _write_png(path: Path, width: int, height: int, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"".join(
        b"\x00" + bytes([value, value, value]) * width for _ in range(height)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def _approve_h3_masters(prepared, plan, tmp_path: Path):
    pending = build_storyboard_asset_bundle(prepared, plan)
    bindings: dict[str, dict] = {}
    for index, asset in enumerate(pending.assets):
        path = tmp_path / "masters" / f"{asset.asset_id}.png"
        _write_png(path, 72, 72, 40 + index * 20)
        bindings[asset.asset_id] = {
            "path": str(path),
            "generated_by": "fixture-master",
            "operation_id": f"fixture:{asset.asset_id}",
        }
    return apply_asset_approvals(
        pending,
        {
            "approved_by": "ci-reviewer",
            "assets": bindings,
            "voices": {},
        },
        approved_at=FIXED_TIME,
    )


class FakePublisher:
    def __init__(self) -> None:
        self.objects: set[str] = set()
        self.uploads: list[tuple[str, str]] = []
        self.signatures: list[tuple[str, int]] = []

    @property
    def is_configured(self) -> bool:
        return True

    def object_key(self, relative_path: str) -> str:
        return "test-bucket/" + relative_path.strip("/")

    def object_exists(self, object_key: str) -> bool:
        return object_key in self.objects

    def upload(self, local_path: str, relative_path: str) -> str:
        key = self.object_key(relative_path)
        assert Path(local_path).is_file()
        self.objects.add(key)
        self.uploads.append((local_path, relative_path))
        return key

    def sign_for_api(self, object_key: str, expires_seconds: int) -> str:
        self.signatures.append((object_key, expires_seconds))
        return f"https://oss.example.invalid/{object_key}?expires={expires_seconds}"


def _write_video(
    path: Path,
    *,
    seconds: int,
    fps: int,
    with_audio: bool,
    value: str = "testsrc2",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    video_source = f"{value}=size=90x160:rate={fps}:duration={seconds}"
    arguments = ["-f", "lavfi", "-i", video_source]
    if with_audio:
        arguments.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=48000:duration={seconds}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
            ]
        )
    else:
        arguments.append("-an")
    arguments.extend(
        [
            "-frames:v",
            str(seconds * fps),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    ffmpeg(*arguments)


def test_h3_approved_assets_publish_once_and_materialize_exact_manifest(
    tmp_path: Path,
) -> None:
    prepared, plan = _route("h3")
    bundle = _approve_h3_masters(prepared, plan, tmp_path)
    segment = plan.segments[0]

    preflight = build_h3_asset_publication_preflight(
        prepared,
        plan,
        bundle,
        segment_id=segment.segment_id,
    )

    assert preflight.external_storage_calls == 0
    assert preflight.generation_plan_digest == plan.content_digest
    assert preflight.asset_bundle_digest == bundle.content_digest
    assert [item.asset_id for item in preflight.items] == segment.reference_asset_ids
    assert len(preflight.items) == 4
    assert all(item.object_relative_path.endswith(".png") for item in preflight.items)

    publisher = FakePublisher()
    first = publish_h3_assets(
        preflight,
        approval_digest=preflight.content_digest,
        execute_upload=True,
        publisher=publisher,
        expires_seconds=1800,
        now=FIXED_TIME,
    )
    assert first.external_storage_uploads == 4
    assert first.external_storage_signatures == 4
    assert len(publisher.uploads) == 4

    h3_manifest = materialize_h3_canary_asset_manifest(
        preflight,
        first,
        now=FIXED_TIME + timedelta(seconds=60),
    )
    assert h3_manifest.segment_id == segment.segment_id
    assert [item.asset_id for item in h3_manifest.assets] == segment.reference_asset_ids
    assert all(item.url.startswith("https://") for item in h3_manifest.assets)
    assert [item.sha256 for item in h3_manifest.assets] == [
        item.local_sha256 for item in preflight.items
    ]

    second = publish_h3_assets(
        preflight,
        approval_digest=preflight.content_digest,
        execute_upload=True,
        publisher=publisher,
        expires_seconds=1800,
        now=FIXED_TIME + timedelta(minutes=1),
    )
    assert second.external_storage_uploads == 0
    assert len(publisher.uploads) == 4
    assert len(publisher.signatures) == 8


def test_h3_publication_fails_closed_on_digest_or_expiry(tmp_path: Path) -> None:
    prepared, plan = _route("h3")
    bundle = _approve_h3_masters(prepared, plan, tmp_path)
    preflight = build_h3_asset_publication_preflight(
        prepared,
        plan,
        bundle,
        segment_id=plan.segments[0].segment_id,
    )
    publisher = FakePublisher()

    with pytest.raises(H3AssetPublicationError, match="approval digest"):
        publish_h3_assets(
            preflight,
            approval_digest="sha256:" + "0" * 64,
            execute_upload=True,
            publisher=publisher,
        )

    published = publish_h3_assets(
        preflight,
        approval_digest=preflight.content_digest,
        execute_upload=True,
        publisher=publisher,
        expires_seconds=600,
        now=FIXED_TIME,
    )
    with pytest.raises(H3AssetPublicationError, match="expiry"):
        materialize_h3_canary_asset_manifest(
            preflight,
            published,
            now=FIXED_TIME + timedelta(seconds=400),
            minimum_remaining_seconds=300,
        )


def test_manual_seedance_mp4_becomes_approved_segment_artifact(
    tmp_path: Path,
) -> None:
    _, plan = _route("seedance")
    segment = plan.segments[0]
    output = tmp_path / "seedance.mp4"
    _write_video(
        output,
        seconds=segment.requested_duration_seconds,
        fps=segment.timeline_fps,
        with_audio=True,
    )
    evidence = SegmentEvidence(
        kind="seedance_operator",
        operator_notes="Official Seedance platform output reviewed for identity and timing.",
    )

    preflight = inspect_segment_import(
        plan,
        segment_id=segment.segment_id,
        output_path=output,
        evidence=evidence,
    )
    assert preflight.valid is True
    assert preflight.external_api_calls == 0
    assert preflight.evidence_paths == {}
    approval, artifact = approve_segment_import(
        plan,
        preflight,
        approved_by="operator-reviewer",
        approved_at=FIXED_TIME,
    )
    assert artifact.segment_id == segment.segment_id
    assert artifact.provider_route_id == "seedance/platform"
    assert artifact.approval_digest == approval.content_digest
    assert artifact.ledger_path is None
    assert artifact.audio_present is True

    output.write_bytes(output.read_bytes() + b"changed-after-preflight")
    with pytest.raises(SegmentImportError, match="changed after preflight"):
        revalidate_segment_import(plan, preflight, evidence=evidence)


def test_wan_and_h3_automated_evidence_bind_ledgers_and_output(
    tmp_path: Path,
) -> None:
    for route, evidence_kind, with_audio in (
        ("wan", "wan_canary", False),
        ("h3", "minimax_h3_canary", True),
    ):
        _, plan = _route(route)
        segment = plan.segments[0]
        output = tmp_path / route / "segment.mp4"
        _write_video(
            output,
            seconds=segment.requested_duration_seconds,
            fps=segment.timeline_fps,
            with_audio=with_audio,
        )
        report_path = tmp_path / route / "report.json"
        ledger_path = tmp_path / route / "ledger.json"
        approval_path = tmp_path / route / "request-approval.json"
        report = {
            "valid": True,
            "stage": "render",
            "status": "succeeded" if route == "wan" else "validated",
            "segment_id": segment.segment_id,
            "provider_route_id": segment.provider_route_id,
            "generation_plan_digest": plan.content_digest,
            "output": str(output.resolve()),
            "delegate_exit_code": 0,
            "submission_attempts": 1,
        }
        if route == "wan":
            ledger = {
                "shot_id": segment.segment_id,
                "operations": {
                    "video": {
                        "operation_id": "video",
                        "stage": "render",
                        "shot_id": segment.segment_id,
                        "operation_type": "video",
                        "provider": "dashscope",
                        "model": "wan2.7-i2v",
                        "status": "succeeded",
                    }
                },
            }
            approval = {"shot_id": segment.segment_id}
        else:
            ledger = {
                "segment_id": segment.segment_id,
                "route_id": segment.provider_route_id,
                "status": "validated",
                "submission_attempts": 1,
                "external_api_calls": 1,
                "final_video_path": str(output.resolve()),
                "final_video_sha256": file_sha256(output),
            }
            approval = {"segment_id": segment.segment_id}
        report_path.write_text(json.dumps(report), encoding="utf-8")
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
        evidence = SegmentEvidence(
            kind=evidence_kind,
            report_path=str(report_path),
            ledger_path=str(ledger_path),
            approval_manifest_path=str(approval_path),
        )

        preflight = inspect_segment_import(
            plan,
            segment_id=segment.segment_id,
            output_path=output,
            evidence=evidence,
        )
        assert preflight.valid is True
        assert preflight.evidence_paths["ledger"] == str(ledger_path.resolve())
        _, artifact = approve_segment_import(
            plan,
            preflight,
            approved_by="ci-reviewer",
            approved_at=FIXED_TIME,
        )
        assert artifact.ledger_path == str(ledger_path.resolve())


def test_artifact_manifest_requires_exact_generation_plan_order() -> None:
    _, plan = _route("seedance")
    artifacts = [
        SegmentArtifact(
            segment_id=segment.segment_id,
            generation_plan_digest=plan.content_digest,
            provider_route_id=segment.provider_route_id,
            output_path=f"/tmp/{segment.segment_id}.mp4",
            output_sha256="sha256:" + f"{index:064x}"[-64:],
            width=90,
            height=160,
            fps=segment.timeline_fps,
            frame_count=segment.requested_duration_seconds * segment.timeline_fps,
            duration_seconds=segment.requested_duration_seconds,
            audio_present=True,
            approval_digest="sha256:" + f"{index + 100:064x}"[-64:],
            imported_by="fixture",
            valid=True,
        )
        for index, segment in enumerate(plan.segments, start=1)
    ]
    manifest = build_artifact_manifest(plan, artifacts)
    assert [item.segment_id for item in manifest.artifacts] == [
        item.segment_id for item in plan.segments
    ]
    with pytest.raises(SegmentImportError, match="order"):
        build_artifact_manifest(plan, list(reversed(artifacts)))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_segment_import_cli_requires_exact_preflight_digest(tmp_path: Path) -> None:
    _, plan = _route("seedance")
    segment = plan.segments[0]
    plan_path = tmp_path / "plan.json"
    output = tmp_path / "segment.mp4"
    result_dir = tmp_path / "import"
    plan_path.write_text(plan.to_canonical_json() + "\n", encoding="utf-8")
    _write_video(
        output,
        seconds=segment.requested_duration_seconds,
        fps=segment.timeline_fps,
        with_audio=True,
    )
    common = [
        "--generation-plan",
        str(plan_path),
        "--segment-id",
        segment.segment_id,
        "--input",
        str(output),
        "--evidence-kind",
        "seedance_operator",
        "--operator-notes",
        "Official platform output visually reviewed by the operator.",
        "--output-dir",
        str(result_dir),
    ]
    assert import_main(common) == 0
    preflight_path = result_dir / f"{segment.segment_id}.import.preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    assert (
        import_main(
            [
                *common,
                "--stage",
                "approve",
                "--stored-preflight",
                str(preflight_path),
                "--preflight-digest",
                "sha256:" + "0" * 64,
                "--approved-by",
                "ci-reviewer",
            ]
        )
        == 7
    )
    assert (
        import_main(
            [
                *common,
                "--stage",
                "approve",
                "--stored-preflight",
                str(preflight_path),
                "--preflight-digest",
                preflight["content_digest"],
                "--approved-by",
                "ci-reviewer",
            ]
        )
        == 0
    )
    artifact_path = result_dir / f"{segment.segment_id}.segment_artifact.json"
    artifact = SegmentArtifact.model_validate_json(
        artifact_path.read_text(encoding="utf-8")
    )
    assert artifact.approval_digest.startswith("sha256:")
