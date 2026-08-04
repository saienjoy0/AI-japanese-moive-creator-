"""Convert a normal Japanese TXT/MD/pasted script into EpisodePackage JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..domain import RightsStatus
from ..ingestion import (
    DashScopeStructuredScriptLLM,
    FixtureStructuredScriptLLM,
    ScriptIngestionError,
    ScriptLLMError,
    ingest_script,
    write_failure_report,
    write_ingestion_artifacts,
)


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_LLM = 2
EXIT_VALIDATION = 3
EXIT_OUTPUT = 4

ROOT = Path(__file__).resolve().parents[4]
DEFAULT_FIXTURE = (
    ROOT
    / "examples"
    / "jp_drama"
    / "script_ingestion"
    / "structured_script_fixture.json"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize a Japanese script, structure it with an LLM, validate it, "
            "and compile a deterministic EpisodePackage."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="UTF-8 TXT or MD script path")
    source.add_argument("--text", help="Japanese script pasted directly on the command line")
    parser.add_argument(
        "--output-dir",
        default="output/jp_drama/ingestion",
        help="Directory for normalized script, structured draft, package, and reports",
    )
    parser.add_argument(
        "--llm-provider",
        choices=["fixture", "dashscope"],
        default="fixture",
    )
    parser.add_argument(
        "--fixture",
        help=f"Fixture JSON path (default: {DEFAULT_FIXTURE})",
    )
    parser.add_argument("--model", default="qwen-plus")
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument(
        "--rights-status",
        choices=[item.value for item in RightsStatus],
        default=RightsStatus.UNKNOWN.value,
    )
    parser.add_argument(
        "--print-report",
        action="store_true",
        help="Print ingestion_report.json to stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir)

    try:
        if args.input:
            text = Path(args.input).read_text(encoding="utf-8")
        else:
            text = args.text
    except OSError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if args.llm_provider == "fixture":
        fixture_path = Path(args.fixture) if args.fixture else DEFAULT_FIXTURE
        try:
            llm = FixtureStructuredScriptLLM(fixture_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"fixture error: {exc}", file=sys.stderr)
            return EXIT_INPUT
    else:
        llm = DashScopeStructuredScriptLLM(
            model=args.model,
            api_key_env=args.api_key_env,
        )

    try:
        result = ingest_script(
            text,
            llm=llm,
            rights_status=RightsStatus(args.rights_status),
        )
    except ScriptLLMError as exc:
        print(f"LLM error: {exc}", file=sys.stderr)
        return EXIT_LLM
    except ScriptIngestionError as exc:
        write_failure_report(exc.report, output_dir)
        print(f"validation error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    except (TypeError, ValueError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    try:
        paths = write_ingestion_artifacts(result, output_dir)
    except OSError as exc:
        print(f"output error: {exc}", file=sys.stderr)
        return EXIT_OUTPUT

    if args.print_report:
        print(result.report.model_dump_json(indent=2))
    else:
        print(
            f"Normalized script: {paths['normalized_script']}\n"
            f"Structured script: {paths['structured_script']}\n"
            f"EpisodePackage: {paths['episode_package']}\n"
            f"Report: {paths['ingestion_report']}\n"
            f"LLM provider: {result.report.llm_provider}\n"
            f"Attempts: {result.report.attempts}\n"
            f"Unresolved items: {len(result.structured_script.unresolved_items)}\n"
            f"Valid: {'YES' if result.report.valid else 'NO'}"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
