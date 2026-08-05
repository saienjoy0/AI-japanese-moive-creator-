from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.apps.jp_drama.seedance_storyboard import (
    ProjectMarkdown,
    SeedanceStoryboardParseError,
    UPSTREAM_COMMIT,
    UPSTREAM_FILES,
    git_blob_sha,
    load_project_directory,
    parse_asset_catalog,
    parse_project,
    parse_storyboard,
    write_import_artifacts,
)


FIXTURE = Path("examples/jp_drama/seedance_storyboard/upstream_fixture")


def _read(name: str) -> str:
    return (FIXTURE / name).read_text(encoding="utf-8")


def test_parse_upstream_asset_catalog_preserves_csp() -> None:
    assets = parse_asset_catalog(_read("雨の新店長_素材清单.md"))
    assert [item.asset_id for item in assets] == [
        "C01",
        "C02",
        "S01",
        "P01",
        "P02",
    ]
    assert [item.kind for item in assets] == [
        "character",
        "character",
        "scene",
        "prop",
        "prop",
    ]
    assert "身份反转" in next(
        item.prompt for item in assets if item.asset_id == "P01"
    )


def test_parse_storyboard_preserves_timeline_slots_sound_and_ending_frame() -> None:
    episode = parse_storyboard(_read("雨の新店長_E01_分镜.md"))
    assert episode.episode_id == "E01"
    assert episode.duration_seconds == 15
    assert len(episode.timeline) == 5
    assert [item.asset_id for item in episode.upload_slots] == [
        "C01",
        "C02",
        "S01",
        "P02",
    ]
    assert "雨声" in (episode.sound_prompt or "")
    assert "美緒位于画面左侧" in episode.ending_frame
    assert episode.continuation_source is None


def test_parse_continuation_episode() -> None:
    episode = parse_storyboard(_read("雨の新店長_E02_分镜.md"))
    assert episode.continuation_source == "视频1"
    assert episode.continuation_seconds == 15
    assert "焦点从纸面转移到太田的眼睛" in episode.timeline[1].text


def test_project_import_binds_assets_to_episodes_and_writes_artifacts(
    tmp_path: Path,
) -> None:
    source = load_project_directory(FIXTURE)
    package = parse_project(source)
    assert package.project_title == "雨の新店長"
    assert [item.episode_id for item in package.episodes] == ["E01", "E02"]
    p01 = next(item for item in package.assets if item.asset_id == "P01")
    assert p01.used_in_episode_ids == ["E02"]
    assert package.content_digest.startswith("sha256:")

    outputs = write_import_artifacts(package, tmp_path)
    assert len(outputs) == 2
    payload = json.loads(
        (tmp_path / "seedance_storyboard_package.json").read_text()
    )
    assert (
        payload["provenance"]["commit"]
        == "17b9ca6dfac3e4a086a2874791ef19ae5aae3932"
    )
    operator = (tmp_path / "seedance_operator_manifest.md").read_text()
    assert "@图片4 新店長辞令" in operator
    assert "将@视频1延长15s" in operator


def test_unknown_reference_slot_is_rejected() -> None:
    markdown = _read("雨の新店長_E01_分镜.md").replace(
        "@图片4 皮革鞄",
        "@图片9 未定义素材",
    )
    with pytest.raises(
        SeedanceStoryboardParseError,
        match="undefined upload slots",
    ):
        parse_storyboard(markdown)


def test_timeline_gap_is_rejected() -> None:
    markdown = _read("雨の新店長_E01_分镜.md").replace(
        "**3-6秒画面：**",
        "**4-6秒画面：**",
    )
    with pytest.raises(ValueError, match="gap or overlap"):
        parse_storyboard(markdown)


def test_duplicate_project_asset_catalog_requires_explicit_cleanup(
    tmp_path: Path,
) -> None:
    (tmp_path / "x_素材清单.md").write_text(
        _read("雨の新店長_素材清单.md"),
        encoding="utf-8",
    )
    (tmp_path / "x_素材清单_完整版.md").write_text(
        _read("雨の新店長_素材清单.md"),
        encoding="utf-8",
    )
    (tmp_path / "x_E01_分镜.md").write_text(
        _read("雨の新店長_E01_分镜.md"),
        encoding="utf-8",
    )
    with pytest.raises(SeedanceStoryboardParseError, match="exactly one"):
        load_project_directory(tmp_path)


def test_git_blob_sha_matches_git_object_format() -> None:
    assert git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_upstream_lock_matches_runtime_sync_contract() -> None:
    lock = json.loads(
        Path("third_party/seedance2_storyboard_generator/upstream.lock.json")
        .read_text(encoding="utf-8")
    )
    assert lock["commit"] == UPSTREAM_COMMIT
    assert lock["files"] == UPSTREAM_FILES
