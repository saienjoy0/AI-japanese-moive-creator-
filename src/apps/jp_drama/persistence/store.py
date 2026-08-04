"""Atomic, idempotent file persistence for LumenX projects."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable

from src.apps.comic_gen.models import Script

from ..preparation.models import PreparedEpisode
from .adapter import build_lumenx_project
from .models import (
    PersistenceEntry,
    PersistenceIndex,
    PersistenceResult,
)
from .verifier import verify_lumenx_project


_STORE_LOCK = threading.RLock()


class PersistenceError(RuntimeError):
    """Base failure for PR5 persistence."""


class PersistenceNotReadyError(PersistenceError):
    """PreparedEpisode is not allowed to be persisted."""


class PersistenceConflictError(PersistenceError):
    """Existing project state differs and overwrite was not requested."""


class PersistenceVerificationError(PersistenceError):
    """A converted or read-back LumenX project failed verification."""


class LumenXProjectStore:
    """Save one PreparedEpisode into the existing LumenX projects.json format."""

    def __init__(
        self,
        projects_file: str | Path = "output/projects.json",
        index_file: str | Path = "output/jp_drama/persistence_index.json",
    ) -> None:
        self.projects_file = Path(projects_file)
        self.index_file = Path(index_file)
        if self.projects_file.resolve() == self.index_file.resolve():
            raise ValueError("projects_file and index_file must be different")

    def save(
        self,
        prepared: PreparedEpisode,
        *,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> PersistenceResult:
        if not prepared.readiness_report.generation_ready:
            raise PersistenceNotReadyError(
                "PreparedEpisode generation_ready must be true before persistence"
            )
        if prepared.readiness_report.errors:
            raise PersistenceNotReadyError(
                "PreparedEpisode contains readiness errors and cannot be persisted"
            )

        project = build_lumenx_project(prepared)
        verification = verify_lumenx_project(prepared, project)
        if not verification.verified:
            messages = "; ".join(issue.message for issue in verification.errors)
            raise PersistenceVerificationError(
                f"converted LumenX project failed verification: {messages}"
            )

        project_payload = project.model_dump(mode="json")
        project_hash = _digest(project_payload)
        entry = PersistenceEntry(
            project_id=project.id,
            source_digest=prepared.source_digest,
            package_id=prepared.package_id,
            episode_id=prepared.episode_id,
            prepared_schema_version=prepared.schema_version,
            compiler_version=prepared.compiler_version,
            project_hash=project_hash,
            source_series_id=prepared.project_draft.series_id,
            episode_number=prepared.project_draft.episode_number,
        )

        with _STORE_LOCK:
            projects = self._load_projects()
            index = self._load_index()
            existing_payload = projects.get(project.id)
            existing_entry = index.projects.get(project.id)
            existing_hash = _digest(existing_payload) if existing_payload is not None else None

            state = self._classify_existing(
                prepared=prepared,
                project_hash=project_hash,
                existing_payload=existing_payload,
                existing_entry=existing_entry,
                existing_hash=existing_hash,
                overwrite=overwrite,
            )

            if state == "unchanged":
                restored = Script.model_validate(existing_payload)
                readback = verify_lumenx_project(prepared, restored)
                if not readback.verified:
                    raise PersistenceVerificationError(
                        "existing managed LumenX project failed read-back verification"
                    )
                return PersistenceResult(
                    status="unchanged",
                    project_id=project.id,
                    source_digest=prepared.source_digest,
                    project_hash=project_hash,
                    projects_file=str(self.projects_file),
                    index_file=str(self.index_file),
                    files_written=[],
                    verified=True,
                    external_api_calls=0,
                    verification=readback,
                )

            if dry_run:
                return PersistenceResult(
                    status="dry_run",
                    project_id=project.id,
                    source_digest=prepared.source_digest,
                    project_hash=project_hash,
                    projects_file=str(self.projects_file),
                    index_file=str(self.index_file),
                    files_written=[],
                    verified=True,
                    external_api_calls=0,
                    verification=verification,
                )

            projects[project.id] = project_payload
            index.projects[project.id] = entry
            projects_bytes = _json_bytes(projects)
            index_bytes = index.to_canonical_json().encode("utf-8")

            def validate_committed_state() -> None:
                read_projects = self._load_projects()
                read_index = self._load_index()
                if project.id not in read_projects:
                    raise PersistenceVerificationError(
                        "persisted project is missing after commit"
                    )
                read_entry = read_index.projects.get(project.id)
                if read_entry is None:
                    raise PersistenceVerificationError(
                        "persistence index entry is missing after commit"
                    )
                if read_entry != entry:
                    raise PersistenceVerificationError(
                        "persistence index entry changed during commit"
                    )
                read_project = Script.model_validate(read_projects[project.id])
                if _digest(read_projects[project.id]) != project_hash:
                    raise PersistenceVerificationError(
                        "persisted project hash differs from the converted project"
                    )
                report = verify_lumenx_project(prepared, read_project)
                if not report.verified:
                    raise PersistenceVerificationError(
                        "persisted LumenX project failed read-back verification"
                    )

            self._commit_transaction(
                projects_bytes,
                index_bytes,
                validate_committed_state,
            )

            read_project = Script.model_validate(self._load_projects()[project.id])
            readback = verify_lumenx_project(prepared, read_project)
            status = "replaced" if state == "replace" else "created"
            return PersistenceResult(
                status=status,
                project_id=project.id,
                source_digest=prepared.source_digest,
                project_hash=project_hash,
                projects_file=str(self.projects_file),
                index_file=str(self.index_file),
                files_written=[str(self.projects_file), str(self.index_file)],
                verified=readback.verified,
                external_api_calls=0,
                verification=readback,
            )

    def read(self, project_id: str) -> Script | None:
        payload = self._load_projects().get(project_id)
        return Script.model_validate(payload) if payload is not None else None

    def _classify_existing(
        self,
        *,
        prepared: PreparedEpisode,
        project_hash: str,
        existing_payload: dict | None,
        existing_entry: PersistenceEntry | None,
        existing_hash: str | None,
        overwrite: bool,
    ) -> str:
        if existing_payload is None and existing_entry is None:
            return "create"

        fully_managed = existing_payload is not None and existing_entry is not None
        same_source = (
            fully_managed
            and existing_entry.source_digest == prepared.source_digest
        )
        same_expected_hash = (
            fully_managed
            and existing_entry.project_hash == project_hash
            and existing_hash == project_hash
        )
        if same_source and same_expected_hash:
            return "unchanged"

        if overwrite:
            return "replace"

        if not fully_managed:
            raise PersistenceConflictError(
                "project exists in only one persistence file; use --overwrite to repair it"
            )
        if existing_entry.project_hash != existing_hash:
            raise PersistenceConflictError(
                "managed LumenX project was modified outside PR5; use --overwrite to replace it"
            )
        if existing_entry.source_digest != prepared.source_digest:
            raise PersistenceConflictError(
                "project ID is already managed by a different source digest"
            )
        raise PersistenceConflictError(
            "converted LumenX project differs from the managed project; use --overwrite"
        )

    def _load_projects(self) -> dict[str, dict]:
        if not self.projects_file.exists():
            return {}
        try:
            payload = json.loads(self.projects_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PersistenceError(f"failed to read projects file: {exc}") from exc
        if not isinstance(payload, dict):
            raise PersistenceError("projects file must contain a JSON object")
        for project_id, value in payload.items():
            if not isinstance(project_id, str) or not isinstance(value, dict):
                raise PersistenceError("projects file entries must be object values")
            try:
                Script.model_validate(value)
            except Exception as exc:
                raise PersistenceError(
                    f"existing LumenX project {project_id} is invalid: {exc}"
                ) from exc
        return payload

    def _load_index(self) -> PersistenceIndex:
        if not self.index_file.exists():
            return PersistenceIndex()
        try:
            payload = json.loads(self.index_file.read_text(encoding="utf-8"))
            return PersistenceIndex.model_validate(payload)
        except Exception as exc:
            raise PersistenceError(f"failed to read persistence index: {exc}") from exc

    def _commit_transaction(
        self,
        projects_bytes: bytes,
        index_bytes: bytes,
        validate: Callable[[], None],
    ) -> None:
        old_projects = self.projects_file.read_bytes() if self.projects_file.exists() else None
        old_index = self.index_file.read_bytes() if self.index_file.exists() else None
        staged_projects = self._stage_bytes(self.projects_file, projects_bytes)
        staged_index = self._stage_bytes(self.index_file, index_bytes)
        try:
            self._replace_staged(staged_projects, self.projects_file)
            self._replace_staged(staged_index, self.index_file)
            validate()
        except Exception as exc:
            rollback_errors: list[str] = []
            for path, old_bytes in (
                (self.index_file, old_index),
                (self.projects_file, old_projects),
            ):
                try:
                    self._restore_file(path, old_bytes)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
            suffix = (
                f"; rollback failures: {rollback_errors}"
                if rollback_errors
                else ""
            )
            raise PersistenceError(
                f"persistence transaction failed and was rolled back: {exc}{suffix}"
            ) from exc
        finally:
            for staged in (staged_projects, staged_index):
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    pass

    def _stage_bytes(self, destination: Path, content: bytes) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        )
        staged = Path(handle.name)
        try:
            with handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            staged.unlink(missing_ok=True)
            raise
        return staged

    def _replace_staged(self, staged: Path, destination: Path) -> None:
        os.replace(staged, destination)

    def _restore_file(self, destination: Path, old_bytes: bytes | None) -> None:
        if old_bytes is None:
            destination.unlink(missing_ok=True)
            return
        staged = self._stage_bytes(destination, old_bytes)
        try:
            os.replace(staged, destination)
        finally:
            staged.unlink(missing_ok=True)


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
