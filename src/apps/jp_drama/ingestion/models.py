"""Strict intermediate contracts for normal Japanese script ingestion."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCRIPT_INGESTION_SCHEMA_VERSION = "1.0.0"


class IngestionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ScriptDialogueDraft(IngestionModel):
    speaker_character_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    text: str = Field(min_length=1, max_length=2000)
    emotion: str | None = Field(default=None, max_length=100)
    delivery: str | None = Field(default=None, max_length=500)


class ScriptCharacterDraft(IngestionModel):
    character_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    occupation: str | None = Field(default=None, max_length=200)
    speech_style: str | None = Field(default=None, max_length=1000)
    visual_traits: list[str] = Field(default_factory=list, max_length=20)


class ScriptSceneDraft(IngestionModel):
    scene_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    location_name: str = Field(min_length=1, max_length=300)
    time_of_day: str | None = Field(default=None, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    continuity_rules: list[str] = Field(default_factory=list, max_length=20)


class ScriptBeatDraft(IngestionModel):
    beat_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    order: int = Field(ge=1, le=50)
    scene_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    summary: str = Field(min_length=1, max_length=2000)
    action: str = Field(min_length=1, max_length=2000)
    character_ids: list[str] = Field(min_length=1, max_length=12)
    dialogue: list[ScriptDialogueDraft] = Field(default_factory=list, max_length=6)
    camera_hint: str | None = Field(default=None, max_length=500)
    ambience: str | None = Field(default=None, max_length=1000)
    sound_effects: list[str] = Field(default_factory=list, max_length=10)


class StructuredScriptDraft(IngestionModel):
    schema_version: Literal[SCRIPT_INGESTION_SCHEMA_VERSION] = (
        SCRIPT_INGESTION_SCHEMA_VERSION
    )
    title: str = Field(min_length=1, max_length=500)
    synopsis: str = Field(min_length=1, max_length=4000)
    target_duration_seconds: float = Field(default=45.0, ge=30.0, le=90.0)
    characters: list[ScriptCharacterDraft] = Field(min_length=1, max_length=12)
    scenes: list[ScriptSceneDraft] = Field(min_length=1, max_length=12)
    beats: list[ScriptBeatDraft] = Field(min_length=3, max_length=30)
    unresolved_items: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_references(self) -> "StructuredScriptDraft":
        character_ids = [item.character_id for item in self.characters]
        if len(character_ids) != len(set(character_ids)):
            raise ValueError("structured script character IDs must be unique")

        scene_ids = [item.scene_id for item in self.scenes]
        if len(scene_ids) != len(set(scene_ids)):
            raise ValueError("structured script scene IDs must be unique")

        beat_ids = [item.beat_id for item in self.beats]
        if len(beat_ids) != len(set(beat_ids)):
            raise ValueError("structured script beat IDs must be unique")

        expected_orders = list(range(1, len(self.beats) + 1))
        actual_orders = [item.order for item in self.beats]
        if actual_orders != expected_orders:
            raise ValueError(
                f"structured script beat order must be contiguous: {expected_orders}"
            )

        known_characters = set(character_ids)
        known_scenes = set(scene_ids)
        for beat in self.beats:
            if beat.scene_id not in known_scenes:
                raise ValueError(
                    f"beat {beat.beat_id} references unknown scene {beat.scene_id}"
                )
            unknown_characters = set(beat.character_ids) - known_characters
            if unknown_characters:
                raise ValueError(
                    f"beat {beat.beat_id} references unknown characters: "
                    f"{sorted(unknown_characters)}"
                )
            for dialogue in beat.dialogue:
                if dialogue.speaker_character_id not in beat.character_ids:
                    raise ValueError(
                        f"dialogue speaker {dialogue.speaker_character_id} is not "
                        f"present in beat {beat.beat_id}"
                    )

        minimum_beats = int(
            (self.target_duration_seconds + 19.999999) // 20
        )
        if len(self.beats) < minimum_beats:
            raise ValueError(
                f"{self.target_duration_seconds}s requires at least "
                f"{minimum_beats} beats so every shot stays within 20 seconds"
            )
        return self


class IngestionIssue(IngestionModel):
    code: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4000)
    field: str | None = Field(default=None, max_length=500)


class IngestionReport(IngestionModel):
    schema_version: Literal[SCRIPT_INGESTION_SCHEMA_VERSION] = (
        SCRIPT_INGESTION_SCHEMA_VERSION
    )
    source_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    llm_provider: str = Field(min_length=1)
    attempts: int = Field(ge=1, le=2)
    repaired: bool
    valid: bool
    external_api_calls: int = Field(ge=0, le=2)
    errors: list[IngestionIssue] = Field(default_factory=list)
    warnings: list[IngestionIssue] = Field(default_factory=list)
