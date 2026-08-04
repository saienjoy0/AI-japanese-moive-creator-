"""Convert PreparedEpisode into concrete, non-generated LumenX models."""

from __future__ import annotations

from typing import Any

from src.apps.comic_gen.models import (
    AssetUnit,
    AudioNote,
    CameraMovementData,
    Character,
    DialogueStructured,
    GenerationStatus,
    ModelSettings,
    Prop,
    Scene,
    Script,
    StoryboardFrame,
)

from ..preparation.models import (
    CharacterSeed,
    DialogueDraft,
    LocationSeed,
    PreparedEpisode,
    PropSeed,
    StoryboardFrameDraft,
)


_SHOT_SIZE_MAP = {
    "extreme_close_up": "大特写",
    "close_up": "特写",
    "medium_close_up": "近景",
    "medium": "中景",
    "full": "全景",
    "long": "远景",
    "extreme_long": "大远景",
}

_CAMERA_ANGLE_MAP = {
    "eye_level": "平视",
    "high": "俯视",
    "low": "仰视",
    "birds_eye": "鸟瞰",
    "worms_eye": "蚁视",
    "over_shoulder": "过肩",
    "dutch": "荷兰角",
    "pov": "主观视角",
}


def build_lumenx_project(prepared: PreparedEpisode) -> Script:
    """Build a LumenX Script without creating media or provider tasks."""

    intents = {intent.intent_id: intent for intent in prepared.render_intents}
    characters = [_build_character(seed) for seed in prepared.character_seeds]
    scenes = [_build_scene(seed) for seed in prepared.location_seeds]
    props = [_build_prop(seed) for seed in prepared.prop_seeds]
    frames = [
        _build_frame(frame, intents.get(frame.render_intent_id))
        for frame in sorted(prepared.storyboard_frame_drafts, key=lambda item: item.order)
    ]

    return Script(
        id=prepared.project_draft.project_id,
        title=prepared.project_draft.title,
        original_text=_build_script_text(prepared),
        characters=characters,
        scenes=scenes,
        props=props,
        frames=frames,
        video_tasks=[],
        model_settings=ModelSettings(
            character_aspect_ratio="9:16",
            scene_aspect_ratio="9:16",
            prop_aspect_ratio="1:1",
            storyboard_aspect_ratio=prepared.project_draft.aspect_ratio,
        ),
        workflow_mode="r2v",
        default_generation_mode="r2v",
        # PR5 saves a self-contained episode project. The source series ID is
        # retained in the persistence index until series persistence is added.
        series_id=None,
        episode_number=prepared.project_draft.episode_number,
        created_at=0.0,
        updated_at=0.0,
    )


def _build_character(seed: CharacterSeed) -> Character:
    description = " / ".join(
        value
        for value in [
            seed.description,
            f"職業: {seed.occupation}" if seed.occupation else None,
            f"話し方: {seed.speech_style}" if seed.speech_style else None,
            f"外見指示: {seed.visual_prompt}",
            f"避ける要素: {seed.negative_prompt}" if seed.negative_prompt else None,
        ]
        if value
    )
    return Character(
        id=seed.seed_id,
        name=seed.name,
        description=description,
        persona=seed.source_character_id,
        reference_sheet=AssetUnit(
            image_prompt=seed.visual_prompt,
            image_updated_at=0.0,
            video_updated_at=0.0,
        ),
        full_body=AssetUnit(image_updated_at=0.0, video_updated_at=0.0),
        three_views=AssetUnit(image_updated_at=0.0, video_updated_at=0.0),
        head_shot=AssetUnit(image_updated_at=0.0, video_updated_at=0.0),
        full_body_prompt=seed.visual_prompt,
        video_prompt=seed.visual_prompt,
        full_body_updated_at=0.0,
        three_view_updated_at=0.0,
        headshot_updated_at=0.0,
        status=GenerationStatus.PENDING,
    )


