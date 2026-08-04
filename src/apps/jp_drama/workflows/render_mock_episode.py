"""Render a PreparedEpisode end to end using only deterministic mock assets."""

from __future__ import annotations

import argparse
import json
import shutil
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
from ..rendering import (
    RenderExecutionError,
    RenderGraphRunner,
    RenderStateConflictError,
    RenderTaskFailedError,
    RenderValidationError,
)


EXIT_OK = 0
EXIT_INPUT = 1
EXIT_NOT_READY = 2
EXIT_PERSISTENCE = 3
EXIT_RENDER = 4
EXIT_VALIDATION = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Persist a PreparedEpisode as a LumenX project, execute its RenderGraph "
            "with local mock tasks, and write one vertical MP4."
        )
    )
    parser.add_argument("--input", required=True, help="PreparedEpisode JSON")
    parser.add_argument("--output", required=True, help="Final vertical MP4")
    parser.add_argument(
        "--work-dir",
        help="Restart-safe intermediate directory (derived from --output by default)",
    )
    parser.add_argument(
        "--projects-file",
        help="LumenX projects file (defaults inside the work directory)",
    )
    parser.add_argument(
        "--index-file",
        help="Japanese-drama persistence index (defaults inside the work directory)",
    )
    parser.add_argument(
        "--report",
        help="Optional copy of the final validation report",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete prior intermediate state and render from the beginning",
    )
    parser.add_argument(
        "--print-report",
        action="store_true",
        help="Print the complete validation report JSON",
    )
    return parser


def _default_work_dir(output: Path) -> Path:
    return output.parent / f".{output.stem}_work"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = Path(args.input)
    output_path = Path(args.output)
    work_dir = Path(args.work_dir) if args.work_dir else _default_work_dir(output_path)
    projects_file = Path(args.projects_file) if args.projects_file else work_dir / "lumenx" / "projects.json"
    index_file = Path(args.index_file) if args.index_file else work_dir / "lumenx" / "persistence_index.json"

    try:
        prepared = PreparedEpisode.model_validate_json(input_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if not prepared.readiness_report.generation_ready:
        print("not ready: PreparedEpisode generation_ready is false", file=sys.stderr)
        return EXIT_NOT_READY

    if args.reset:
        if work_dir.exists():
            shutil.rmtree(work_dir)
        if output_path.exists():
            output_path.unlink()

    store = LumenXProjectStore(
        projects_file=projects_file,
        index_file=index_file,
    )
    try:
        persistence = store.save(prepared)
    except PersistenceNotReadyError as exc:
        print(f"not ready: {exc}", file=sys.stderr)
        return EXIT_NOT_READY
    except (
        PersistenceConflictError,
        PersistenceVerificationError,
        PersistenceError,
    ) as exc:
        print(f"persistence error: {exc}", file=sys.stderr)
        return EXIT_PERSISTENCE
    except Exception as exc:
        print(f"unexpected persistence error: {exc}", file=sys.stderr)
        return EXIT_PERSISTENCE

    runner = RenderGraphRunner(
        prepared,
        output_file=output_path,
        work_dir=work_dir,
        persistence_status=persistence.status,
    )
    try:
        report = runner.run()
    except RenderValidationError as exc:
        print(f"validation error: {exc}", file=sys.stderr)
        return EXIT_VALIDATION
    except (RenderTaskFailedError, RenderStateConflictError, RenderExecutionError) as exc:
        print(f"render error: {exc}", file=sys.stderr)
        return EXIT_RENDER
    except Exception as exc:
        print(f"unexpected render error: {exc}", file=sys.stderr)
        return EXIT_RENDER

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report.to_canonical_json(), encoding="utf-8")

    if args.print_report:
        print(report.to_canonical_json(), end="")
    else:
        print(
            f"Project: {prepared.project_draft.project_id}\n"
            f"Persistence: {persistence.status}\n"
            f"Output: {output_path}\n"
            f"Duration: {report.duration_seconds:.3f}s\n"
            f"Frame: {report.width}x{report.height} ({report.aspect_ratio})\n"
            f"Audio tracks: {report.audio_streams}\n"
            f"Subtitles: {report.subtitle_artifacts}\n"
            f"External API calls: {report.external_api_calls}\n"
            f"Valid: {'YES' if report.valid else 'NO'}"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
