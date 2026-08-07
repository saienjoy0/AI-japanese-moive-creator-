"""Select HappyHorse I2V or R2V from the generation-plan continuity boundary.

This is the permanent segment routing boundary:

* the first segment in a plan, or a segment whose continuity group differs from
  the immediately previous segment, starts from its own approved first frame
  through HappyHorse I2V;
* a segment in the same continuity group uses HappyHorse R2V and must consume
  the immediately previous segment's SHA-bound end frame;
* every successful render still extracts its end frame through the existing
  continuity runner, so the next same-group segment can be routed automatically.

The router itself never calls a provider. It only validates the current plan,
materializes an unambiguous input mode, and delegates to the existing directed
continuity runner with the normal ledger and paid-call gates intact.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from ..generation.models import GenerationPlanEpisode, GenerationSegment
from . import render_happyhorse_segment_canary as base
from . import render_happyhorse_segment_continuity_canary as continuity
from . import render_happyhorse_segment_directed_continuity_canary as directed


InputMode = Literal["first_frame", "references"]


class HappyHorseAutoRouteError(RuntimeError):
    """The plan cannot produce one unambiguous HappyHorse input route."""


@dataclass(frozen=True)
class HappyHorseAutoRouteDecision:
    segment_id: str
    input_mode: InputMode
    reason: Literal["episode_start", "continuity_group_boundary", "same_continuity_group"]
    target_continuity_group_id: str
    previous_segment_id: str | None = None
    previous_continuity_group_id: str | None = None

    @property
    def continuity_required(self) -> bool:
        return self.input_mode == "references"


def _option_indices(arguments: list[str], option: str) -> list[int]:
    return [index for index, value in enumerate(arguments) if value == option]


def _option_value(
    arguments: list[str],
    option: str,
    default: str | None = None,
) -> str | None:
    indices = _option_indices(arguments, option)
    if not indices:
        return default
    if len(indices) != 1:
        raise HappyHorseAutoRouteError(f"{option} must be supplied at most once")
    index = indices[0]
    if index + 1 >= len(arguments):
        raise HappyHorseAutoRouteError(f"{option} requires a value")
    return arguments[index + 1]


def _set_option(arguments: list[str], option: str, value: str) -> list[str]:
    updated = list(arguments)
    indices = _option_indices(updated, option)
    if len(indices) > 1:
        raise HappyHorseAutoRouteError(f"{option} must be supplied at most once")
    if not indices:
        return [*updated, option, value]
    index = indices[0]
    if index + 1 >= len(updated):
        raise HappyHorseAutoRouteError(f"{option} requires a value")
    updated[index + 1] = value
    return updated


def _find_segment(
    plan: GenerationPlanEpisode,
    segment_id: str,
) -> GenerationSegment:
    matches = [segment for segment in plan.segments if segment.segment_id == segment_id]
    if len(matches) != 1:
        raise HappyHorseAutoRouteError(
            f"unknown or duplicate generation segment: {segment_id}"
        )
    return matches[0]


def _previous_segment(
    plan: GenerationPlanEpisode,
    target: GenerationSegment,
) -> GenerationSegment | None:
    if target.order == 1:
        return None
    matches = [segment for segment in plan.segments if segment.order == target.order - 1]
    if len(matches) != 1:
        raise HappyHorseAutoRouteError(
            f"segment order before {target.segment_id} is missing or ambiguous"
        )
    return matches[0]


def decide_happyhorse_auto_route(
    plan: GenerationPlanEpisode,
    *,
    segment_id: str,
) -> HappyHorseAutoRouteDecision:
    """Choose I2V at scene starts and continuity R2V inside one group."""

    target = _find_segment(plan, segment_id)
    previous = _previous_segment(plan, target)
    if previous is None:
        return HappyHorseAutoRouteDecision(
            segment_id=target.segment_id,
            input_mode="first_frame",
            reason="episode_start",
            target_continuity_group_id=target.continuity_group_id,
        )
    if previous.continuity_group_id != target.continuity_group_id:
        return HappyHorseAutoRouteDecision(
            segment_id=target.segment_id,
            input_mode="first_frame",
            reason="continuity_group_boundary",
            target_continuity_group_id=target.continuity_group_id,
            previous_segment_id=previous.segment_id,
            previous_continuity_group_id=previous.continuity_group_id,
        )
    return HappyHorseAutoRouteDecision(
        segment_id=target.segment_id,
        input_mode="references",
        reason="same_continuity_group",
        target_continuity_group_id=target.continuity_group_id,
        previous_segment_id=previous.segment_id,
        previous_continuity_group_id=previous.continuity_group_id,
    )


def _default_continuity_dir(arguments: list[str]) -> Path:
    explicit = _option_value(arguments, "--continuity-dir")
    if explicit:
        return Path(explicit).resolve()
    output = _option_value(arguments, "--output")
    if not output:
        raise HappyHorseAutoRouteError(
            "--output or --continuity-dir is required to resolve continuity"
        )
    return Path(output).resolve().parent / "continuity"


def materialize_auto_route_arguments(
    arguments: list[str],
    decision: HappyHorseAutoRouteDecision,
) -> list[str]:
    """Bind the selected route and exact prior continuity pair before submission."""

    explicit_mode = _option_value(arguments, "--input-mode")
    if explicit_mode is not None and explicit_mode != decision.input_mode:
        raise HappyHorseAutoRouteError(
            f"plan selects --input-mode {decision.input_mode} for {decision.segment_id}, "
            f"not {explicit_mode}"
        )
    routed = _set_option(arguments, "--input-mode", decision.input_mode)

    frame_supplied = "--continuity-frame" in routed
    metadata_supplied = "--continuity-frame-metadata" in routed
    if frame_supplied != metadata_supplied:
        raise HappyHorseAutoRouteError(
            "--continuity-frame and --continuity-frame-metadata must be supplied together"
        )

    if not decision.continuity_required:
        if frame_supplied:
            raise HappyHorseAutoRouteError(
                f"{decision.segment_id} starts a new continuity group and must not "
                "consume the previous segment's end frame"
            )
        return routed

    if decision.previous_segment_id is None:
        raise HappyHorseAutoRouteError(
            "same-group R2V route has no previous segment"
        )
    if frame_supplied:
        return routed

    continuity_dir = _default_continuity_dir(routed)
    resolved = continuity.resolve_previous_continuity(
        continuity_dir,
        target_segment_id=decision.segment_id,
    )
    if resolved is None:
        raise HappyHorseAutoRouteError(
            f"same-group segment {decision.segment_id} requires the verified end frame "
            f"from {decision.previous_segment_id}, but no continuity pair was found in "
            f"{continuity_dir}"
        )
    routed = _set_option(routed, "--continuity-frame", str(resolved[0]))
    routed = _set_option(
        routed,
        "--continuity-frame-metadata",
        str(resolved[1]),
    )
    return routed


@contextmanager
def install_auto_route_report(
    decision: HappyHorseAutoRouteDecision,
) -> Iterator[None]:
    """Record the plan-derived route in preflight and render evidence."""

    original_report = base._base_report

    def routed_report(**kwargs):
        payload = original_report(**kwargs)
        payload["auto_route"] = {
            "segment_id": decision.segment_id,
            "input_mode": decision.input_mode,
            "reason": decision.reason,
            "continuity_required": decision.continuity_required,
            "target_continuity_group_id": decision.target_continuity_group_id,
            "previous_segment_id": decision.previous_segment_id,
            "previous_continuity_group_id": decision.previous_continuity_group_id,
        }
        return payload

    base._base_report = routed_report
    try:
        yield
    finally:
        base._base_report = original_report


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        plan_path = _option_value(arguments, "--generation-plan")
        segment_id = _option_value(arguments, "--segment-id")
        if not plan_path:
            raise HappyHorseAutoRouteError("--generation-plan is required")
        if not segment_id:
            raise HappyHorseAutoRouteError("--segment-id is required")
        plan = GenerationPlanEpisode.model_validate_json(
            Path(plan_path).read_text(encoding="utf-8")
        )
        decision = decide_happyhorse_auto_route(plan, segment_id=segment_id)
        routed_arguments = materialize_auto_route_arguments(arguments, decision)
        with install_auto_route_report(decision):
            return directed.main(routed_arguments)
    except (OSError, ValueError, HappyHorseAutoRouteError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return base.EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
