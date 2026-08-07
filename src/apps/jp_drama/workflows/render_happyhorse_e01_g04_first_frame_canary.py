"""Compatibility entry point for the E01-G04 hosted workflow.

The permanent routing policy now lives in
``render_happyhorse_segment_auto_route_canary``.  This module remains only so
the already-reviewed G04 owner-only workflow can keep its stable command while
the plan, rather than a hard-coded segment rule, selects first-frame I2V.
"""

from __future__ import annotations

import sys

from . import render_happyhorse_segment_auto_route_canary as auto_route
from . import render_happyhorse_segment_canary as base


class G4FirstFrameError(RuntimeError):
    """The compatibility command was pointed at a segment other than E01-G04."""


def _option_value(arguments: list[str], option: str) -> str | None:
    try:
        index = arguments.index(option)
    except ValueError:
        return None
    if index + 1 >= len(arguments):
        raise G4FirstFrameError(f"{option} requires a value")
    return arguments[index + 1]


def validate_g4_arguments(arguments: list[str]) -> None:
    """Keep the old command sealed to G04; route details come from the plan."""

    segment_id = _option_value(arguments, "--segment-id")
    if segment_id != "E01-G04":
        raise G4FirstFrameError(
            "this compatibility runner is sealed to --segment-id E01-G04"
        )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        validate_g4_arguments(arguments)
        return auto_route.main(arguments)
    except G4FirstFrameError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
