"""CLI for deterministic, no-cost preparation of a Japanese short drama."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..domain import EpisodePackage
from ..preparation.compiler import compile_episode, load_model_catalog
from ..preparation.readiness import render_summary


OUTPUT_FILENAMES = (
    "prepared_episode.json",
    "readiness_report.json",
    "summary.txt",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile EpisodePackage into an offline LumenX production draft."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-summary", action="store_true")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--fail-on-warning", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        package = EpisodePackage.model_validate(payload)
        catalog = load_model_catalog(args.catalog)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 1

    effective_strict = args.strict or args.fail_on_warning
    prepared = compile_episode(
        package,
        catalog=catalog,
        strict=effective_strict,
        source_payload=payload,
    )
    summary = render_summary(prepared.readiness_report)

    try:
        _write_outputs(args.output, prepared, summary, overwrite=args.overwrite)
    except OSError as exc:
        print(f"output error: {exc}", file=sys.stderr)
        return 5

    if args.print_summary:
        print(summary, end="")

    report = prepared.readiness_report
    if any(issue.code == "budget_exceeded" for issue in report.errors):
        return 2
    if any(
        issue.code
        in {
            "model_not_found",
            "model_capability_missing",
            "fallback_capability_missing",
            "fallback_cost_missing",
        }
        for issue in report.errors
    ):
        return 3
    if any(
        issue.code
        in {
            "mapping_incomplete",
            "render_intents_unresolved",
            "render_graph_invalid",
            "cost_strategy_mismatch",
        }
        for issue in report.errors
    ):
        return 4
    if report.errors or (args.fail_on_warning and report.warnings) or not report.generation_ready:
        return 1
    return 0


def _write_outputs(
    output_dir: Path,
    prepared: object,
    summary: str,
    *,
    overwrite: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    target_paths = [output_dir / filename for filename in OUTPUT_FILENAMES]
    if not overwrite and any(path.exists() for path in target_paths):
        raise OSError("output already exists; pass --overwrite to replace it")

    prepared_json = prepared.to_canonical_json(indent=2) + "\n"
    report_json = json.dumps(
        prepared.readiness_report.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    (output_dir / "prepared_episode.json").write_text(prepared_json, encoding="utf-8")
    (output_dir / "readiness_report.json").write_text(report_json, encoding="utf-8")
    (output_dir / "summary.txt").write_text(summary, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
