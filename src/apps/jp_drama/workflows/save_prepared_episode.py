"""CLI for saving a PreparedEpisode into LumenX project storage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..persistence import (
    LumenXProjectStore,
    PersistenceConflictError,
    PersistenceError,
    PersistenceNotReadyError,
    PersistenceVerificationError,
)
from ..preparation.models import PreparedEpisode


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_CONFLICT = 3
EXIT_VERIFICATION = 4
EXIT_STORAGE = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Persist PreparedEpisode as a provider-free LumenX project."
    )
    parser.add_argument("--input", required=True, help="PreparedEpisode JSON")
    parser.add_argument(
        "--projects-file",
        default="output/projects.json",
        help="Existing LumenX projects.json destination",
    )
    parser.add_argument(
        "--index-file",
        default="output/jp_drama/persistence_index.json",
        help="Japanese-drama persistence ownership index",
    )
    parser.add_argument(
        "--report",
        help="Optional path for PersistenceResult JSON",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate conversion and conflict rules without modifying storage",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing conflicting managed project",
    )
    parser.add_argument(
        "--print-report",
        action="store_true",
        help="Print the complete result JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prepared = PreparedEpisode.model_validate_json(
            Path(args.input).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    store = LumenXProjectStore(
        projects_file=args.projects_file,
        index_file=args.index_file,
    )
    try:
        result = store.save(
            prepared,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
    except PersistenceNotReadyError as exc:
        print(f"not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY
    except PersistenceConflictError as exc:
        print(f"conflict: {exc}", file=sys.stderr)
        return EXIT_CONFLICT
    except PersistenceVerificationError as exc:
        print(f"verification error: {exc}", file=sys.stderr)
        return EXIT_VERIFICATION
    except PersistenceError as exc:
        print(f"storage error: {exc}", file=sys.stderr)
        return EXIT_STORAGE
    except Exception as exc:
        print(f"unexpected persistence error: {exc}", file=sys.stderr)
        return EXIT_STORAGE

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(result.to_canonical_json(), encoding="utf-8")

    if args.print_report:
        print(result.to_canonical_json(), end="")
    else:
        print(
            f"Project: {result.project_id}\n"
            f"Status: {result.status}\n"
            f"Verified: {'YES' if result.verified else 'NO'}\n"
            f"External API calls: {result.external_api_calls}\n"
            f"Files written: {len(result.files_written)}"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
