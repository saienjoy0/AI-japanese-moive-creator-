"""Script normalization, bounded LLM repair, validation, and artifact output."""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..domain import EpisodePackage, RightsStatus
from .compiler import CompilationOptions, compile_structured_script
from .llm import StructuredScriptLLM
from .models import (
    IngestionIssue,
    IngestionReport,
    StructuredScriptDraft,
)


@dataclass(frozen=True)
class ScriptIngestionResult:
    normalized_script: str
    structured_script: StructuredScriptDraft
    episode_package: EpisodePackage
    report: IngestionReport


class ScriptIngestionError(RuntimeError):
    def __init__(self, message: str, report: IngestionReport) -> None:
        self.report = report
        super().__init__(message)


def normalize_script_text(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("script text must be a string")
    normalized = unicodedata.normalize("NFC", text.replace("\ufeff", ""))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(
        line.rstrip().replace("\t", "    ")
        for line in normalized.split("\n")
    )
    while "\n\n\n\n" in normalized:
        normalized = normalized.replace("\n\n\n\n", "\n\n\n")
    normalized = normalized.strip()
    if not normalized:
        raise ValueError("script text is empty after normalization")
    if len(normalized) > 100_000:
        raise ValueError("script text exceeds the 100000-character limit")
    return normalized + "\n"


def ingest_script(
    text: str,
    *,
    llm: StructuredScriptLLM,
    rights_status: RightsStatus = RightsStatus.UNKNOWN,
    created_at: datetime | None = None,
) -> ScriptIngestionResult:
    normalized = normalize_script_text(text)
    digest = f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"
    previous_payload: dict[str, Any] | None = None
    validation_errors: list[dict[str, Any]] = []

    for attempt in range(1, 3):
        try:
            payload = llm.generate(
                normalized,
                previous_payload=previous_payload,
                validation_errors=validation_errors,
            )
            if not isinstance(payload, dict):
                raise ValueError("LLM payload root must be an object")
            previous_payload = payload
            draft = StructuredScriptDraft.model_validate(payload)
            package = compile_structured_script(
                draft,
                normalized_script=normalized,
                options=CompilationOptions(
                    rights_status=rights_status,
                    created_at=created_at or datetime.now(timezone.utc),
                ),
            )
            warnings = [
                IngestionIssue(
                    code="unresolved_item",
                    message=item,
                    field="unresolved_items",
                )
                for item in draft.unresolved_items
            ]
            report = IngestionReport(
                source_digest=digest,
                llm_provider=llm.provider_id,
                attempts=attempt,
                repaired=attempt > 1,
                valid=True,
                external_api_calls=attempt if llm.external else 0,
                errors=[],
                warnings=warnings,
            )
            return ScriptIngestionResult(
                normalized_script=normalized,
                structured_script=draft,
                episode_package=package,
                report=report,
            )
        except (ValidationError, ValueError) as exc:
            validation_errors = _validation_errors(exc)
            issues = [
                IngestionIssue(
                    code=item["type"],
                    message=item["message"],
                    field=item.get("field"),
                )
                for item in validation_errors
            ]
            if attempt == 2:
                report = IngestionReport(
                    source_digest=digest,
                    llm_provider=llm.provider_id,
                    attempts=2,
                    repaired=True,
                    valid=False,
                    external_api_calls=2 if llm.external else 0,
                    errors=issues,
                    warnings=[],
                )
                raise ScriptIngestionError(
                    "script ingestion failed after one bounded repair",
                    report,
                ) from exc

    raise AssertionError("bounded ingestion loop must return or raise")


def write_ingestion_artifacts(
    result: ScriptIngestionResult,
    output_dir: str | Path,
) -> dict[str, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = {
        "normalized_script": destination / "normalized_script.txt",
        "structured_script": destination / "structured_script.json",
        "episode_package": destination / "episode_package.json",
        "ingestion_report": destination / "ingestion_report.json",
        "unresolved_items": destination / "unresolved_items.json",
    }
    _atomic_write(paths["normalized_script"], result.normalized_script)
    _atomic_write(
        paths["structured_script"],
        result.structured_script.model_dump_json(
            indent=2,
            exclude_none=True,
        )
        + "\n",
    )
    _atomic_write(
        paths["episode_package"],
        result.episode_package.to_canonical_json() + "\n",
    )
    _atomic_write(
        paths["ingestion_report"],
        result.report.model_dump_json(indent=2, exclude_none=True) + "\n",
    )
    _atomic_write(
        paths["unresolved_items"],
        json.dumps(
            result.structured_script.unresolved_items,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return paths


def write_failure_report(
    report: IngestionReport,
    output_dir: str | Path,
) -> Path:
    destination = Path(output_dir) / "ingestion_report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        destination,
        report.model_dump_json(indent=2, exclude_none=True) + "\n",
    )
    return destination


def _validation_errors(exc: ValidationError | ValueError) -> list[dict[str, Any]]:
    if isinstance(exc, ValidationError):
        return [
            {
                "type": str(item.get("type", "validation_error")),
                "message": str(item.get("msg", "validation failed")),
                "field": ".".join(str(part) for part in item.get("loc", ())) or None,
            }
            for item in exc.errors()
        ]
    return [
        {
            "type": "value_error",
            "message": str(exc),
            "field": None,
        }
    ]


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
