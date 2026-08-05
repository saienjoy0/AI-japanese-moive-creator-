"""Single production entry for zero-call preflight and exact episode composition.

Paid provider execution is deliberately disabled in this PR. Later provider
dispatch must return hash-bound SegmentArtifact records before composition.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from ..assets import AssetBundleError, assess_asset_readiness, load_bundle
from ..assets.bundle import prepared_content_digest
from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode
from ..production import (
    ProductionComposeError,
    ProductionEpisodeComposer,
    ProductionPreflightReport,
    SegmentArtifactManifest,
)


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_COMPOSE = 5
EXIT_PAID_DISABLED = 6


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Use the single Japanese-drama production entry. Preflight and compose "
            "make zero provider calls. Paid render is fail-closed until the common "
            "provider dispatcher is added."
        )
    )
    parser.add_argument("--prepared-input", required=True, type=Path)
    parser.add_argument("--generation-plan", required=True, type=Path)
    parser.add_argument(
        "--stage",
        choices=("preflight", "compose", "render"),
        default="preflight",
    )
    parser.add_argument("--asset-bundle", type=Path)
    parser.add_argument(
        "--segment-artifacts",
        type=Path,
        help="SegmentArtifactManifest JSON; required for compose",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--target-width", type=int, default=720)
    parser.add_argument("--target-height", type=int, default=1280)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_prepared(path: Path) -> PreparedEpisode:
    return PreparedEpisode.model_validate_json(path.read_text(encoding="utf-8"))


def _load_plan(path: Path) -> GenerationPlanEpisode:
    return GenerationPlanEpisode.model_validate_json(path.read_text(encoding="utf-8"))


def _load_manifest(path: Path) -> SegmentArtifactManifest:
    return SegmentArtifactManifest.model_validate_json(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _write_payload(
    payload: dict[str, object],
    *,
    report: Path | None,
    print_report: bool,
) -> None:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    if report is not None:
        _atomic_write(report, content)
    if print_report:
        print(content, end="")


def _base_contract(
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
) -> tuple[str, list[str]]:
    digest = prepared_content_digest(prepared)
    blockers: list[str] = []
    if plan.source_prepared_episode_digest != digest:
        blockers.append("generation_plan_prepared_episode_digest_mismatch")
    if plan.source_episode_id != prepared.episode_id:
        blockers.append("generation_plan_source_episode_id_mismatch")
    if plan.timeline_fps != prepared.project_draft.fps:
        blockers.append("generation_plan_fps_mismatch")
    if not plan.readiness_report.planning_ready:
        blockers.append("generation_plan_not_planning_ready")
    return digest, blockers


def _preflight(
    prepared: PreparedEpisode,
    plan: GenerationPlanEpisode,
    *,
    asset_bundle_path: Path | None,
) -> ProductionPreflightReport:
    prepared_digest, blockers = _base_contract(prepared, plan)
    warnings = [
        item.message for item in plan.readiness_report.warnings
    ]
    asset_bundle_digest = None
    asset_ready = False

    if not plan.readiness_report.execution_route_ready:
        blockers.append("generation_plan_execution_route_not_ready")
        blockers.extend(
            item.code for item in plan.readiness_report.errors
        )

    if asset_bundle_path is None:
        blockers.append("approved_asset_bundle_missing")
    else:
        bundle = load_bundle(asset_bundle_path)
        asset_bundle_digest = bundle.content_digest
        readiness = assess_asset_readiness(
            bundle,
            prepared,
            plan,
            stage="full_episode",
        )
        asset_ready = readiness.ready
        blockers.extend(item.code for item in readiness.errors)
        warnings.extend(item.message for item in readiness.warnings)

    blockers = sorted(set(blockers))
    warnings = sorted(set(warnings))
    valid = (
        not blockers
        and plan.readiness_report.planning_ready
        and plan.readiness_report.execution_route_ready
        and asset_ready
    )
    return ProductionPreflightReport(
        prepared_episode_digest=prepared_digest,
        generation_plan_digest=plan.content_digest,
        asset_bundle_digest=asset_bundle_digest,
        route_id=plan.provider_route_id,
        segment_count=len(plan.segments),
        target_frame_count=plan.target_frame_count,
        timeline_fps=plan.timeline_fps,
        target_duration_seconds=float(plan.target_duration_seconds),
        planning_ready=plan.readiness_report.planning_ready,
        execution_route_ready=plan.readiness_report.execution_route_ready,
        asset_ready=asset_ready,
        paid_execution_enabled=False,
        external_api_calls=0,
        valid=valid,
        blockers=blockers,
        warnings=warnings,
        next_action=(
            "Create or complete the ApprovedAssetBundle, then rerun preflight."
            if not asset_ready
            else (
                "Resolve GenerationPlan blockers, then rerun preflight."
                if blockers
                else (
                    "Generate or import each provider segment as a SegmentArtifact; "
                    "paid execution remains disabled in this PR."
                )
            )
        ),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prepared = _load_prepared(args.prepared_input)
        plan = _load_plan(args.generation_plan)
        prepared_digest, contract_blockers = _base_contract(prepared, plan)
    except (OSError, ValidationError, json.JSONDecodeError, ValueError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if contract_blockers:
        payload = {
            "valid": False,
            "stage": args.stage,
            "prepared_episode_digest": prepared_digest,
            "generation_plan_digest": plan.content_digest,
            "blockers": contract_blockers,
            "external_api_calls": 0,
        }
        _write_payload(
            payload,
            report=args.report,
            print_report=args.print_report,
        )
        print(
            "not ready: PreparedEpisode and GenerationPlan contracts do not match",
            file=sys.stderr,
        )
        return EXIT_NOT_READY

    if args.stage == "preflight":
        try:
            report = _preflight(
                prepared,
                plan,
                asset_bundle_path=args.asset_bundle,
            )
        except (
            OSError,
            ValidationError,
            json.JSONDecodeError,
            AssetBundleError,
            ValueError,
        ) as exc:
            print(f"input error: {exc}", file=sys.stderr)
            return EXIT_INPUT
        payload = report.model_dump(mode="json", exclude_none=True)
        _write_payload(
            payload,
            report=args.report,
            print_report=args.print_report,
        )
        if not args.print_report:
            print(
                f"Route: {report.route_id}\n"
                f"Segments: {report.segment_count}\n"
                f"Timeline: {report.target_frame_count} frames at "
                f"{report.timeline_fps} fps\n"
                f"Assets ready: {report.asset_ready}\n"
                f"Paid execution enabled: {report.paid_execution_enabled}\n"
                "External API calls: 0"
            )
        return EXIT_OK if report.valid else EXIT_NOT_READY

    if args.stage == "render":
        payload = {
            "valid": False,
            "stage": "render",
            "status": "blocked",
            "prepared_episode_digest": prepared_digest,
            "generation_plan_digest": plan.content_digest,
            "paid_execution_gate": "common_provider_dispatcher_not_implemented",
            "message": (
                "Paid full-episode execution is disabled. Use the existing approval-gated "
                "single-segment Wan or H3 Canary, or import an operator-generated segment. "
                "Every result must become a SegmentArtifact before compose."
            ),
            "external_api_calls": 0,
        }
        _write_payload(
            payload,
            report=args.report,
            print_report=args.print_report,
        )
        print(
            "provider error: paid production render is fail-closed in PR22; "
            "no provider request was submitted",
            file=sys.stderr,
        )
        return EXIT_PAID_DISABLED

    if args.segment_artifacts is None or args.output is None or args.work_dir is None:
        print(
            "input error: compose requires --segment-artifacts, --output, and --work-dir",
            file=sys.stderr,
        )
        return EXIT_INPUT
    try:
        manifest = _load_manifest(args.segment_artifacts)
        composer = ProductionEpisodeComposer(
            plan,
            manifest,
            output_file=args.output,
            work_dir=args.work_dir,
            target_width=args.target_width,
            target_height=args.target_height,
        )
        compose_report = composer.compose(reset=args.reset)
    except (
        OSError,
        ValidationError,
        json.JSONDecodeError,
        ValueError,
        ProductionComposeError,
    ) as exc:
        payload = {
            "valid": False,
            "stage": "compose",
            "generation_plan_digest": plan.content_digest,
            "error": str(exc),
            "external_api_calls": 0,
        }
        _write_payload(
            payload,
            report=args.report,
            print_report=args.print_report,
        )
        print(f"compose error: {exc}", file=sys.stderr)
        return EXIT_COMPOSE

    payload = compose_report.model_dump(mode="json", exclude_none=True)
    payload["stage"] = "compose"
    _write_payload(
        payload,
        report=args.report,
        print_report=args.print_report,
    )
    if not args.print_report:
        print(
            f"Output: {compose_report.output_file}\n"
            f"Segments: {len(compose_report.segment_order)}\n"
            f"Frames: {compose_report.actual_frame_count}/"
            f"{compose_report.expected_frame_count}\n"
            f"Duration: {compose_report.duration_seconds:.3f}s\n"
            "External API calls: 0\n"
            "Status: VALID"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