def _build_scene(seed: LocationSeed) -> Scene:
    continuity = " / ".join(seed.continuity_rules)
    description = " / ".join(
        value
        for value in [
            seed.description,
            f"継続ルール: {continuity}" if continuity else None,
            f"生成指示: {seed.visual_prompt}",
        ]
        if value
    )
    return Scene(
        id=seed.seed_id,
        name=seed.name,
        description=description,
        time_of_day=seed.time_of_day,
        lighting_mood=continuity or None,
        video_prompt=seed.visual_prompt,
        status=GenerationStatus.PENDING,
    )


def _build_prop(seed: PropSeed) -> Prop:
    return Prop(
        id=seed.seed_id,
        name=seed.name,
        description=(
            f"{seed.story_function} / 生成指示: {seed.visual_prompt}"
        ),
        video_prompt=seed.visual_prompt,
        status=GenerationStatus.PENDING,
    )


def _build_frame(frame: StoryboardFrameDraft, intent: Any | None) -> StoryboardFrame:
    dialogue_text = "\n".join(
        f"{cue.speaker_character_id}: {cue.text}" for cue in frame.dialogue_cues
    ) or None
    first_cue = frame.dialogue_cues[0] if frame.dialogue_cues else None
    sfx = " / ".join(frame.audio.sound_effects) or None
    intent_payload = (
        intent.model_dump(mode="json", exclude_none=True)
        if intent is not None and hasattr(intent, "model_dump")
        else None
    )

    composition_data = {
        "jp_drama": {
            "source_shot_id": frame.source_shot_id,
            "adapted_beat_id": frame.adapted_beat_id,
            "order": frame.order,
            "duration_seconds": frame.duration_seconds,
            "render_intent_id": frame.render_intent_id,
            "dialogue_cues": [
                cue.model_dump(mode="json", exclude_none=True)
                for cue in frame.dialogue_cues
            ],
            "render_intent": intent_payload,
        }
    }

    return StoryboardFrame(
        id=frame.frame_id,
        scene_id=frame.location_seed_id,
        character_ids=list(frame.character_seed_ids),
        prop_ids=list(frame.prop_seed_ids),
        action_description=frame.action,
        dialogue=dialogue_text,
        speaker=first_cue.speaker_character_id if first_cue else None,
        character_acting=frame.action,
        visual_description=frame.visual_description,
        shot_size=_SHOT_SIZE_MAP.get(frame.camera.shot_size, frame.camera.shot_size),
        camera_angle=_CAMERA_ANGLE_MAP.get(frame.camera.angle, frame.camera.angle),
        camera_movement=frame.camera.movement,
        composition_data=composition_data,
        duration=max(1, int(round(frame.duration_seconds))),
        dialogue_structured=_dialogue_structured(first_cue),
        camera_movement_structured=CameraMovementData(
            primary=frame.camera.movement,
            speed=frame.camera.speed,
            description=(
                f"{frame.camera.shot_size} / {frame.camera.angle} / "
                f"{frame.camera.movement}"
            ),
        ),
        audio_note=AudioNote(
            sfx=sfx,
            ambience=frame.audio.ambience,
            bgm_note=frame.audio.bgm_cue,
        ),
        image_prompt=frame.visual_description,
        video_prompt=(
            f"{frame.visual_description}。{frame.action}。"
            f"カメラ: {frame.camera.movement} ({frame.camera.speed})"
        ),
        status=GenerationStatus.PENDING,
        updated_at=0.0,
    )


def _dialogue_structured(cue: DialogueDraft | None) -> DialogueStructured | None:
    if cue is None:
        return None
    return DialogueStructured(
        speaker=cue.speaker_character_id,
        line=cue.text,
        emotion=cue.emotion,
        delivery=cue.delivery,
    )


def _build_script_text(prepared: PreparedEpisode) -> str:
    lines = [
        f"# {prepared.project_draft.title}",
        "",
        prepared.project_draft.description,
        "",
    ]
    for frame in sorted(prepared.storyboard_frame_drafts, key=lambda item: item.order):
        lines.append(
            f"[Shot {frame.order} / {frame.duration_seconds:g}s] {frame.action}"
        )
        for cue in frame.dialogue_cues:
            lines.append(f"{cue.speaker_character_id}: {cue.text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
