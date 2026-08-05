from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

from src.apps.jp_drama.workflows.apply_master_reference_approval import main


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _write_png(path: Path, *, width: int, height: int, value: int) -> None:
    raw = b"".join(
        b"\x00" + bytes([value, value, value]) * width for _ in range(height)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _approval_asset(root: Path, source_id: str, relative: str, value: int) -> dict:
    path = root / relative
    _write_png(path, width=54, height=96, value=value)
    return {
        "source_asset_id": source_id,
        "path": relative,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "width": 54,
        "height": 96,
        "format": "PNG",
        "generated_by": "fixture/image-generation",
        "operation_id": f"human-approved:test:{source_id}",
    }


def _write_package(root: Path) -> None:
    for episode in ("E01", "E02", "E03"):
        for route in ("h3", "wan", "seedance"):
            path = (
                root
                / "approval_templates"
                / episode
                / route
                / "bindings.template.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "approved_by": "",
                        "assets": {
                            f"ref_char_C01_{episode}_{route}": {
                                "path": "",
                                "generated_by": "pending",
                                "operation_id": "pending",
                                "verified_against_asset_ids": [],
                                "source_asset_id": "C01",
                            },
                            f"ref_prop_P01_{episode}_{route}": {
                                "path": "",
                                "generated_by": "pending",
                                "operation_id": "pending",
                                "verified_against_asset_ids": [],
                                "source_asset_id": "P01",
                            },
                        },
                        "voices": {
                            "C01": {
                                "provider": "qwen3-tts",
                                "voice_id": "",
                            }
                        },
                        "_instructions": "fill",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    commands = [
        {
            "episode_id": episode,
            "route": route,
            "status": "blocked_until_paths_and_voices_are_filled",
        }
        for episode in ("E01", "E02", "E03")
        for route in ("h3", "wan", "seedance")
    ]
    (root / "bundle_approval_commands.json").write_text(
        json.dumps(commands, indent=2) + "\n",
        encoding="utf-8",
    )


def test_human_approval_fills_all_templates_but_preserves_voice_gate(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    package = tmp_path / "preproduction"
    repository.mkdir()
    package.mkdir()
    assets = [
        _approval_asset(repository, "C01", "assets/C01.png", 50),
        _approval_asset(repository, "P01", "assets/P01.png", 100),
    ]
    approval = {
        "schema_version": "1.0",
        "approval_id": "fixture-approval",
        "decision": "approved",
        "scope": "master_reference_images_only",
        "approved_by": "human-reviewer",
        "approved_at": "2026-08-05T23:20:00+09:00",
        "asset_count": len(assets),
        "assets": assets,
    }
    approval_path = repository / "approval.json"
    approval_path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8")
    _write_package(package)

    assert main(
        [
            "--preproduction-root",
            str(package),
            "--approval-manifest",
            str(approval_path),
            "--repository-root",
            str(repository),
        ]
    ) == 0

    templates = sorted(
        (package / "approval_templates").rglob("bindings.template.json")
    )
    assert len(templates) == 9
    for path in templates:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["approved_by"] == "human-reviewer"
        assert {item["path"] for item in payload["assets"].values()} == {
            "assets/C01.png",
            "assets/P01.png",
        }
        assert all(item["voice_id"] == "" for item in payload["voices"].values())

    commands = json.loads(
        (package / "bundle_approval_commands.json").read_text(encoding="utf-8")
    )
    assert all(
        item["status"] == "blocked_until_voices_are_filled"
        for item in commands
    )
    report = json.loads(
        (package / "master_asset_approval_report.json").read_text(encoding="utf-8")
    )
    assert report["valid"] is True
    assert report["approved_source_asset_count"] == 2
    assert report["binding_template_count"] == 9
    assert report["filled_asset_binding_count"] == 18
    assert report["blank_voice_identity_count"] == 9
    assert report["external_api_calls"] == 0


def test_sha_change_fails_before_templates_are_modified(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    package = tmp_path / "preproduction"
    repository.mkdir()
    package.mkdir()
    asset = _approval_asset(repository, "C01", "assets/C01.png", 50)
    asset["sha256"] = "0" * 64
    approval = {
        "schema_version": "1.0",
        "approval_id": "fixture-approval",
        "decision": "approved",
        "scope": "master_reference_images_only",
        "approved_by": "human-reviewer",
        "approved_at": "2026-08-05T23:20:00+09:00",
        "asset_count": 1,
        "assets": [asset],
    }
    approval_path = repository / "approval.json"
    approval_path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8")
    _write_package(package)
    before = {
        path: path.read_bytes()
        for path in (package / "approval_templates").rglob("bindings.template.json")
    }

    assert main(
        [
            "--preproduction-root",
            str(package),
            "--approval-manifest",
            str(approval_path),
            "--repository-root",
            str(repository),
        ]
    ) == 2
    assert all(path.read_bytes() == content for path, content in before.items())
