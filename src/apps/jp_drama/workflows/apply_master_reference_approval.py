"""Apply a human-approved master-reference set to generated binding templates.

This workflow performs no provider calls and does not approve voice identities.
It verifies every committed PNG against the human approval manifest, then fills
the image portions of all route-specific ``bindings.template.json`` files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..rendering.approval import ApprovalError, png_dimensions


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2


class MasterReferenceApprovalError(RuntimeError):
    """The human-approved reference set cannot be safely applied."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a human-approved master-image manifest and populate all "
            "preproduction AssetBundle binding templates. No provider calls."
        )
    )
    parser.add_argument("--preproduction-root", required=True)
    parser.add_argument("--approval-manifest", required=True)
    parser.add_argument("--repository-root", default=".")
    parser.add_argument("--report")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MasterReferenceApprovalError(f"cannot load JSON {path}: {exc}") from exc


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_assets(
    manifest: dict[str, Any],
    repository_root: Path,
) -> dict[str, dict[str, Any]]:
    required = {
        "approval_id",
        "decision",
        "scope",
        "approved_by",
        "approved_at",
        "asset_count",
        "assets",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise MasterReferenceApprovalError(
            "approval manifest is missing fields: " + ", ".join(missing)
        )
    if manifest["decision"] != "approved":
        raise MasterReferenceApprovalError("master-reference decision is not approved")
    if manifest["scope"] != "master_reference_images_only":
        raise MasterReferenceApprovalError("approval scope is not master_reference_images_only")
    approver = str(manifest["approved_by"]).strip()
    if not approver:
        raise MasterReferenceApprovalError("approved_by is required")

    raw_assets = manifest["assets"]
    if not isinstance(raw_assets, list) or not raw_assets:
        raise MasterReferenceApprovalError("approval manifest assets must be a non-empty list")
    if manifest["asset_count"] != len(raw_assets):
        raise MasterReferenceApprovalError("asset_count does not match approval assets")

    by_id: dict[str, dict[str, Any]] = {}
    root = repository_root.resolve()
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise MasterReferenceApprovalError("approval asset entry must be an object")
        source_id = str(raw.get("source_asset_id", "")).strip()
        if not source_id or source_id in by_id:
            raise MasterReferenceApprovalError(
                f"approval asset ID is missing or duplicated: {source_id!r}"
            )
        relative = Path(str(raw.get("path", "")))
        if relative.is_absolute():
            raise MasterReferenceApprovalError(
                f"approval asset path must be repository-relative: {source_id}"
            )
        path = (root / relative).resolve()
        if root != path and root not in path.parents:
            raise MasterReferenceApprovalError(
                f"approval asset path escapes repository root: {source_id}"
            )
        if path.suffix.lower() != ".png" or not path.is_file() or path.stat().st_size == 0:
            raise MasterReferenceApprovalError(
                f"approved PNG is missing or invalid: {source_id} -> {relative}"
            )
        expected_sha = str(raw.get("sha256", "")).removeprefix("sha256:")
        actual_sha = _sha256(path)
        if actual_sha != expected_sha:
            raise MasterReferenceApprovalError(
                f"approved PNG SHA-256 changed: {source_id}"
            )
        try:
            width, height = png_dimensions(path)
        except ApprovalError as exc:
            raise MasterReferenceApprovalError(str(exc)) from exc
        if (width, height) != (raw.get("width"), raw.get("height")):
            raise MasterReferenceApprovalError(
                f"approved PNG dimensions changed: {source_id}"
            )
        if raw.get("format") != "PNG":
            raise MasterReferenceApprovalError(
                f"approved image format is not PNG: {source_id}"
            )
        generated_by = str(raw.get("generated_by", "")).strip()
        operation_id = str(raw.get("operation_id", "")).strip()
        if not generated_by or not operation_id:
            raise MasterReferenceApprovalError(
                f"approved image lineage is incomplete: {source_id}"
            )
        by_id[source_id] = {
            **raw,
            "path": relative.as_posix(),
            "sha256": actual_sha,
            "width": width,
            "height": height,
        }
    return by_id


def apply_master_reference_approval(
    *,
    preproduction_root: str | Path,
    approval_manifest: str | Path,
    repository_root: str | Path,
) -> dict[str, Any]:
    package = Path(preproduction_root).resolve()
    approval_path = Path(approval_manifest).resolve()
    repo = Path(repository_root).resolve()
    if not package.is_dir():
        raise MasterReferenceApprovalError(
            f"preproduction root does not exist: {package}"
        )
    approval = _load_json(approval_path)
    if not isinstance(approval, dict):
        raise MasterReferenceApprovalError("approval manifest must be an object")
    assets_by_source = _validated_assets(approval, repo)

    templates = sorted(
        (package / "approval_templates").rglob("bindings.template.json")
    )
    if len(templates) != 9:
        raise MasterReferenceApprovalError(
            f"expected 9 binding templates, found {len(templates)}"
        )

    template_updates: list[tuple[Path, dict[str, Any]]] = []
    binding_count = 0
    used_source_ids: set[str] = set()
    blank_voice_count = 0
    for template_path in templates:
        payload = _load_json(template_path)
        if not isinstance(payload, dict):
            raise MasterReferenceApprovalError(
                f"binding template must be an object: {template_path}"
            )
        template_assets = payload.get("assets")
        template_voices = payload.get("voices")
        if not isinstance(template_assets, dict) or not isinstance(template_voices, dict):
            raise MasterReferenceApprovalError(
                f"binding template has invalid assets or voices: {template_path}"
            )
        payload["approved_by"] = approval["approved_by"]
        for bundle_asset_id, binding in template_assets.items():
            if not isinstance(binding, dict):
                raise MasterReferenceApprovalError(
                    f"invalid asset binding {bundle_asset_id} in {template_path}"
                )
            source_id = str(binding.get("source_asset_id", "")).strip()
            approved = assets_by_source.get(source_id)
            if approved is None:
                raise MasterReferenceApprovalError(
                    f"no human approval exists for {source_id} in {template_path}"
                )
            binding["path"] = approved["path"]
            binding["generated_by"] = approved["generated_by"]
            binding["operation_id"] = approved["operation_id"]
            binding["verified_against_asset_ids"] = []
            binding_count += 1
            used_source_ids.add(source_id)
        blank_voice_count += sum(
            not str(voice.get("voice_id", "")).strip()
            for voice in template_voices.values()
            if isinstance(voice, dict)
        )
        payload["_instructions"] = (
            "Master image paths were filled from a SHA-256-bound human approval. "
            "Assign distinct voice_id values before running approve_asset_bundle."
        )
        template_updates.append((template_path, payload))

    missing_usage = sorted(set(assets_by_source) - used_source_ids)
    if missing_usage:
        raise MasterReferenceApprovalError(
            "approved source assets are absent from all binding templates: "
            + ", ".join(missing_usage)
        )

    command_path = package / "bundle_approval_commands.json"
    commands = _load_json(command_path)
    if not isinstance(commands, list) or len(commands) != 9:
        raise MasterReferenceApprovalError(
            "bundle_approval_commands.json must contain 9 commands"
        )
    command_status = (
        "blocked_until_voices_are_filled"
        if blank_voice_count
        else "ready_for_approval_command"
    )
    for command in commands:
        if not isinstance(command, dict):
            raise MasterReferenceApprovalError("bundle approval command must be an object")
        command["status"] = command_status
        command["master_assets_approved"] = True
        command["master_asset_approval_id"] = approval["approval_id"]

    for template_path, payload in template_updates:
        _atomic_write_json(template_path, payload)
    _atomic_write_json(command_path, commands)

    report = {
        "valid": True,
        "stage": "master_reference_human_approval_application",
        "approval_id": approval["approval_id"],
        "approved_by": approval["approved_by"],
        "approved_at": approval["approved_at"],
        "approved_source_asset_count": len(assets_by_source),
        "binding_template_count": len(templates),
        "filled_asset_binding_count": binding_count,
        "blank_voice_identity_count": blank_voice_count,
        "asset_bundle_command_status": command_status,
        "voice_identity_status": "pending" if blank_voice_count else "ready",
        "external_api_calls": 0,
        "next_action": (
            "assign four distinct voice IDs, then run the nine approve_asset_bundle commands"
            if blank_voice_count
            else "run the nine approve_asset_bundle commands"
        ),
    }
    _atomic_write_json(package / "master_asset_approval_report.json", report)
    return report


def _write_report(
    payload: dict[str, Any],
    path: str | None,
    print_report: bool,
) -> None:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path:
        _atomic_write_json(Path(path).resolve(), payload)
    if print_report:
        print(content, end="")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = apply_master_reference_approval(
            preproduction_root=args.preproduction_root,
            approval_manifest=args.approval_manifest,
            repository_root=args.repository_root,
        )
    except (OSError, ValueError, MasterReferenceApprovalError) as exc:
        payload = {
            "valid": False,
            "stage": "master_reference_human_approval_application",
            "external_api_calls": 0,
            "errors": [str(exc)],
        }
        _write_report(payload, args.report, args.print_report)
        print(f"not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY

    _write_report(report, args.report, args.print_report)
    if not args.print_report:
        print(
            "Master-reference approval applied: VALID\n"
            f"Approved source assets: {report['approved_source_asset_count']}\n"
            f"Templates: {report['binding_template_count']}\n"
            f"Filled bindings: {report['filled_asset_binding_count']}\n"
            f"Blank voice identities: {report['blank_voice_identity_count']}\n"
            "External API calls: 0"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
