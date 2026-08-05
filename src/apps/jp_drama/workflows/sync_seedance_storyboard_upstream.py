"""Synchronise the pinned upstream Seedance storyboard Skill locally."""

from __future__ import annotations

import argparse

from ..seedance_storyboard import sync_upstream


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--destination",
        default=".local/upstream/Seedance2-Storyboard-Generator",
    )
    args = parser.parse_args(argv)
    manifest = sync_upstream(args.destination)
    print(manifest)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
