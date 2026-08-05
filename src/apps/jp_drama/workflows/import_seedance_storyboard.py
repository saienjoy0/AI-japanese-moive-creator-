"""Import native Seedance2 Storyboard Generator Markdown artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..seedance_storyboard import (
    load_project_directory,
    parse_project,
    write_import_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Parse a Seedance2 Storyboard Generator project without rewriting its "
            "creative output."
        )
    )
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = load_project_directory(args.project_dir)
    package = parse_project(source)
    outputs = write_import_artifacts(package, args.output_dir)
    print(f"project={package.project_title}")
    print(f"assets={len(package.assets)}")
    print(f"episodes={len(package.episodes)}")
    print(f"digest={package.content_digest}")
    for output in outputs:
        print(f"wrote={Path(output)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
