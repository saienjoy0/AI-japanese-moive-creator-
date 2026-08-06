"""Small helpers for ordered Japanese-drama generation segment IDs."""

from __future__ import annotations

import re


_SEGMENT_ID_RE = re.compile(r"^(?P<episode>[A-Za-z]+\d+)-G(?P<number>\d+)$")


class SegmentIdError(ValueError):
    """A segment ID does not match the supported episode-generation format."""


def _parts(segment_id: str) -> tuple[str, int, int]:
    value = segment_id.strip()
    match = _SEGMENT_ID_RE.fullmatch(value)
    if match is None:
        raise SegmentIdError(
            f"unsupported segment ID {segment_id!r}; expected a value like E01-G02"
        )
    digits = match.group("number")
    number = int(digits)
    if number < 1:
        raise SegmentIdError("generation segment numbers must start at 1")
    return match.group("episode"), number, len(digits)


def next_segment_id(segment_id: str) -> str:
    """Return the next generation segment in the same episode."""

    episode, number, width = _parts(segment_id)
    return f"{episode}-G{number + 1:0{width}d}"


def previous_segment_id(segment_id: str) -> str | None:
    """Return the prior generation segment, or None for the first segment."""

    episode, number, width = _parts(segment_id)
    if number == 1:
        return None
    return f"{episode}-G{number - 1:0{width}d}"
