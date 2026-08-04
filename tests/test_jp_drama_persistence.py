from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.apps.comic_gen.models import Script
from src.apps.jp_drama import EpisodePackage
from src.apps.jp_drama.persistence import (
    LumenXProjectStore,
    PersistenceConflictError,
    PersistenceError,
    PersistenceNotReadyError,
    build_lumenx_project,
    verify_lumenx_project,
)
from src.apps.jp_drama.preparation import compile_episode, load_model_catalog
from src.apps.jp_drama.workflows.save_prepared_episode import main as persistence_main


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "jp_drama" / "minimal_episode_package.json"
CATALOG = ROOT / "examples" / "jp_drama" / "model_capabilities.json"


@pytest.fixture()
def prepared():
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    package = EpisodePackage.model_validate(payload)
    return compile_episode(
        package,
        catalog=load_model_catalog(CATALOG),
        strict=True,
        source_payload=payload,
    )


def test_adapter_builds_self_contained_lumenx_project(prepared) -> None:
    project = build_lumenx_project(prepared)

    assert project.id == prepared.project_draft.project_id
    assert project.series_id is None
    assert project.episode_number == prepared.project_draft.episode_number
    assert project.workflow_mode == "r2v"
    assert project.default_generation_mode == "r2v"
    assert [item.id for item in project.characters] == [
        item.seed_id for item in prepared.character_seeds
    ]
    assert [item.id for item in project.scenes] == [
        item.seed_id for item in prepared.location_seeds
    ]
    assert [item.id for item in project.props] == [
        item.seed_id for item in prepared.prop_seeds
    ]
    assert [item.id for item in project.frames] == [
        item.frame_id
        for item in sorted(prepared.storyboard_frame_drafts, key=lambda value: value.order)
    ]


def test_adapter_does_not_create_tasks_or_media_urls(prepared) -> None:
    project = build_lumenx_project(prepared)
    report = verify_lumenx_project(prepared, project)

    assert report.verified is True
    assert report.video_task_count == 0
    assert report.media_url_count == 0
    assert report.external_api_calls == 0
    assert project.video_tasks == []


def test_frame_trace_preserves_exact_prepared_values(prepared) -> None:
    project = build_lumenx_project(prepared)
    first_source = prepared.storyboard_frame_drafts[0]
    first_frame = project.frames[0]
    trace = first_frame.composition_data["jp_drama"]

    assert trace["source_shot_id"] == first_source.source_shot_id
    assert trace["adapted_beat_id"] == first_source.adapted_beat_id
    assert trace["order"] == first_source.order
    assert trace["duration_seconds"] == first_source.duration_seconds
    assert trace["render_intent_id"] == first_source.render_intent_id
    assert trace["dialogue_cues"] == [
        cue.model_dump(mode="json", exclude_none=True)
        for cue in first_source.dialogue_cues
    ]


def test_save_creates_lumenx_projects_and_index(prepared, tmp_path: Path) -> None:
    projects_file = tmp_path / "projects.json"
    index_file = tmp_path / "jp_drama" / "persistence_index.json"
    store = LumenXProjectStore(projects_file, index_file)

    result = store.save(prepared)

    assert result.status == "created"
    assert result.verified is True
    assert result.external_api_calls == 0
    assert projects_file.exists()
    assert index_file.exists()

    projects = json.loads(projects_file.read_text(encoding="utf-8"))
    project = Script.model_validate(projects[result.project_id])
    assert project.id == result.project_id

    index = json.loads(index_file.read_text(encoding="utf-8"))
    entry = index["projects"][result.project_id]
    assert entry["source_digest"] == prepared.source_digest
    assert entry["source_series_id"] == prepared.project_draft.series_id
    assert entry["project_hash"] == result.project_hash


def test_second_save_is_idempotent_and_byte_stable(prepared, tmp_path: Path) -> None:
    projects_file = tmp_path / "projects.json"
    index_file = tmp_path / "index.json"
    store = LumenXProjectStore(projects_file, index_file)

    first = store.save(prepared)
    projects_before = projects_file.read_bytes()
    index_before = index_file.read_bytes()
    second = store.save(prepared)

    assert first.status == "created"
    assert second.status == "unchanged"
    assert second.files_written == []
    assert projects_file.read_bytes() == projects_before
    assert index_file.read_bytes() == index_before


