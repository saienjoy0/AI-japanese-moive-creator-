from __future__ import annotations

import json
from pathlib import Path

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.preparation import compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.workflows.render_episode import main as legacy_render_main


ROOT = Path(__file__).resolve().parents[1]
EPISODE_PATH = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG_PATH = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
PROVIDER_PATH = ROOT / "examples" / "jp_drama" / "dashscope_live_providers.json"


def test_legacy_entry_blocks_even_with_large_explicit_call_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = json.loads(EPISODE_PATH.read_text(encoding="utf-8"))
    package = EpisodePackage.model_validate(payload)
    prepared = compile_episode(
        package,
        catalog=load_model_catalog(CATALOG_PATH),
        strict=True,
        source_payload=payload,
    )
    prepared_path = tmp_path / "prepared.json"
    output = tmp_path / "episode.mp4"
    report = tmp_path / "blocked.json"
    prepared_path.write_text(prepared.to_canonical_json() + "\n", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "must-not-be-used")
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", "must-not-be-used")

    result = legacy_render_main(
        [
            "--input",
            str(prepared_path),
            "--output",
            str(output),
            "--providers",
            str(PROVIDER_PATH),
            "--max-api-calls",
            "999999",
            "--report",
            str(report),
        ]
    )

    assert result == 6
    assert output.exists() is False
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["paid_execution_gate"] == "legacy_full_episode_entry_disabled"
    assert payload["external_api_calls"] == 0
