from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.apps.jp_drama import EpisodePackage


@pytest.fixture()
def valid_payload() -> dict:
    return {
        "package_id": "pkg_001",
        "source": {
            "source_id": "source_001",
            "kind": "manual_text",
            "original_language": "zh-CN",
            "title": "身分を隠した社長",
            "synopsis": "身なりを理由に見下された客が、実は店の新責任者だった。",
            "rights_status": "reference_only",
            "usage_constraints": ["固有名詞と台詞を流用しない"],
            "assets": [],
        },
        "beat_sheet": {
            "beat_sheet_id": "beats_001",
            "source_id": "source_001",
            "core_premise": "見下された人物の正体が明かされる逆転劇",
            "audience_hook": "冒頭で入店を拒否される",
            "emotional_promise": "理不尽から一気に形勢逆転する爽快感",
            "beats": [
                {
                    "beat_id": "beat_01",
                    "order": 1,
                    "beat_type": "hook",
                    "summary": "客が服装を理由に止められる",
                },
                {
                    "beat_id": "beat_02",
                    "order": 2,
                    "beat_type": "escalation",
                    "summary": "店員が客を追い返そうとする",
                },
                {
                    "beat_id": "beat_03",
                    "order": 3,
                    "beat_type": "reversal",
                    "summary": "客が本部からの辞令を示す",
                },
                {
                    "beat_id": "beat_04",
                    "order": 4,
                    "beat_type": "cliffhanger",
                    "summary": "客が店の改革を宣言する",
                },
            ],
            "extracted_character_roles": ["見下される主人公", "高圧的な店員"],
            "success_signals": ["冒頭3秒の対立", "終盤の身分逆転"],
        },
        "adaptation": {
            "adaptation_id": "adapt_001",
            "source_id": "source_001",
            "beat_sheet_id": "beats_001",
            "working_title": "新店長はその客でした",
            "logline": "古びた服の客を追い返した店員が、客の本当の肩書きを知る。",
            "target_audience": "20〜40代の縦型ショートドラマ視聴者",
            "setting": "埼玉県内の郊外型コンビニ",
            "characters": [
                {
                    "character_id": "char_mio",
                    "display_name": "水野美緒",
                    "dramatic_role": "正体を隠した新店長",
                    "occupation": "エリアマネージャー",
                    "speech_style": "静かで端的",
                },
                {
                    "character_id": "char_ota",
                    "display_name": "太田健",
                    "dramatic_role": "客を見下す店員",
                    "occupation": "コンビニ店員",
                    "speech_style": "早口で高圧的",
                },
            ],
            "beat_mappings": [
                {
                    "source_beat_id": "beat_01",
                    "adapted_beat_id": "jp_beat_01",
                    "adapted_summary": "雨に濡れた美緒が入店を止められる",
                    "transformation_notes": "舞台を日本のコンビニへ変更",
                },
                {
                    "source_beat_id": "beat_02",
                    "adapted_beat_id": "jp_beat_02",
                    "adapted_summary": "太田が防犯を理由に退店を迫る",
                    "transformation_notes": "侮辱を現実的な接客トラブルへ変更",
                },
                {
                    "source_beat_id": "beat_03",
                    "adapted_beat_id": "jp_beat_03",
                    "adapted_summary": "美緒が本部の辞令を見せる",
                    "transformation_notes": "身分設定を日本企業の人事異動へ変更",
                },
                {
                    "source_beat_id": "beat_04",
                    "adapted_beat_id": "jp_beat_04",
                    "adapted_summary": "美緒が最初の監査対象を告げる",
                    "transformation_notes": "次話へつながる監査フックを追加",
                },
            ],
            "cultural_changes": [
                {
                    "category": "setting",
                    "original_element": "高級店",
                    "japanese_element": "郊外型コンビニ",
                    "reason": "日本の日常生活に近づける",
                }
            ],
            "originality": {
                "similarity_risk": "low",
                "retained_elements": ["立場逆転という抽象的な構造"],
                "transformed_elements": ["人物", "舞台", "制度", "台詞", "結末"],
                "prohibited_copy_elements": ["元作品の固有名詞", "特徴的な決め台詞"],
                "approved": True,
            },
            "restrictions": ["実在企業のロゴを出さない"],
        },
        "episode": {
            "episode_id": "episode_001",
            "adaptation_id": "adapt_001",
            "series_id": "series_store_reversal",
            "episode_number": 1,
            "title": "その客、新店長です",
            "target_duration_seconds": 45,
            "aspect_ratio": "9:16",
            "fps": 30,
            "narrative_goal": "接客トラブルから正体の開示までを45秒で見せる",
            "opening_hook_text": "その格好で、店には入れません。",
            "closing_hook_text": "では、最初の監査を始めます。",
            "character_ids": ["char_mio", "char_ota"],
            "locations": [
                {
                    "location_id": "loc_store",
                    "name": "郊外型コンビニ",
                    "description": "雨の夜、白い蛍光灯の店内",
                    "time_of_day": "night",
                    "continuity_rules": ["入口の床は濡れている"],
                }
            ],
            "props": [
                {
                    "prop_id": "prop_letter",
                    "name": "本部の辞令",
                    "story_function": "主人公の正体を証明する",
                }
            ],
        },
        "shot_plan": {
            "shot_plan_id": "shots_001",
            "episode_id": "episode_001",
            "target_duration_seconds": 45,
            "duration_tolerance_seconds": 0.5,
            "shots": [
                _shot(
                    "shot_01",
                    1,
                    "jp_beat_01",
                    10,
                    "char_ota",
                    "その格好で、店には入れません。",
                    "video_plus_tts",
                ),
                _shot(
                    "shot_02",
                    2,
                    "jp_beat_02",
                    10,
                    "char_ota",
                    "ほかのお客様が不安になるので。",
                    "video_plus_tts",
                ),
                _shot(
                    "shot_03",
                    3,
                    "jp_beat_03",
                    15,
                    "char_mio",
                    "私は今日から、この店の責任者です。",
                    "native_av",
                    prop_ids=["prop_letter"],
                ),
                _shot(
                    "shot_04",
                    4,
                    "jp_beat_04",
                    10,
                    "char_mio",
                    "では、最初の監査を始めます。",
                    "video_plus_tts",
                    prop_ids=["prop_letter"],
                ),
            ],
        },
        "cost_plan": {
            "cost_plan_id": "cost_001",
            "episode_id": "episode_001",
            "currency": "JPY",
            "budget_limit": 2700,
            "contingency_rate": 0.1,
            "hard_stop": True,
            "shot_estimates": [
                _cost("shot_01", "video_plus_tts", 350, 150),
                _cost("shot_02", "video_plus_tts", 350, 150),
                _cost("shot_03", "native_av", 650, 250),
                _cost("shot_04", "video_plus_tts", 350, 100),
            ],
        },
    }