def test_dry_run_does_not_write_storage(prepared, tmp_path: Path) -> None:
    projects_file = tmp_path / "projects.json"
    index_file = tmp_path / "index.json"
    result = LumenXProjectStore(projects_file, index_file).save(
        prepared,
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert result.files_written == []
    assert not projects_file.exists()
    assert not index_file.exists()


def test_generation_not_ready_is_rejected(prepared, tmp_path: Path) -> None:
    report = prepared.readiness_report.model_copy(
        update={"generation_ready": False}
    )
    blocked = prepared.model_copy(update={"readiness_report": report})

    with pytest.raises(PersistenceNotReadyError, match="generation_ready"):
        LumenXProjectStore(
            tmp_path / "projects.json",
            tmp_path / "index.json",
        ).save(blocked)


def test_different_source_digest_conflicts_without_overwrite(
    prepared,
    tmp_path: Path,
) -> None:
    store = LumenXProjectStore(
        tmp_path / "projects.json",
        tmp_path / "index.json",
    )
    store.save(prepared)
    changed = prepared.model_copy(
        update={"source_digest": f"sha256:{'1' * 64}"}
    )

    with pytest.raises(PersistenceConflictError, match="different source digest"):
        store.save(changed)


def test_explicit_overwrite_replaces_conflicting_entry(prepared, tmp_path: Path) -> None:
    store = LumenXProjectStore(
        tmp_path / "projects.json",
        tmp_path / "index.json",
    )
    store.save(prepared)
    changed = prepared.model_copy(
        update={"source_digest": f"sha256:{'2' * 64}"}
    )

    result = store.save(changed, overwrite=True)

    assert result.status == "replaced"
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert index["projects"][result.project_id]["source_digest"] == changed.source_digest


def test_external_project_modification_is_not_silently_overwritten(
    prepared,
    tmp_path: Path,
) -> None:
    projects_file = tmp_path / "projects.json"
    store = LumenXProjectStore(projects_file, tmp_path / "index.json")
    result = store.save(prepared)

    projects = json.loads(projects_file.read_text(encoding="utf-8"))
    projects[result.project_id]["title"] = "手動変更"
    projects_file.write_text(
        json.dumps(projects, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(PersistenceConflictError, match="modified outside PR5"):
        store.save(prepared)


def test_partial_commit_failure_rolls_back_both_files(
    prepared,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_file = tmp_path / "projects.json"
    index_file = tmp_path / "index.json"
    projects_file.write_text("{}\n", encoding="utf-8")
    store = LumenXProjectStore(projects_file, index_file)
    calls = {"count": 0}
    original = store._replace_staged

    def fail_second_replace(staged: Path, destination: Path) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected index replacement failure")
        original(staged, destination)

    monkeypatch.setattr(store, "_replace_staged", fail_second_replace)

    with pytest.raises(PersistenceError, match="rolled back"):
        store.save(prepared)

    assert projects_file.read_text(encoding="utf-8") == "{}\n"
    assert not index_file.exists()


def test_readback_verification_rejects_broken_reference(prepared) -> None:
    project = build_lumenx_project(prepared)
    project.frames[0].scene_id = "missing_scene"

    report = verify_lumenx_project(prepared, project)

    assert report.verified is False
    assert any(issue.code == "frame_scene_missing" for issue in report.errors)
    assert any(
        issue.code == "frame_scene_mapping_mismatch"
        for issue in report.errors
    )


def test_invalid_existing_projects_file_is_rejected(prepared, tmp_path: Path) -> None:
    projects_file = tmp_path / "projects.json"
    projects_file.write_text("[]", encoding="utf-8")

    with pytest.raises(PersistenceError, match="JSON object"):
        LumenXProjectStore(
            projects_file,
            tmp_path / "index.json",
        ).save(prepared)


def test_cli_persists_and_reports_success(
    prepared,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_file = tmp_path / "prepared.json"
    input_file.write_text(prepared.to_canonical_json(), encoding="utf-8")
    projects_file = tmp_path / "projects.json"
    index_file = tmp_path / "index.json"
    report_file = tmp_path / "result.json"

    code = persistence_main(
        [
            "--input",
            str(input_file),
            "--projects-file",
            str(projects_file),
            "--index-file",
            str(index_file),
            "--report",
            str(report_file),
        ]
    )

    assert code == 0
    assert "Status: created" in capsys.readouterr().out
    assert json.loads(report_file.read_text(encoding="utf-8"))["verified"] is True


def test_cli_dry_run_leaves_storage_absent(prepared, tmp_path: Path) -> None:
    input_file = tmp_path / "prepared.json"
    input_file.write_text(prepared.to_canonical_json(), encoding="utf-8")
    projects_file = tmp_path / "projects.json"
    index_file = tmp_path / "index.json"

    code = persistence_main(
        [
            "--input",
            str(input_file),
            "--projects-file",
            str(projects_file),
            "--index-file",
            str(index_file),
            "--dry-run",
        ]
    )

    assert code == 0
    assert not projects_file.exists()
    assert not index_file.exists()
