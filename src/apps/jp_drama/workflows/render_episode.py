"""Legacy PreparedEpisode renderer retained only for zero-call compatibility preflight.

Paid full-episode execution was intentionally removed from this entry because it
could bypass GenerationPlan, ApprovedAssetBundle, ExecutionBudget, and the new
provider-neutral SegmentArtifact contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..preparation.models import PreparedEpisode
from ..rendering import LiveProviderConfig, LiveTaskExecutor, ProviderConfigurationError


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_PERSISTENCE = 3
EXIT_RENDER = 4
EXIT_VALIDATION = 5
EXIT_PROVIDER = 6


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Legacy zero-call PreparedEpisode preflight. Paid episode rendering is "
            "disabled; use run_production_episode with a GenerationPlan and "
            "ApprovedAssetBundle."
        )
    )
    parser.add_argument("--input", required=True, help="PreparedEpisode JSON")
    parser.add_argument("--output", required=True, help="Former final MP4 path")
    parser.add_argument("--providers", required=True, help="Live provider configuration JSON")
    parser.add_argument("--work-dir")
    parser.add_argument("--projects-file")
    parser.add_argument("--index-file")
    parser.add_argument("--report")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Validate the legacy PreparedEpisode and provider configuration without API calls",
    )
    parser.add_argument(
        "--max-api-calls",
        type=int,
        default=0,
        help="Accepted for CLI compatibility; paid execution is always blocked",
    )
    parser.add_argument("--print-report", action="store_true")
    return parser


def _load_prepared(path: Path) -> PreparedEpisode:
    return PreparedEpisode.model_validate_json(path.read_text(encoding="utf-8"))


def _preflight_report(
    prepared: PreparedEpisode,
    config: LiveProviderConfig,
) -> dict[str, object]:
    required = config.dashscope.required_environment()
    missing = config.dashscope.missing_environment()
    return {
        "valid": prepared.readiness_report.generation_ready,
        "legacy_entry": True,
        "paid_execution_enabled": False,
        "project_id": prepared.project_draft.project_id,
        "source_digest": prepared.source_digest,
        "execution_profile": config.execution_profile,
        "provider_manifest": config.provider_manifest,
        "required_environment": required,
        "missing_environment": missing,
        "credentials_present": not missing,
        "estimated_external_api_calls": LiveTaskExecutor.estimate_api_calls(prepared),
        "shot_count": len(prepared.storyboard_frame_drafts),
        "target_duration_seconds": prepared.project_draft.target_duration_seconds,
        "external_api_calls": 0,
        "next_action": (
            "Compile a GenerationPlan and ApprovedAssetBundle, then use "
            "python -m src.apps.jp_drama.workflows.run_production_episode."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prepared = _load_prepared(Path(args.input))
        config = LiveProviderConfig.load(args.providers)
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except ProviderConfigurationError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER

    if not prepared.readiness_report.generation_ready:
        print("not ready: PreparedEpisode generation_ready is false", file=sys.stderr)
        return EXIT_NOT_READY

    if args.preflight:
        report = _preflight_report(prepared, config)
        content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.report:
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(content, encoding="utf-8")
        if args.print_report:
            print(content, end="")
        else:
            print(
                f"Project: {prepared.project_draft.project_id}\n"
                f"Provider: {config.dashscope.provider}\n"
                f"Estimated API calls: {report['estimated_external_api_calls']}\n"
                "Paid execution enabled: NO\n"
                "External API calls: 0\n"
                "Preflight: VALID"
            )
        return EXIT_OK

    blocked = {
        "valid": False,
        "legacy_entry": True,
        "status": "blocked",
        "paid_execution_gate": "legacy_full_episode_entry_disabled",
        "project_id": prepared.project_draft.project_id,
        "source_digest": prepared.source_digest,
        "external_api_calls": 0,
        "next_action": (
            "Use run_production_episode with --prepared-input, --generation-plan, "
            "and --asset-bundle. Paid provider dispatch remains fail-closed in PR22."
        ),
    }
    content = json.dumps(blocked, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(content, encoding="utf-8")
    if args.print_report:
        print(content, end="")
    print(
        "provider error: legacy paid full-episode rendering is disabled; "
        "no provider request was submitted",
        file=sys.stderr,
    )
    return EXIT_PROVIDER


if __name__ == "__main__":
    raise SystemExit(main())
