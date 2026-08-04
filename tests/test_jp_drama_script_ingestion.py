from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.ingestion import (
    CompilationOptions,
    FixtureStructuredScriptLLM,
    ScriptIngestionError,
    StructuredScriptDraft,
    compile_structured_script,
    ingest_script,
    normalize_script_text,
    write_ingestion_artifacts,
)
from src.apps.jp_drama.preparation import compile_episode
from src.apps.jp_drama.preparation.compiler import load_model_catalog
from src.apps.jp_drama.workflows.ingest_script import EXIT_OK, main


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "examples"
    / "jp_drama"
    / "script_ingestion"
    / "structured_script_fixture.json"
)
SCRIPT = (
    ROOT
    / "examples"
    / "jp_drama"
    / "script_ingestion"
    / "sample_script.md"
)
CATALOG = ROOT / "examples" / "jp_drama" / "model_capabilities.json"
FIXED_TIME = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


class SequenceLLM:
    provider_id = "sequence-fixture"
    external = False

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.calls = 0
        self.repair_errors: list[list[dict[str, Any]] | None] = []

    def generate(
        self,
        normalized_script: str,
        *,
        previous_payload: dict[str, Any] | None = None,
        validation_errors: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.repair_errors.append(validation_errors)
        index = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return json.loads(json.dumps(self.payloads[index], ensure_ascii=False))


def _fixture_payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_normalize_script_text_handles_bom_newlines_tabs_and_blank_runs() -> None:
    value = "\ufeff題名\r\n\t台詞\r\n\r\n\r\n\r\n終わり  \r\n"
    normalized = normalize_script_text(value)
    assert normalized == "題名\n    台詞\n\n\n終わり\n"


def test_fixture_ingestion_creates_a_valid_episode_package() -> None:
    result = ingest_script(
        SCRIPT.read_text(encoding="utf-8"),
        llm=FixtureStructuredScriptLLM(FIXTURE),
        created_at=FIXED_TIME,
    )

    assert result.report.valid is True
    assert result.report.attempts == 1
    assert result.report.external_api_calls == 0
    assert len(result.report.warnings) == 1
    assert len(result.episode_package.shot_plan.shots) == 3
    assert result.episode_package.shot_plan.total_duration_seconds == 45
    assert all(
        estimate.provider == "provider-a"
        for estimate in result.episode_package.cost_plan.shot_estimates
    )

    restored = EpisodePackage.model_validate_json(
        result.episode_package.to_canonical_json()
    )
    assert restored.package_id == result.episode_package.package_id

    prepared = compile_episode(
        restored,
        catalog=load_model_catalog(CATALOG),
        strict=True,
    )
    assert prepared.readiness_report.generation_ready is True


def test_compiler_is_deterministic_for_same_inputs_and_options() -> None:
    draft = StructuredScriptDraft.model_validate(_fixture_payload())
    normalized = normalize_script_text(SCRIPT.read_text(encoding="utf-8"))
    options = CompilationOptions(created_at=FIXED_TIME)

    first = compile_structured_script(
        draft,
        normalized_script=normalized,
        options=options,
    )
    second = compile_structured_script(
        draft,
        normalized_script=normalized,
        options=options,
    )
    assert first.to_canonical_json() == second.to_canonical_json()


def test_ingestion_repairs_once_using_validation_errors() -> None:
    invalid = _fixture_payload()
    invalid["beats"][1]["scene_id"] = "scene_missing"
    llm = SequenceLLM([invalid, _fixture_payload()])

    result = ingest_script(
        SCRIPT.read_text(encoding="utf-8"),
        llm=llm,
        created_at=FIXED_TIME,
    )

    assert llm.calls == 2
    assert result.report.attempts == 2
    assert result.report.repaired is True
    assert llm.repair_errors[1]
    assert any(
        "scene_missing" in item["message"]
        for item in llm.repair_errors[1] or []
    )


def test_ingestion_stops_after_one_failed_repair() -> None:
    invalid = _fixture_payload()
    invalid["beats"] = invalid["beats"][:2]
    llm = SequenceLLM([invalid, invalid, _fixture_payload()])

    with pytest.raises(ScriptIngestionError) as caught:
        ingest_script(
            SCRIPT.read_text(encoding="utf-8"),
            llm=llm,
            created_at=FIXED_TIME,
        )

    assert llm.calls == 2
    assert caught.value.report.valid is False
    assert caught.value.report.attempts == 2
    assert caught.value.report.errors


def test_artifact_writer_creates_all_five_outputs(tmp_path: Path) -> None:
    result = ingest_script(
        SCRIPT.read_text(encoding="utf-8"),
        llm=FixtureStructuredScriptLLM(FIXTURE),
        created_at=FIXED_TIME,
    )
    paths = write_ingestion_artifacts(result, tmp_path)

    assert set(paths) == {
        "normalized_script",
        "structured_script",
        "episode_package",
        "ingestion_report",
        "unresolved_items",
    }
    assert all(path.is_file() for path in paths.values())
    assert EpisodePackage.model_validate_json(
        paths["episode_package"].read_text(encoding="utf-8")
    )
    report = json.loads(paths["ingestion_report"].read_text(encoding="utf-8"))
    assert report["valid"] is True
    unresolved = json.loads(
        paths["unresolved_items"].read_text(encoding="utf-8")
    )
    assert unresolved == ["美緒の正確な年齢は台本に明記されていない"]


def test_cli_accepts_normal_txt_or_md_and_emits_episode_package(
    tmp_path: Path,
) -> None:
    output = tmp_path / "ingestion"
    code = main(
        [
            "--input",
            str(SCRIPT),
            "--output-dir",
            str(output),
            "--llm-provider",
            "fixture",
            "--fixture",
            str(FIXTURE),
            "--rights-status",
            "unknown",
        ]
    )
    assert code == EXIT_OK
    assert (output / "normalized_script.txt").is_file()
    assert (output / "structured_script.json").is_file()
    assert (output / "episode_package.json").is_file()
    assert (output / "ingestion_report.json").is_file()
    assert (output / "unresolved_items.json").is_file()
