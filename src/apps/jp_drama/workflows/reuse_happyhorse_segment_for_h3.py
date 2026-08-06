"""Reuse one verified HappyHorse clip and hand the remaining segments to MiniMax H3.

This workflow is deliberately provider-call free.  It converts the already generated
E01-G01 MP4 plus its immutable HappyHorse evidence into the existing production
``SegmentArtifact`` contract, then writes a handoff manifest for the remaining H3
segments.  The successful provider task is never submitted again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..generation.models import GenerationPlanEpisode
from ..production.models import SegmentArtifact
from ..rendering.ffmpeg import black_duration, ffprobe_json, file_sha256


EXIT_OK = 0
EXIT_INPUT = 1

EXPECTED_SEGMENT_ID = "E01-G01"
EXPECTED_MODEL = "happyhorse-1.1-r2v"
EXPECTED_PROVIDER_ROUTE = "dashscope/happyhorse-1.1-r2v"
EXPECTED_RUN_ID = "31085709337"
EXPECTED_TASK_ID = "2efce664-a6c0-4c95-adb6-ad7eed80403f"
EXPECTED_REQUEST_ID = "43794f28-b04b-9b9a-a49a-ac71a6abcf56"
EXPECTED_APPROVAL_DIGEST = (
    "sha256:3ec02df5cebad83874ac04ac3b1711037af70231fa8861e3df939e744b70eb3e"
)
EXPECTED_REQUEST_FINGERPRINT = (
    "sha256:4cc0dbce337978c8918e4ec8e93ad6d8922519cb5d81e2a92a49b93c1d311131"
)
EXPECTED_REFERENCE_SUBJECTS = ["C01", "S01", "S05", "P03", "P04"]
H3_ROUTE = "minimax/h3-reference-av"
H3_CN_CONFIG = "examples/jp_drama/minimax_h3_cn_live_provider.json"


class ReuseError(RuntimeError):
    """The existing clip or its evidence cannot be safely reused."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class ReusedMediaFacts(FrozenModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    frame_count: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    audio_present: bool
    black_duration_seconds: float = Field(ge=0)