def _shot(
    shot_id: str,
    order: int,
    adapted_beat_id: str,
    duration: float,
    speaker: str,
    text: str,
    strategy: str,
    *,
    prop_ids: list[str] | None = None,
) -> dict:
    return {
        "shot_id": shot_id,
        "order": order,
        "adapted_beat_id": adapted_beat_id,
        "duration_seconds": duration,
        "location_id": "loc_store",
        "character_ids": ["char_mio", "char_ota"],
        "prop_ids": prop_ids or [],
        "action": "人物が対立し、視線と小道具で次の情報を示す。",
        "visual_description": "縦型画面で表情と手元を明確に見せる。",
        "dialogue": [
            {
                "cue_id": f"cue_{order:02d}",
                "speaker_character_id": speaker,
                "text": text,
                "start_seconds": 1,
                "end_seconds": min(5, duration),
            }
        ],
        "camera": {
            "shot_size": "medium",
            "angle": "eye_level",
            "movement": "push_in",
            "speed": "normal",
        },
        "audio": {
            "ambience": "雨音と店内の冷蔵庫音",
            "sound_effects": [],
            "bgm_cue": "緊張を維持",
        },
        "render_strategy": strategy,
    }


def _cost(
    shot_id: str,
    strategy: str,
    primary: int,
    retry: int,
) -> dict:
    return {
        "shot_id": shot_id,
        "render_strategy": strategy,
        "provider": "provider-a",
        "model": "model-v1",
        "estimated_primary_cost": primary,
        "reserved_retry_cost": retry,
        "max_attempts": 2,
        "fallback_strategy": "still_motion",
    }


