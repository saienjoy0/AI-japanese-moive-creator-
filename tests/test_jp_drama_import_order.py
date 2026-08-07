from __future__ import annotations

import subprocess
import sys


def _run_imports(code: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_segment_canary_imports_before_minimax_h3() -> None:
    _run_imports(
        "import src.apps.jp_drama.rendering.segment_canary; "
        "import src.apps.jp_drama.rendering.minimax_h3_canary"
    )


def test_minimax_h3_imports_before_segment_canary() -> None:
    _run_imports(
        "import src.apps.jp_drama.rendering.minimax_h3_canary; "
        "import src.apps.jp_drama.rendering.segment_canary"
    )
