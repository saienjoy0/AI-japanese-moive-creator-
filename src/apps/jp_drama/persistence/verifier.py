"""Read-back verification for persisted Japanese-drama LumenX projects."""

from __future__ import annotations

from typing import Any

from src.apps.comic_gen.models import Script

from ..preparation.models import PreparedEpisode
from .models import VerificationIssue, VerificationReport


_MEDIA_URL_FIELDS = {
    "image_url",
    "video_url",
    "audio_url",
    "sfx_url",
    "bgm_url",
    "rendered_image_url",
    "dubbed_video_url",
    "bg_audio_url",
    "preview_video_url",
    "full_body_image_url",
    "three_view_image_url",
    "headshot_image_url",
    "avatar_url",
    "merged_video_url",
}


def verify_lumenx_project(
    prepared: PreparedEpisode,
    project: Script,
) -> VerificationReport:
    errors: list[VerificationIssue] = []
    warnings: list[VerificationIssue] = []

    def error(
        code: str,
        message: str,
        *,
        field: str | None = None,
        frame_id: str | None = None,
    ) -> None:
        errors.append(
            VerificationIssue(
                code=code,
                severity="error",
                message=message,
                field=field,
                frame_id=frame_id,
            )
        )

    if project.id != prepared.project_draft.project_id:
        error("project_id_mismatch", "LumenX project ID does not match ProjectDraft", field="id")
    if project.title != prepared.project_draft.title:
        error(
            "project_title_mismatch",
            "LumenX project title does not match ProjectDraft",
            field="title",
        )
    if project.episode_number != prepared.project_draft.episode_number:
        error(
            "episode_number_mismatch",
            "LumenX episode number does not match ProjectDraft",
            field="episode_number",
        )
    if project.video_tasks:
        error("video_tasks_present", "PR5 must not create VideoTask records", field="video_tasks")

    expected_character_ids = [seed.seed_id for seed in prepared.character_seeds]
    expected_scene_ids = [seed.seed_id for seed in prepared.location_seeds]
    expected_prop_ids = [seed.seed_id for seed in prepared.prop_seeds]
    expected_frame_ids = [
        frame.frame_id
        for frame in sorted(prepared.storyboard_frame_drafts, key=lambda item: item.order)
    ]

    _verify_ids(
        expected_character_ids,
        [character.id for character in project.characters],
        "character",
        error,
    )
    _verify_ids(
        expected_scene_ids,
        [scene.id for scene in project.scenes],
        "scene",
        error,
    )
    _verify_ids(
        expected_prop_ids,
        [prop.id for prop in project.props],
        "prop",
        error,
    )
    _verify_ids(
        expected_frame_ids,
        [frame.id for frame in project.frames],
        "frame",
        error,
    )

    character_ids = {character.id for character in project.characters}
    scene_ids = {scene.id for scene in project.scenes}
    prop_ids = {prop.id for prop in project.props}
    prepared_frames = {
        frame.frame_id: frame for frame in prepared.storyboard_frame_drafts
    }

    for frame in project.frames:
        source = prepared_frames.get(frame.id)
        if source is None:
            continue
        if frame.scene_id not in scene_ids:
            error(
                "frame_scene_missing",
                f"Frame references unknown scene {frame.scene_id}",
                field="scene_id",
                frame_id=frame.id,
            )
        unknown_characters = sorted(set(frame.character_ids) - character_ids)
        if unknown_characters:
            error(
                "frame_characters_missing",
                f"Frame references unknown characters: {unknown_characters}",
                field="character_ids",
                frame_id=frame.id,
            )
        unknown_props = sorted(set(frame.prop_ids) - prop_ids)
        if unknown_props:
            error(
                "frame_props_missing",
                f"Frame references unknown props: {unknown_props}",
                field="prop_ids",
                frame_id=frame.id,
            )
        if frame.scene_id != source.location_seed_id:
            error(
                "frame_scene_mapping_mismatch",
                "Frame scene mapping differs from StoryboardFrameDraft",
                field="scene_id",
                frame_id=frame.id,
            )
        if frame.character_ids != source.character_seed_ids:
            error(
                "frame_character_mapping_mismatch",
                "Frame character mapping differs from StoryboardFrameDraft",
                field="character_ids",
                frame_id=frame.id,
            )
        if frame.prop_ids != source.prop_seed_ids:
            error(
                "frame_prop_mapping_mismatch",
                "Frame prop mapping differs from StoryboardFrameDraft",
                field="prop_ids",
                frame_id=frame.id,
            )
        metadata = _jp_drama_metadata(frame.composition_data)
        expected_metadata = {
            "source_shot_id": source.source_shot_id,
            "adapted_beat_id": source.adapted_beat_id,
            "order": source.order,
            "duration_seconds": source.duration_seconds,
            "render_intent_id": source.render_intent_id,
        }
        for key, expected in expected_metadata.items():
            if metadata.get(key) != expected:
                error(
                    "frame_trace_mismatch",
                    f"Frame trace field {key} does not match the prepared draft",
                    field=f"composition_data.jp_drama.{key}",
                    frame_id=frame.id,
                )

    media_url_count = _count_media_urls(project.model_dump(mode="json"))
    if media_url_count:
        error(
            "media_urls_present",
            f"PR5 must not persist generated media URLs; found {media_url_count}",
        )

    try:
        restored = Script.model_validate(project.model_dump(mode="json"))
    except Exception as exc:
        error("lumenx_round_trip_failed", f"LumenX Script round-trip failed: {exc}")
    else:
        if restored.id != project.id:
            error("lumenx_round_trip_mismatch", "Round-tripped project ID changed")

    if prepared.project_draft.series_id:
        warnings.append(
            VerificationIssue(
                code="source_series_deferred",
                severity="warning",
                message=(
                    "The source series ID is retained in the persistence index; "
                    "PR5 saves a self-contained LumenX episode project."
                ),
                field="series_id",
            )
        )

    return VerificationReport(
        project_id=project.id,
        verified=not errors,
        character_count=len(project.characters),
        scene_count=len(project.scenes),
        prop_count=len(project.props),
        frame_count=len(project.frames),
        video_task_count=0,
        media_url_count=0 if not media_url_count else media_url_count,
        external_api_calls=0,
        errors=errors,
        warnings=warnings,
    )


def _verify_ids(
    expected: list[str],
    actual: list[str],
    label: str,
    add_error: Any,
) -> None:
    if actual != expected:
        add_error(
            f"{label}_ids_mismatch",
            f"LumenX {label} IDs differ from PreparedEpisode: expected={expected}, actual={actual}",
            field=f"{label}s",
        )
    if len(actual) != len(set(actual)):
        add_error(
            f"{label}_ids_duplicate",
            f"LumenX {label} IDs must be unique",
            field=f"{label}s",
        )


def _jp_drama_metadata(composition_data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(composition_data, dict):
        return {}
    metadata = composition_data.get("jp_drama")
    return metadata if isinstance(metadata, dict) else {}


def _count_media_urls(value: Any, field_name: str | None = None) -> int:
    if isinstance(value, dict):
        return sum(
            _count_media_urls(child, key)
            for key, child in value.items()
        )
    if isinstance(value, list):
        if field_name in {
            "reference_video_urls",
            "reference_image_urls",
            "t2i_image_urls",
        }:
            return sum(
                1 for item in value if isinstance(item, str) and item.strip()
            )
        return sum(_count_media_urls(child, field_name) for child in value)
    if field_name in _MEDIA_URL_FIELDS and isinstance(value, str) and value.strip():
        return 1
    return 0