def test_valid_package_round_trips_as_canonical_json(valid_payload: dict) -> None:
    package = EpisodePackage.model_validate(valid_payload)

    assert package.shot_plan.total_duration_seconds == 45
    assert package.cost_plan.subtotal == Decimal("2350")
    assert package.cost_plan.estimated_total == Decimal("2585.00")
    assert package.cost_plan.within_budget is True

    canonical = package.to_canonical_json()
    restored = EpisodePackage.model_validate_json(canonical)
    assert restored.package_id == package.package_id
    assert json.loads(canonical)["schema_version"] == "1.0.0"


def test_json_schema_contains_all_pipeline_stages() -> None:
    schema = EpisodePackage.model_json_schema()
    required = set(schema["required"])

    assert {
        "source",
        "beat_sheet",
        "adaptation",
        "episode",
        "shot_plan",
        "cost_plan",
    } <= required


def test_adaptation_must_cover_every_source_beat(valid_payload: dict) -> None:
    broken = deepcopy(valid_payload)
    broken["adaptation"]["beat_mappings"].pop()

    with pytest.raises(ValidationError, match="must cover the beat sheet"):
        EpisodePackage.model_validate(broken)


def test_shot_duration_must_match_episode_target(valid_payload: dict) -> None:
    broken = deepcopy(valid_payload)
    broken["shot_plan"]["shots"][0]["duration_seconds"] = 5

    with pytest.raises(ValidationError, match="shot durations do not match"):
        EpisodePackage.model_validate(broken)


def test_dialogue_speaker_must_be_present_in_shot(valid_payload: dict) -> None:
    broken = deepcopy(valid_payload)
    broken["shot_plan"]["shots"][0]["dialogue"][0][
        "speaker_character_id"
    ] = "char_missing"

    with pytest.raises(ValidationError, match="is not in shot characters"):
        EpisodePackage.model_validate(broken)


def test_cost_plan_must_cover_every_shot(valid_payload: dict) -> None:
    broken = deepcopy(valid_payload)
    broken["cost_plan"]["shot_estimates"].pop()

    with pytest.raises(ValidationError, match="cost plan must cover every shot"):
        EpisodePackage.model_validate(broken)


def test_hard_budget_stop_rejects_overage(valid_payload: dict) -> None:
    broken = deepcopy(valid_payload)
    broken["cost_plan"]["budget_limit"] = 2000

    with pytest.raises(ValidationError, match="exceeds budget"):
        EpisodePackage.model_validate(broken)


def test_soft_budget_allows_overage_but_reports_status(valid_payload: dict) -> None:
    soft = deepcopy(valid_payload)
    soft["cost_plan"]["budget_limit"] = 2000
    soft["cost_plan"]["hard_stop"] = False

    package = EpisodePackage.model_validate(soft)
    assert package.cost_plan.within_budget is False


def test_high_similarity_adaptation_cannot_be_approved(valid_payload: dict) -> None:
    broken = deepcopy(valid_payload)
    broken["adaptation"]["originality"]["similarity_risk"] = "high"

    with pytest.raises(ValidationError, match="high-similarity"):
        EpisodePackage.model_validate(broken)


def test_extra_fields_are_rejected(valid_payload: dict) -> None:
    broken = deepcopy(valid_payload)
    broken["episode"]["unexpected"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EpisodePackage.model_validate(broken)
