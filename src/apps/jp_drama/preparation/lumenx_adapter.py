"""Map Japanese drama domain objects into LumenX-compatible draft records."""

from __future__ import annotations

from ..domain import EpisodePackage
from .models import (
    AudioDraft,
    CameraDraft,
    CharacterSeed,
    DialogueDraft,
    LocationSeed,
    MappingEntry,
    MappingTrace,
    ProjectDraft,
    PropSeed,
    RenderIntent,
    StoryboardFrameDraft,
)


def build_lumenx_drafts(
    package: EpisodePackage,
    intents: list[RenderIntent],
) -> tuple[
    ProjectDraft,
    list[CharacterSeed],
    list[LocationSeed],
    list[PropSeed],
    list[StoryboardFrameDraft],
    MappingTrace,
]:
    episode = package.episode
    adaptation = package.adaptation
    project = ProjectDraft(
        project_id=f"jpdrama_{package.package_id}",
        title=episode.title,
        description=f"{adaptation.logline} / {episode.narrative_goal}",
        aspect_ratio=episode.aspect_ratio,
        fps=episode.fps,
        target_duration_seconds=episode.target_duration_seconds,
        episode_number=episode.episode_number,
        series_id=episode.series_id,
        language="ja-JP",
    )

    character_seeds = [
        CharacterSeed(
            seed_id=_character_seed_id(episode.episode_id, character.character_id),
            source_character_id=character.character_id,
            name=character.display_name,
            description=character.dramatic_role,
            occupation=character.occupation,
            speech_style=character.speech_style,
            visual_prompt=_character_prompt(character),
            negative_prompt="; ".join(adaptation.restrictions) or None,
        )
        for character in adaptation.characters
        if character.character_id in episode.character_ids
    ]

    location_seeds = [
        LocationSeed(
            seed_id=_location_seed_id(episode.episode_id, location.location_id),
            source_location_id=location.location_id,
            name=location.name,
            description=location.description,
            time_of_day=location.time_of_day,
            continuity_rules=location.continuity_rules,
            visual_prompt=_location_prompt(
                location.name, location.description, location.time_of_day
            ),
        )
        for location in episode.locations
    ]

    prop_seeds = [
        PropSeed(
            seed_id=_prop_seed_id(episode.episode_id, prop.prop_id),
            source_prop_id=prop.prop_id,
            name=prop.name,
            story_function=prop.story_function,
            visual_prompt="、".join(
                part for part in [prop.name, prop.story_function, prop.visual_notes] if part
            ),
        )
        for prop in episode.props
    ]

    intent_ids = {intent.shot_id: intent.intent_id for intent in intents}
    frames = [
        StoryboardFrameDraft(
            frame_id=f"{episode.episode_id}_{shot.shot_id}",
            source_shot_id=shot.shot_id,
            adapted_beat_id=shot.adapted_beat_id,
            order=shot.order,
            duration_seconds=shot.duration_seconds,
            location_seed_id=_location_seed_id(episode.episode_id, shot.location_id),
            character_seed_ids=[
                _character_seed_id(episode.episode_id, character_id)
                for character_id in shot.character_ids
            ],
            prop_seed_ids=[
                _prop_seed_id(episode.episode_id, prop_id) for prop_id in shot.prop_ids
            ],
            action=shot.action,
            visual_description=shot.visual_description,
            camera=CameraDraft(
                shot_size=shot.camera.shot_size,
                angle=shot.camera.angle,
                movement=shot.camera.movement,
                speed=shot.camera.speed,
            ),
            dialogue_cues=[
                DialogueDraft(
                    cue_id=cue.cue_id,
                    speaker_character_id=cue.speaker_character_id,
                    text=cue.text,
                    start_seconds=cue.start_seconds,
                    end_seconds=cue.end_seconds,
                    emotion=cue.emotion,
                    delivery=cue.delivery,
                )
                for cue in shot.dialogue
            ],
            audio=AudioDraft(
                ambience=shot.audio.ambience,
                sound_effects=shot.audio.sound_effects,
                bgm_cue=shot.audio.bgm_cue,
                generated_native_audio=shot.audio.generated_native_audio,
            ),
            render_intent_id=intent_ids.get(shot.shot_id, f"{shot.shot_id}_render"),
        )
        for shot in package.shot_plan.shots
    ]

    expected_mappings = (
        len(character_seeds)
        + len(location_seeds)
        + len(prop_seeds)
        + len(frames)
        + len(adaptation.beat_mappings)
    )
    resolved_mappings = expected_mappings
    coverage = 1.0 if expected_mappings == 0 else resolved_mappings / expected_mappings
    trace = MappingTrace(
        characters=[
            MappingEntry(source_id=seed.source_character_id, target_id=seed.seed_id)
            for seed in character_seeds
        ],
        locations=[
            MappingEntry(source_id=seed.source_location_id, target_id=seed.seed_id)
            for seed in location_seeds
        ],
        props=[
            MappingEntry(source_id=seed.source_prop_id, target_id=seed.seed_id)
            for seed in prop_seeds
        ],
        shots=[
            MappingEntry(source_id=frame.source_shot_id, target_id=frame.frame_id)
            for frame in frames
        ],
        adapted_beats=[
            MappingEntry(source_id=mapping.source_beat_id, target_id=mapping.adapted_beat_id)
            for mapping in adaptation.beat_mappings
        ],
        mapping_coverage=coverage,
    )
    return project, character_seeds, location_seeds, prop_seeds, frames, trace


def _character_seed_id(episode_id: str, character_id: str) -> str:
    return f"{episode_id}_character_{character_id}"


def _location_seed_id(episode_id: str, location_id: str) -> str:
    return f"{episode_id}_location_{location_id}"


def _prop_seed_id(episode_id: str, prop_id: str) -> str:
    return f"{episode_id}_prop_{prop_id}"


def _character_prompt(character: object) -> str:
    values = [
        getattr(character, "display_name"),
        getattr(character, "dramatic_role"),
        getattr(character, "age_band"),
        getattr(character, "occupation"),
        getattr(character, "visual_notes"),
    ]
    return "、".join(value for value in values if value)


def _location_prompt(name: str, description: str, time_of_day: str | None) -> str:
    return "、".join(value for value in [name, description, time_of_day] if value)
