"""Validate and approve provider or operator MP4s as SegmentArtifact records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..generation.models import GenerationPlanEpisode, GenerationSegment
from ..rendering.ffmpeg import black_duration, ffprobe_json, file_sha256, run_command
from .models import SegmentArtifact, SegmentArtifactManifest


SEGMENT_IMPORT_SCHEMA_VERSION = "1.0.0"
EvidenceKind = Literal["seedance_operator", "wan_canary", "minimax_h3_canary"]


class SegmentImportError(RuntimeError):
    """A provider result cannot be trusted as a production segment."""


class ImportModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )


class SegmentEvidence(ImportModel):
    kind: EvidenceKind
    report_path: str | None = None
    ledger_path: str | None = None
    approval_manifest_path: str | None = None
    operator_notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_evidence(self) -> "SegmentEvidence":
        automated = self.kind in {"wan_canary", "minimax_h3_canary"}
        paths = [self.report_path, self.ledger_path, self.approval_manifest_path]
        if automated and any(not item for item in paths):
            raise ValueError(
                f"{self.kind} evidence requires report, ledger, and approval manifest"
            )
        if self.kind == "seedance_operator":
            if any(paths):
                raise ValueError(
                    "seedance_operator evidence must not masquerade as automated evidence"
                )
            if not self.operator_notes or len(self.operator_notes.strip()) < 10:
                raise ValueError(
                    "seedance_operator evidence requires meaningful operator_notes"
                )
        return self


class SegmentMediaFacts(ImportModel):
    output_path: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    frame_count: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    video_streams: int = Field(ge=0)
    audio_streams: int = Field(ge=0)
    audio_present: bool
    black_duration_seconds: float = Field(ge=0)


class SegmentImportPreflight(ImportModel):
    schema_version: Literal[SEGMENT_IMPORT_SCHEMA_VERSION] = SEGMENT_IMPORT_SCHEMA_VERSION
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_id: str = Field(min_length=1)
    provider_route_id: str = Field(min_length=1)
    evidence_kind: EvidenceKind
    evidence_hashes: dict[str, str] = Field(default_factory=dict)
    evidence_paths: dict[str, str] = Field(default_factory=dict)
    media: SegmentMediaFacts
    required_window_end_seconds: float = Field(gt=0)
    audio_required: bool
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    external_api_calls: Literal[0] = 0
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_digest(self) -> "SegmentImportPreflight":
        if self.valid and self.errors:
            raise ValueError("valid preflight cannot contain errors")
        if not self.valid and not self.errors:
            raise ValueError("invalid preflight requires errors")
        if self.content_digest != self.compute_content_digest():
            raise ValueError("segment import preflight digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "SegmentImportPreflight":
        provisional = cls.model_construct(
            **data,
            content_digest="sha256:" + "0" * 64,
        )
        return cls.model_validate(
            {**data, "content_digest": provisional.compute_content_digest()}
        )

    def compute_content_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return _canonical_digest(payload)

    def to_canonical_json(self) -> str:
        return _json(self.model_dump(mode="json", exclude_none=True))


class SegmentImportApproval(ImportModel):
    schema_version: Literal[SEGMENT_IMPORT_SCHEMA_VERSION] = SEGMENT_IMPORT_SCHEMA_VERSION
    preflight_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    generation_plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    segment_id: str = Field(min_length=1)
    provider_route_id: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    approval_note: str | None = Field(default=None, max_length=4000)
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_digest(self) -> "SegmentImportApproval":
        if self.content_digest != self.compute_content_digest():
            raise ValueError("segment import approval digest does not match content")
        return self

    @classmethod
    def build_with_digest(cls, **data: object) -> "SegmentImportApproval":
        provisional = cls.model_construct(
            **data,
            content_digest="sha256:" + "0" * 64,
        )
        return cls.model_validate(
            {**data, "content_digest": provisional.compute_content_digest()}
        )

    def compute_content_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("content_digest", None)
        return _canonical_digest(payload)

    def to_canonical_json(self) -> str:
        return _json(self.model_dump(mode="json", exclude_none=True))


def inspect_segment_import(
    plan: GenerationPlanEpisode,
    *,
    segment_id: str,
    output_path: str | Path,
    evidence: SegmentEvidence,
    max_black_seconds: float = 0.25,
) -> SegmentImportPreflight:
    segment = _find_segment(plan, segment_id)
    _validate_evidence_route(segment, evidence.kind)
    evidence_hashes, evidence_paths = _validate_evidence_files(
        plan,
        segment,
        output_path=Path(output_path).resolve(),
        evidence=evidence,
    )
    media = _inspect_media(Path(output_path).resolve())
    errors: list[str] = []
    warnings: list[str] = []
    expected_ratio = 9 / 16
    actual_ratio = media.width / media.height
    if media.video_streams != 1:
        errors.append(f"expected exactly one video stream, found {media.video_streams}")
    if abs(actual_ratio - expected_ratio) > 0.025:
        errors.append(
            f"source aspect ratio {actual_ratio:.5f} is not close to vertical 9:16"
        )
    required_end = segment.used_end_frame / segment.timeline_fps
    tolerance = max(0.08, 2 / segment.timeline_fps)
    if media.duration_seconds + tolerance < required_end:
        errors.append(
            f"source duration {media.duration_seconds:.4f}s is shorter than required "
            f"editorial window end {required_end:.4f}s"
        )
    maximum_accepted = max(
        float(segment.requested_duration_seconds) + 5.0,
        required_end + 2.0,
    )
    if media.duration_seconds > maximum_accepted + tolerance:
        errors.append(
            f"source duration {media.duration_seconds:.4f}s exceeds accepted provider "
            f"window {maximum_accepted:.4f}s"
        )
    audio_required = segment.audio_strategy in {"native_av", "external_audio_post"}
    if audio_required and not media.audio_present:
        errors.append(
            f"segment audio strategy {segment.audio_strategy} requires a final audio track"
        )
    if not audio_required and not media.audio_present:
        warnings.append(
            "silent segment will receive deterministic stereo silence at compose"
        )
    if media.black_duration_seconds > max_black_seconds:
        errors.append(
            f"black-frame duration {media.black_duration_seconds:.4f}s exceeds "
            f"{max_black_seconds:.4f}s"
        )
    return SegmentImportPreflight.build_with_digest(
        generation_plan_digest=plan.content_digest,
        segment_id=segment.segment_id,
        provider_route_id=segment.provider_route_id,
        evidence_kind=evidence.kind,
        evidence_hashes=evidence_hashes,
        evidence_paths=evidence_paths,
        media=media,
        required_window_end_seconds=required_end,
        audio_required=audio_required,
        valid=not errors,
        errors=errors,
        warnings=warnings,
        external_api_calls=0,
    )


def revalidate_segment_import(
    plan: GenerationPlanEpisode,
    preflight: SegmentImportPreflight,
    *,
    evidence: SegmentEvidence,
    max_black_seconds: float = 0.25,
) -> SegmentImportPreflight:
    current = inspect_segment_import(
        plan,
        segment_id=preflight.segment_id,
        output_path=preflight.media.output_path,
        evidence=evidence,
        max_black_seconds=max_black_seconds,
    )
    if current.content_digest != preflight.content_digest:
        raise SegmentImportError(
            "segment MP4, evidence, or plan changed after preflight"
        )
    return current


def approve_segment_import(
    plan: GenerationPlanEpisode,
    preflight: SegmentImportPreflight,
    *,
    approved_by: str,
    approval_note: str | None = None,
    approved_at: datetime | None = None,
) -> tuple[SegmentImportApproval, SegmentArtifact]:
    if not preflight.valid:
        raise SegmentImportError("cannot approve an invalid segment import preflight")
    if preflight.generation_plan_digest != plan.content_digest:
        raise SegmentImportError("preflight belongs to another GenerationPlan")
    segment = _find_segment(plan, preflight.segment_id)
    if segment.provider_route_id != preflight.provider_route_id:
        raise SegmentImportError("preflight provider route changed after inspection")
    approver = approved_by.strip()
    if not approver:
        raise SegmentImportError("approved_by is required")
    timestamp = approved_at or datetime.now(timezone.utc)
    approval = SegmentImportApproval.build_with_digest(
        preflight_digest=preflight.content_digest,
        generation_plan_digest=plan.content_digest,
        segment_id=segment.segment_id,
        provider_route_id=segment.provider_route_id,
        output_sha256=preflight.media.output_sha256,
        approved_by=approver,
        approved_at=timestamp,
        approval_note=approval_note,
    )
    artifact = SegmentArtifact(
        segment_id=segment.segment_id,
        generation_plan_digest=plan.content_digest,
        provider_route_id=segment.provider_route_id,
        output_path=preflight.media.output_path,
        output_sha256=preflight.media.output_sha256,
        width=preflight.media.width,
        height=preflight.media.height,
        fps=preflight.media.fps,
        frame_count=preflight.media.frame_count,
        duration_seconds=preflight.media.duration_seconds,
        audio_present=preflight.media.audio_present,
        approval_digest=approval.content_digest,
        ledger_path=preflight.evidence_paths.get("ledger"),
        imported_by=approver,
        valid=True,
    )
    return approval, artifact


def build_artifact_manifest(
    plan: GenerationPlanEpisode,
    artifacts: list[SegmentArtifact],
) -> SegmentArtifactManifest:
    expected = [item.segment_id for item in plan.segments]
    actual = [item.segment_id for item in artifacts]
    if actual != expected:
        raise SegmentImportError(
            "artifact order must exactly match GenerationPlan; "
            f"expected={expected}, actual={actual}"
        )
    for artifact, segment in zip(artifacts, plan.segments):
        if artifact.provider_route_id != segment.provider_route_id:
            raise SegmentImportError(
                f"artifact {artifact.segment_id} route does not match GenerationPlan"
            )
    return SegmentArtifactManifest.build_with_digest(
        generation_plan_digest=plan.content_digest,
        artifacts=artifacts,
    )


def _find_segment(plan: GenerationPlanEpisode, segment_id: str) -> GenerationSegment:
    matches = [item for item in plan.segments if item.segment_id == segment_id]
    if len(matches) != 1:
        raise SegmentImportError(f"unknown or duplicate segment: {segment_id}")
    return matches[0]


def _validate_evidence_route(segment: GenerationSegment, kind: EvidenceKind) -> None:
    allowed = {
        "seedance/platform": "seedance_operator",
        "wan/i2v": "wan_canary",
        "minimax/h3-reference-av": "minimax_h3_canary",
        "minimax/h3-first-frame": "minimax_h3_canary",
        "minimax/h3-text": "minimax_h3_canary",
    }
    expected = allowed.get(segment.provider_route_id)
    if expected is None:
        raise SegmentImportError(
            f"segment route {segment.provider_route_id} has no import evidence contract"
        )
    if kind != expected:
        raise SegmentImportError(
            f"segment route {segment.provider_route_id} requires evidence kind "
            f"{expected}, not {kind}"
        )


def _validate_evidence_files(
    plan: GenerationPlanEpisode,
    segment: GenerationSegment,
    *,
    output_path: Path,
    evidence: SegmentEvidence,
) -> tuple[dict[str, str], dict[str, str]]:
    if evidence.kind == "seedance_operator":
        return (
            {
                "operator_notes": _canonical_digest(
                    {"operator_notes": evidence.operator_notes or ""}
                )
            },
            {},
        )
    paths = {
        "report": Path(evidence.report_path or "").resolve(),
        "ledger": Path(evidence.ledger_path or "").resolve(),
        "approval_manifest": Path(evidence.approval_manifest_path or "").resolve(),
    }
    payloads: dict[str, dict] = {}
    hashes: dict[str, str] = {}
    for name, path in paths.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise SegmentImportError(f"evidence {name} is missing or empty: {path}")
        hashes[name] = file_sha256(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SegmentImportError(
                f"evidence {name} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise SegmentImportError(f"evidence {name} must be a JSON object")
        payloads[name] = payload

    report = payloads["report"]
    if report.get("valid") is not True:
        raise SegmentImportError("provider report is not valid")
    if report.get("segment_id") != segment.segment_id:
        raise SegmentImportError("provider report segment_id does not match")
    if report.get("provider_route_id") != segment.provider_route_id:
        raise SegmentImportError("provider report route does not match")
    report_plan_digest = report.get("generation_plan_digest")
    if report_plan_digest is not None and report_plan_digest != plan.content_digest:
        raise SegmentImportError("provider report GenerationPlan digest does not match")
    report_output = report.get("output") or report.get("output_file")
    if report_output and Path(str(report_output)).resolve() != output_path:
        raise SegmentImportError("provider report output path does not match MP4")

    if evidence.kind == "wan_canary":
        _validate_wan_report(report)
        _validate_wan_ledger(payloads["ledger"], segment)
    else:
        _validate_h3_report(report)
        _validate_h3_ledger(payloads["ledger"], segment, output_path)
    _validate_request_approval(payloads["approval_manifest"], segment)
    return hashes, {name: str(path) for name, path in paths.items()}


def _validate_wan_report(report: dict) -> None:
    if report.get("stage") != "render":
        raise SegmentImportError("Wan report is not a render report")
    if report.get("status") not in {"succeeded", "validated"}:
        raise SegmentImportError("Wan report is not a successful render report")
    if int(report.get("delegate_exit_code", 0)) != 0:
        raise SegmentImportError("Wan delegated render did not exit successfully")


def _validate_h3_report(report: dict) -> None:
    if report.get("stage") not in {"render", "resume"}:
        raise SegmentImportError("H3 report is not a render/resume report")
    if report.get("status") not in {"validated", "downloaded"}:
        raise SegmentImportError("H3 report is not in a reusable media state")
    if int(report.get("submission_attempts", 0)) != 1:
        raise SegmentImportError("H3 report must show exactly one submission attempt")


def _validate_wan_ledger(ledger: dict, segment: GenerationSegment) -> None:
    if ledger.get("shot_id") != segment.segment_id:
        raise SegmentImportError("Wan ledger shot_id does not match segment")
    operations = ledger.get("operations")
    if not isinstance(operations, dict) or not operations:
        raise SegmentImportError("Wan ledger contains no provider operations")
    records = list(operations.values())
    if any(not isinstance(item, dict) for item in records):
        raise SegmentImportError("Wan ledger operation must be a JSON object")
    if any(item.get("shot_id") != segment.segment_id for item in records):
        raise SegmentImportError("Wan ledger operation belongs to another segment")
    if any(item.get("status") != "succeeded" for item in records):
        raise SegmentImportError("Wan ledger contains a non-succeeded operation")
    if not any(item.get("operation_type") == "video" for item in records):
        raise SegmentImportError("Wan ledger has no succeeded video operation")


def _validate_h3_ledger(
    ledger: dict,
    segment: GenerationSegment,
    output_path: Path,
) -> None:
    if ledger.get("segment_id") != segment.segment_id:
        raise SegmentImportError("H3 ledger segment_id does not match")
    if ledger.get("route_id") != segment.provider_route_id:
        raise SegmentImportError("H3 ledger route_id does not match")
    if ledger.get("status") != "validated":
        raise SegmentImportError("H3 ledger must be in validated state")
    if int(ledger.get("submission_attempts", 0)) != 1:
        raise SegmentImportError("H3 ledger must contain exactly one submission attempt")
    if int(ledger.get("external_api_calls", 0)) != 1:
        raise SegmentImportError("H3 ledger must contain exactly one external API call")
    final_path = ledger.get("final_video_path")
    final_hash = ledger.get("final_video_sha256")
    if not final_path or Path(str(final_path)).resolve() != output_path:
        raise SegmentImportError("H3 ledger final video path does not match MP4")
    if final_hash != file_sha256(output_path):
        raise SegmentImportError("H3 ledger final video hash does not match MP4")


def _validate_request_approval(approval: dict, segment: GenerationSegment) -> None:
    approval_segment = approval.get("segment_id") or approval.get("shot_id")
    if approval_segment != segment.segment_id:
        raise SegmentImportError(
            "provider approval manifest segment identity does not match"
        )


def _inspect_media(path: Path) -> SegmentMediaFacts:
    if not path.is_file() or path.stat().st_size == 0:
        raise SegmentImportError(f"segment MP4 is missing or empty: {path}")
    probe = ffprobe_json(path)
    streams = probe.get("streams", [])
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if not videos:
        raise SegmentImportError("segment MP4 has no video stream")
    video = videos[0]
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    duration = float(probe.get("format", {}).get("duration") or 0.0)
    frames = _count_frames(path)
    if width <= 0 or height <= 0 or fps <= 0 or duration <= 0 or frames <= 0:
        raise SegmentImportError("segment MP4 media facts are incomplete or invalid")
    return SegmentMediaFacts(
        output_path=str(path),
        output_sha256=file_sha256(path),
        width=width,
        height=height,
        fps=fps,
        frame_count=frames,
        duration_seconds=duration,
        video_streams=len(videos),
        audio_streams=len(audios),
        audio_present=bool(audios),
        black_duration_seconds=max(0.0, black_duration(path)),
    )


def _parse_rate(value: object) -> float:
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _count_frames(path: Path) -> int:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ]
    )
    value = result.stdout.strip()
    if not value or value == "N/A":
        return 0
    return int(value)


def _canonical_digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    ) + "\n"
