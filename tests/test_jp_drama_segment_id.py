from __future__ import annotations

import pytest

from src.apps.jp_drama.workflows.segment_id import (
    SegmentIdError,
    next_segment_id,
    previous_segment_id,
)


def test_orders_generation_segments_with_zero_padding() -> None:
    assert next_segment_id("E01-G01") == "E01-G02"
    assert next_segment_id("E01-G09") == "E01-G10"
    assert previous_segment_id("E01-G02") == "E01-G01"
    assert previous_segment_id("E01-G01") is None


def test_rejects_unsupported_segment_ids() -> None:
    for value in ("", "G01", "E01-01", "E01-G00", "E01/G01"):
        with pytest.raises(SegmentIdError):
            next_segment_id(value)
