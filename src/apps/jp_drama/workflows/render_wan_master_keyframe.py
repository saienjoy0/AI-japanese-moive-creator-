"""Public keyframe-only entry that deliberately ignores video clip duration.

A first frame is a still image and does not consume a Wan video-duration slot.
The video workflow continues to enforce ``provider_clip_seconds`` separately.
"""

from __future__ import annotations

from ..rendering.segment_canary import (
    materialize_generation_segment_canary as _materialize_segment,
)
from . import _render_wan_master_keyframe_impl as _impl


def _materialize_keyframe_segment(prepared, plan, segment_id, *, provider_clip_seconds=None):
    # Preserve every narrative and readiness check except the video-only duration cap.
    return _materialize_segment(
        prepared,
        plan,
        segment_id,
        provider_clip_seconds=None,
    )


_impl.materialize_generation_segment_canary = _materialize_keyframe_segment

build_parser = _impl.build_parser
main = _impl.main

__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