class ExistingSegmentReuseApproval(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_id: Literal[EXPECTED_SEGMENT_ID] = EXPECTED_SEGMENT_ID
    planned_provider_route_id: str = Field(min_length=1)
    actual_provider_route_id: Literal[EXPECTED_PROVIDER_ROUTE] = EXPECTED_PROVIDER_ROUTE
    provider_model: Literal[EXPECTED_MODEL] = EXPECTED_MODEL
    source_workflow_run_id: Literal[EXPECTED_RUN_ID] = EXPECTED_RUN_ID
    provider_task_id: Literal[EXPECTED_TASK_ID] = EXPECTED_TASK_ID
    provider_request_id: Literal[EXPECTED_REQUEST_ID] = EXPECTED_REQUEST_ID
    request_fingerprint: Literal[EXPECTED_REQUEST_FINGERPRINT] = (
        EXPECTED_REQUEST_FINGERPRINT
    )
    source_approval_digest: Literal[EXPECTED_APPROVAL_DIGEST] = (
        EXPECTED_APPROVAL_DIGEST
    )
    ordered_reference_subjects: list[str]
    media: ReusedMediaFacts
    evidence_sha256: dict[str, str]
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    note: str = Field(min_length=10, max_length=2000)
    external_api_calls: Literal[0] = 0
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_contract(self) -> "ExistingSegmentReuseApproval":
        if self.ordered_reference_subjects != EXPECTED_REFERENCE_SUBJECTS:
            raise ValueError("ordered HappyHorse references changed")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("reuse approval digest does not match content")
        return self

    def compute_content_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return _digest(payload)

    @classmethod
    def build(cls, **values: Any) -> "ExistingSegmentReuseApproval":
        provisional = cls.model_construct(
            **values,
            content_digest="sha256:" + "0" * 64,
        )
        payload = provisional.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return cls.model_validate({**values, "content_digest": _digest(payload)})


class H3ContinuationHandoff(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    reused_segment_id: Literal[EXPECTED_SEGMENT_ID] = EXPECTED_SEGMENT_ID
    reused_segment_artifact: str = Field(min_length=1)
    reuse_approval_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    target_provider_route_id: Literal[H3_ROUTE] = H3_ROUTE
    provider_config: Literal[H3_CN_CONFIG] = H3_CN_CONFIG
    remaining_segment_ids: list[str]
    resolution: Literal["768P"] = "768P"
    rule: Literal[
        "reuse_E01-G01_without_provider_call_then_generate_only_remaining_segments"
    ] = "reuse_E01-G01_without_provider_call_then_generate_only_remaining_segments"
    external_api_calls: Literal[0] = 0
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_handoff(self) -> "H3ContinuationHandoff":
        if self.reused_segment_id in self.remaining_segment_ids:
            raise ValueError("the reused segment must not be sent to H3")
        if len(self.remaining_segment_ids) != len(set(self.remaining_segment_ids)):
            raise ValueError("remaining H3 segment IDs must be unique")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("H3 handoff digest does not match content")
        return self

    def compute_content_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return _digest(payload)

    @classmethod
    def build(cls, **values: Any) -> "H3ContinuationHandoff":
        provisional = cls.model_construct(
            **values,
            content_digest="sha256:" + "0" * 64,
        )
        payload = provisional.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return cls.model_validate({**values, "content_digest": _digest(payload)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the successful E01-G01 HappyHorse MP4, register it as an "
            "existing production segment, and route only the remaining segments "
            "to the China MiniMax H3 profile. Provider calls: zero."
        )
    )
    parser.add_argument("--generation-plan", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--provider-report", required=True, type=Path)
    parser.add_argument("--provider-ledger", required=True, type=Path)
    parser.add_argument("--provider-approval", required=True, type=Path)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--print-report", action="store_true")
    return parser


def _json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ReuseError(f"required evidence is missing or empty: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReuseError(f"invalid JSON evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReuseError(f"evidence must be a JSON object: {path}")
    return value


def _digest(payload: Any) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write(path: Path, payload: Any, *, overwrite: bool) -> None:
    path = path.resolve()
    if path.exists() and not overwrite:
        raise ReuseError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json", exclude_none=True)
    else:
        data = payload
    content = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _one_operation(ledger: dict[str, Any]) -> dict[str, Any]:
    operations = ledger.get("operations")
    if not isinstance(operations, dict) or len(operations) != 1:
        raise ReuseError("HappyHorse ledger must contain exactly one operation")
    operation = next(iter(operations.values()))
    if not isinstance(operation, dict):
        raise ReuseError("HappyHorse ledger operation is invalid")
    return operation


def _media(path: Path) -> ReusedMediaFacts:
    path = path.resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ReuseError(f"existing HappyHorse MP4 is missing or empty: {path}")
    probe = ffprobe_json(path)
    streams = probe.get("streams", [])
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise ReuseError(f"expected one video stream, found {len(videos)}")
    video = videos[0]
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0 or abs(width / height - 9 / 16) > 0.025:
        raise ReuseError("existing HappyHorse MP4 is not a valid vertical 9:16 video")
    fps_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    try:
        fps = float(Fraction(str(fps_text)))
    except (ValueError, ZeroDivisionError) as exc:
        raise ReuseError("existing HappyHorse MP4 has invalid FPS") from exc
    duration = float(probe.get("format", {}).get("duration") or 0)
    if duration + 0.08 < 10:
        raise ReuseError(f"existing HappyHorse MP4 is shorter than 10 seconds: {duration}")
    if not audios:
        raise ReuseError("existing HappyHorse MP4 has no audio stream")
    black = max(0.0, float(black_duration(path)))
    if black > 0.25:
        raise ReuseError(f"black-frame duration exceeds 0.25 seconds: {black}")
    raw_frames = video.get("nb_frames")
    try:
        frame_count = int(raw_frames) if raw_frames not in {None, "N/A"} else 0
    except (TypeError, ValueError):
        frame_count = 0
    if frame_count <= 0:
        frame_count = max(1, round(duration * fps))
    return ReusedMediaFacts(
        path=str(path),
        sha256=file_sha256(path),
        width=width,
        height=height,
        fps=fps,
        frame_count=frame_count,
        duration_seconds=duration,
        audio_present=True,
        black_duration_seconds=black,
    )


def _validate_evidence(
    *,
    plan: GenerationPlanEpisode,
    report: dict[str, Any],
    ledger: dict[str, Any],
    provider_approval: dict[str, Any],
    media: ReusedMediaFacts,
) -> tuple[str, list[str]]:
    matches = [item for item in plan.segments if item.segment_id == EXPECTED_SEGMENT_ID]
    if len(matches) != 1:
        raise ReuseError("generation plan must contain E01-G01 exactly once")
    segment = matches[0]

    if provider_approval.get("approval_digest") != EXPECTED_APPROVAL_DIGEST:
        raise ReuseError("HappyHorse source approval digest changed")
    if provider_approval.get("request_fingerprint") != EXPECTED_REQUEST_FINGERPRINT:
        raise ReuseError("HappyHorse source request fingerprint changed")
    material = provider_approval.get("approval_material")
    if not isinstance(material, dict):
        raise ReuseError("HappyHorse source approval material is missing")
    if material.get("model") != EXPECTED_MODEL:
        raise ReuseError("HappyHorse source model changed")
    references = material.get("references")
    if not isinstance(references, list):
        raise ReuseError("HappyHorse source references are missing")
    subjects = [str(item.get("subject_id")) for item in references if isinstance(item, dict)]
    if subjects != EXPECTED_REFERENCE_SUBJECTS:
        raise ReuseError(f"HappyHorse ordered references changed: {subjects}")

    if report.get("valid") is not True:
        raise ReuseError("HappyHorse render report is not valid")
    if report.get("stage") != "render":
        raise ReuseError("HappyHorse report is not a render report")
    if report.get("status") not in {
        "succeeded_awaiting_human_review",
        "reused_verified_output",
    }:
        raise ReuseError(f"HappyHorse report is not reusable: {report.get('status')}")
    if report.get("segment_id") != EXPECTED_SEGMENT_ID:
        raise ReuseError("HappyHorse report segment changed")
    if report.get("model") != EXPECTED_MODEL:
        raise ReuseError("HappyHorse report model changed")
    if report.get("provider_route_id") != EXPECTED_PROVIDER_ROUTE:
        raise ReuseError("HappyHorse actual provider route changed")
    if report.get("provider_task_id") != EXPECTED_TASK_ID:
        raise ReuseError("HappyHorse provider task ID changed")
    if report.get("provider_request_id") != EXPECTED_REQUEST_ID:
        raise ReuseError("HappyHorse provider request ID changed")
    if report.get("request", {}).get("request_fingerprint") != EXPECTED_REQUEST_FINGERPRINT:
        raise ReuseError("HappyHorse report fingerprint changed")
    source_route = str(report.get("source_provider_route_id") or "")
    if source_route != segment.provider_route_id:
        raise ReuseError(
            "HappyHorse report source route does not match the GenerationPlan: "
            f"{source_route} != {segment.provider_route_id}"
        )
    if report.get("output_sha256") != media.sha256:
        raise ReuseError("HappyHorse report MP4 SHA does not match downloaded media")

    operation = _one_operation(ledger)
    if operation.get("status") != "succeeded":
        raise ReuseError("HappyHorse ledger operation is not succeeded")
    if operation.get("operation_type") != "video":
        raise ReuseError("HappyHorse ledger operation is not a video operation")
    if operation.get("model") != EXPECTED_MODEL:
        raise ReuseError("HappyHorse ledger model changed")
    if operation.get("provider_task_id") != EXPECTED_TASK_ID:
        raise ReuseError("HappyHorse ledger task ID changed")
    if operation.get("provider_request_id") != EXPECTED_REQUEST_ID:
        raise ReuseError("HappyHorse ledger request ID changed")
    if operation.get("output_sha256") != media.sha256:
        raise ReuseError("HappyHorse ledger MP4 SHA does not match downloaded media")
    if int(ledger.get("max_api_calls", -1)) != 1:
        raise ReuseError("HappyHorse ledger does not prove a one-task ceiling")
    return source_route, subjects


def run(args: argparse.Namespace) -> dict[str, Any]:
    plan = GenerationPlanEpisode.model_validate_json(
        args.generation_plan.read_text(encoding="utf-8")
    )
    report = _json(args.provider_report.resolve())
    ledger = _json(args.provider_ledger.resolve())
    source_approval = _json(args.provider_approval.resolve())
    media = _media(args.input)
    source_route, subjects = _validate_evidence(
        plan=plan,
        report=report,
        ledger=ledger,
        provider_approval=source_approval,
        media=media,
    )
    approver = args.approved_by.strip()
    if not approver:
        raise ReuseError("--approved-by must not be empty")
    evidence_paths = {
        "provider_report": str(args.provider_report.resolve()),
        "provider_ledger": str(args.provider_ledger.resolve()),
        "provider_approval": str(args.provider_approval.resolve()),
    }
    evidence_sha = {name: file_sha256(Path(path)) for name, path in evidence_paths.items()}
    reuse_approval = ExistingSegmentReuseApproval.build(
        generation_plan_digest=plan.content_digest,
        planned_provider_route_id=source_route,
        ordered_reference_subjects=subjects,
        media=media,
        evidence_sha256=evidence_sha,
        approved_by=approver,
        approved_at=datetime.now(timezone.utc),
        note=(
            "Reuse the human-reviewed, provider-verified first ten seconds. Do not "
            "submit E01-G01 to HappyHorse or MiniMax again."
        ),
        external_api_calls=0,
    )
    output_dir = args.output_dir.resolve()
    approval_path = output_dir / f"{EXPECTED_SEGMENT_ID}.happyhorse-reuse.approval.json"
    artifact_path = output_dir / f"{EXPECTED_SEGMENT_ID}.segment_artifact.json"
    handoff_path = output_dir / "minimax_h3_cn_continuation_handoff.json"
    artifact = SegmentArtifact(
        segment_id=EXPECTED_SEGMENT_ID,
        generation_plan_digest=plan.content_digest,
        provider_route_id=source_route,
        output_path=media.path,
        output_sha256=media.sha256,
        width=media.width,
        height=media.height,
        fps=media.fps,
        frame_count=media.frame_count,
        duration_seconds=media.duration_seconds,
        audio_present=media.audio_present,
        approval_digest=reuse_approval.content_digest,
        ledger_path=str(args.provider_ledger.resolve()),
        imported_by=approver,
        valid=True,
    )
    remaining = [item.segment_id for item in plan.segments if item.segment_id != EXPECTED_SEGMENT_ID]
    handoff = H3ContinuationHandoff.build(
        generation_plan_digest=plan.content_digest,
        reused_segment_artifact=str(artifact_path),
        reuse_approval_digest=reuse_approval.content_digest,
        remaining_segment_ids=remaining,
        external_api_calls=0,
    )
    _write(approval_path, reuse_approval, overwrite=args.overwrite)
    _write(artifact_path, artifact, overwrite=args.overwrite)
    _write(handoff_path, handoff, overwrite=args.overwrite)
    return {
        "valid": True,
        "reused_segment_id": EXPECTED_SEGMENT_ID,
        "source_workflow_run_id": EXPECTED_RUN_ID,
        "provider_task_id": EXPECTED_TASK_ID,
        "provider_submissions": 0,
        "external_api_calls": 0,
        "media_sha256": media.sha256,
        "reuse_approval": str(approval_path),
        "reuse_approval_digest": reuse_approval.content_digest,
        "segment_artifact": str(artifact_path),
        "h3_handoff": str(handoff_path),
        "remaining_h3_segment_ids": remaining,
        "h3_provider_config": H3_CN_CONFIG,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except (
        OSError,
        ValueError,
        ValidationError,
        ReuseError,
    ) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT
    if args.print_report:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(
            "E01-G01 reuse: VALID\n"
            f"Existing MP4: {args.input.resolve()}\n"
            f"Existing task: {EXPECTED_TASK_ID}\n"
            f"Remaining H3 segments: {len(result['remaining_h3_segment_ids'])}\n"
            "New provider calls: 0"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
