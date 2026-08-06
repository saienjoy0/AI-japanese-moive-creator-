from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.apps.jp_drama.assets.models import (
    AssetReadinessIssue,
    AssetReadinessReport,
)
from src.apps.jp_drama.assets.wan_references import (
    WanMasterReference,
    WanMasterReferenceManifest,
    _master_asset_set_digest,
)
from src.apps.jp_drama.generation.models import PromptBundle
from src.apps.jp_drama.rendering.happyhorse11 import (
    HappyHorse11I2VModel,
    HappyHorse11R2VModel,
)
from src.apps.jp_drama.workflows.render_happyhorse_segment_canary import (
    HappyHorseCanaryError,
    _assert_e01_g01_reference_scope,
    _request_fingerprint,
    add_native_voice_warning,
    allow_provider_native_voice,
    build_happyhorse_prompt,
    build_happyhorse_reference_prompt,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _reference(
    *,
    asset_id: str,
    role: str,
    subject_id: str,
    order: int,
    digest_char: str,
) -> WanMasterReference:
    return WanMasterReference(
        asset_id=asset_id,
        role=role,
        subject_id=subject_id,
        order=order,
        asset_path=f"/tmp/{asset_id}.png",
        asset_sha256="sha256:" + digest_char * 64,
        width=941,
        height=1672,
        generated_by="test",
        operation_id=f"test:{asset_id}",
    )


def _manifest() -> WanMasterReferenceManifest:
    references = [
        _reference(
            asset_id="ref_char_C01",
            role="character_master",
            subject_id="C01",
            order=0,
            digest_char="1",
        ),
        _reference(
            asset_id="ref_loc_S01",
            role="location_master",
            subject_id="S01",
            order=1,
            digest_char="2",
        ),
        _reference(
            asset_id="ref_prop_P03",
            role="prop_master",
            subject_id="P03",
            order=2,
            digest_char="3",
        ),
        _reference(
            asset_id="ref_prop_P04",
            role="prop_master",
            subject_id="P04",
            order=3,
            digest_char="4",
        ),
        _reference(
            asset_id="ref_loc_S05",
            role="location_master",
            subject_id="S05",
            order=4,
            digest_char="5",
        ),
    ]
    return WanMasterReferenceManifest.build_with_digest(
        generation_plan_digest=DIGEST_A,
        master_asset_set_digest=_master_asset_set_digest(references),
        segment_id="E01-G01",
        provider_route_id="wan/i2v",
        references=references,
    )


def _prompt_bundle() -> PromptBundle:
    return PromptBundle(
        narrative_summary="少年が濁った絵具に落胆し、横浜港を思い出す",
        visual_prompt="明治期の教室、未完成の海の絵、短い横浜港の記憶",
        motion_prompt="絵具を混ぜて筆を止め、最後に隣を見る",
        camera_prompt="緩やかな寄り、記憶だけ短い切り替え",
        timed_shot_prompt="0-2秒絵、2-4秒港、4-7秒絵具、7-10秒隣を見る",
        dialogue_prompt="海は、こんな色じゃない",
        audio_prompt="自然な少年の日本語、静かな教室、遠い汽笛",
        negative_constraints=["identity drift", "modern objects"],
    )


def test_official_payload_uses_one_first_frame_and_no_voice_fields() -> None:
    payload = HappyHorse11I2VModel.build_request_payload(
        prompt="少年が自然な日本語で短く話す",
        first_frame_url="oss://bucket/frame.png",
        resolution="720P",
        duration=5,
        watermark=False,
        seed=0,
    )

    assert payload == {
        "model": "happyhorse-1.1-i2v",
        "input": {
            "prompt": "少年が自然な日本語で短く話す",
            "media": [
                {
                    "type": "first_frame",
                    "url": "oss://bucket/frame.png",
                }
            ],
        },
        "parameters": {
            "resolution": "720P",
            "duration": 5,
            "watermark": False,
            "seed": 0,
        },
    }
    canonical = repr(payload)
    assert "voice_id" not in canonical
    assert "audio_url" not in canonical
    assert "ratio" not in payload["parameters"]


def test_r2v_payload_uses_ordered_reference_images_and_vertical_ratio() -> None:
    payload = HappyHorse11R2VModel.build_request_payload(
        prompt="[Image 1] boy; [Image 2] classroom",
        reference_image_urls=[
            "oss://bucket/boy.png",
            "oss://bucket/classroom.png",
        ],
        resolution="720P",
        ratio="9:16",
        duration=10,
        watermark=False,
        seed=7,
    )

    assert payload == {
        "model": "happyhorse-1.1-r2v",
        "input": {
            "prompt": "[Image 1] boy; [Image 2] classroom",
            "media": [
                {"type": "reference_image", "url": "oss://bucket/boy.png"},
                {"type": "reference_image", "url": "oss://bucket/classroom.png"},
            ],
        },
        "parameters": {
            "resolution": "720P",
            "ratio": "9:16",
            "duration": 10,
            "watermark": False,
            "seed": 7,
        },
    }


@pytest.mark.parametrize("count", [0, 10])
def test_r2v_payload_rejects_reference_count_outside_one_to_nine(count: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 9"):
        HappyHorse11R2VModel.build_request_payload(
            prompt="test",
            reference_image_urls=[
                f"https://example.test/{index}.png"
                for index in range(count)
            ],
            resolution="720P",
            ratio="9:16",
            duration=10,
        )


def test_r2v_payload_rejects_non_vertical_canary_ratio() -> None:
    with pytest.raises(ValueError, match="requires ratio 9:16"):
        HappyHorse11R2VModel.build_request_payload(
            prompt="test",
            reference_image_urls=["https://example.test/1.png"],
            resolution="720P",
            ratio="16:9",
            duration=10,
        )


@pytest.mark.parametrize("duration", [2, 16])
def test_official_payload_rejects_unsupported_duration(duration: int) -> None:
    with pytest.raises(ValueError, match="between 3 and 15"):
        HappyHorse11I2VModel.build_request_payload(
            prompt="test",
            first_frame_url="https://example.test/frame.png",
            resolution="720P",
            duration=duration,
        )


def test_official_http_submission_persists_task_id_immediately(monkeypatch) -> None:
    posted: dict[str, Any] = {}
    submitted: list[tuple[str, str | None]] = []

    class FakeResponse:
        status_code = 200
        text = '{"output":{"task_id":"task-123"},"request_id":"request-456"}'
        headers: dict[str, str] = {}

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "output": {"task_id": "task-123"},
                "request_id": "request-456",
            }

    def fake_post(url, *, headers, json, timeout):
        posted.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.get_provider_base_url",
        lambda _provider: "https://workspace.example.test",
    )
    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.requests.post",
        fake_post,
    )

    model = HappyHorse11I2VModel({"params": {}})
    monkeypatch.setattr(
        model,
        "_poll_video_task",
        lambda base, task_id, model_name: "https://example.test/result.mp4",
    )
    model.configure_operation(
        on_task_submitted=lambda task_id, request_id: submitted.append(
            (task_id, request_id)
        )
    )

    result = model._generate_happyhorse_i2v_http(
        prompt="test prompt",
        first_frame_url="oss://bucket/frame.png",
        resolution="720P",
        duration=5,
        watermark=False,
        seed=42,
    )

    assert result == "https://example.test/result.mp4"
    assert submitted == [("task-123", "request-456")]
    assert posted["headers"]["X-DashScope-Async"] == "enable"
    assert posted["headers"]["X-DashScope-OssResourceResolve"] == "enable"
    assert posted["json"]["model"] == "happyhorse-1.1-i2v"


def test_r2v_http_submission_persists_task_id_before_poll(monkeypatch) -> None:
    events: list[str] = []

    class FakeResponse:
        status_code = 200
        text = '{"output":{"task_id":"task-r2v"},"request_id":"request-r2v"}'
        headers: dict[str, str] = {}

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "output": {"task_id": "task-r2v"},
                "request_id": "request-r2v",
            }

    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.get_provider_base_url",
        lambda _provider: "https://workspace.example.test",
    )
    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.requests.post",
        lambda *args, **kwargs: FakeResponse(),
    )

    model = HappyHorse11R2VModel({"params": {}})
    model.configure_operation(
        on_task_submitted=lambda task_id, request_id: events.append(
            f"saved:{task_id}:{request_id}"
        )
    )

    def fake_poll(base, task_id, model_name):
        events.append(f"poll:{task_id}")
        return "https://example.test/r2v.mp4"

    monkeypatch.setattr(model, "_poll_video_task", fake_poll)

    result = model._generate_happyhorse_r2v_http(
        prompt="[Image 1] boy",
        reference_image_urls=["oss://bucket/boy.png"],
        resolution="720P",
        ratio="9:16",
        duration=10,
        watermark=False,
        seed=42,
    )

    assert result == "https://example.test/r2v.mp4"
    assert events == [
        "saved:task-r2v:request-r2v",
        "poll:task-r2v",
    ]


@pytest.mark.parametrize(
    ("model_class", "method_name", "kwargs"),
    [
        (
            HappyHorse11I2VModel,
            "_generate_happyhorse_i2v_http",
            {
                "prompt": "test",
                "first_frame_url": "https://example.test/frame.png",
                "resolution": "720P",
                "duration": 5,
                "watermark": False,
                "seed": None,
            },
        ),
        (
            HappyHorse11R2VModel,
            "_generate_happyhorse_r2v_http",
            {
                "prompt": "test",
                "reference_image_urls": [],
                "resolution": "720P",
                "ratio": "9:16",
                "duration": 10,
                "watermark": False,
                "seed": None,
            },
        ),
    ],
)
def test_resume_uses_saved_task_without_second_paid_submission(
    monkeypatch,
    model_class,
    method_name,
    kwargs,
) -> None:
    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.get_provider_base_url",
        lambda _provider: "https://workspace.example.test",
    )

    def reject_post(*args, **kwargs):
        raise AssertionError("resume must not create a second provider task")

    monkeypatch.setattr(
        "src.apps.jp_drama.rendering.happyhorse11.requests.post",
        reject_post,
    )

    model = model_class({"params": {}})
    model.configure_operation(resume_task_id="task-existing")
    monkeypatch.setattr(
        model,
        "_poll_video_task",
        lambda base, task_id, model_name: (
            "https://example.test/resumed.mp4"
            if task_id == "task-existing"
            else pytest.fail("wrong task ID")
        ),
    )

    result = getattr(model, method_name)(**kwargs)
    assert result == "https://example.test/resumed.mp4"


def test_native_voice_exception_relaxes_only_voice_identity_error() -> None:
    report = AssetReadinessReport(
        stage="render",
        generation_plan_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        selected_segment_ids=["seg-1"],
        required_asset_ids=["first-frame-1"],
        required_voice_character_ids=["char-1"],
        ready=False,
        errors=[
            AssetReadinessIssue(
                code="voice_profile_not_ready",
                severity="error",
                message="speaker has no approved voice identity",
                character_seed_id="char-1",
            )
        ],
    )

    relaxed = allow_provider_native_voice(report)

    assert relaxed.ready is True
    assert relaxed.errors == []
    assert relaxed.required_voice_character_ids == []
    assert [item.code for item in relaxed.warnings] == [
        "provider_native_voice_uncontrolled"
    ]


def test_native_voice_exception_never_relaxes_missing_first_frame() -> None:
    report = AssetReadinessReport(
        stage="render",
        generation_plan_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        selected_segment_ids=["seg-1"],
        required_asset_ids=["first-frame-1"],
        required_voice_character_ids=["char-1"],
        ready=False,
        errors=[
            AssetReadinessIssue(
                code="voice_profile_not_ready",
                severity="error",
                message="speaker has no approved voice identity",
                character_seed_id="char-1",
            ),
            AssetReadinessIssue(
                code="required_asset_not_ready",
                severity="error",
                message="asset is not approved",
                asset_id="first-frame-1",
            ),
        ],
    )

    relaxed = allow_provider_native_voice(report)

    assert relaxed.ready is False
    assert [item.code for item in relaxed.errors] == ["required_asset_not_ready"]


def test_keyframe_readiness_gets_native_voice_warning_without_becoming_blocked() -> None:
    report = AssetReadinessReport(
        stage="keyframe",
        generation_plan_digest=DIGEST_A,
        bundle_digest=DIGEST_B,
        selected_segment_ids=["E01-G01"],
        required_asset_ids=["ref_char_C01"],
        required_voice_character_ids=[],
        ready=True,
    )
    segment = SimpleNamespace(
        segment_id="E01-G01",
        dialogue_slices=[
            SimpleNamespace(speaker_character_id="C01"),
        ],
    )

    updated = add_native_voice_warning(report, segment)

    assert updated.ready is True
    assert [item.code for item in updated.warnings] == [
        "provider_native_voice_uncontrolled"
    ]


def test_prompt_includes_dialogue_audio_and_no_on_screen_text() -> None:
    prompt = build_happyhorse_prompt(_prompt_bundle())

    assert "海は、こんな色じゃない" in prompt
    assert "自然な少年の日本語" in prompt
    assert "Do not add subtitles" in prompt
    assert "identity drift" in prompt


def test_reference_prompt_binds_all_images_in_manifest_order() -> None:
    manifest = _manifest()
    prompt = build_happyhorse_reference_prompt(_prompt_bundle(), manifest)

    for number, subject in enumerate(["C01", "S01", "P03", "P04", "S05"], start=1):
        assert f"[Image {number}]" in prompt
        assert subject in prompt
    positions = [prompt.index(f"[Image {number}]") for number in range(1, 6)]
    assert positions == sorted(positions)


def test_e01_g01_requires_harbor_memory_reference() -> None:
    manifest = _manifest()
    missing_s05 = WanMasterReferenceManifest.build_with_digest(
        generation_plan_digest=manifest.generation_plan_digest,
        master_asset_set_digest=_master_asset_set_digest(manifest.references[:-1]),
        segment_id=manifest.segment_id,
        provider_route_id=manifest.provider_route_id,
        references=manifest.references[:-1],
    )

    with pytest.raises(HappyHorseCanaryError, match="S05"):
        _assert_e01_g01_reference_scope(
            SimpleNamespace(segment_id="E01-G01"),
            missing_s05,
        )


def test_request_fingerprint_changes_when_reference_order_changes() -> None:
    base = {
        "protocol": "happyhorse-1.1-r2v-official-canary-v1",
        "model_name": "happyhorse-1.1-r2v",
        "plan_digest": DIGEST_A,
        "bundle_digest": DIGEST_B,
        "segment_id": "E01-G01",
        "prompt": "test",
        "resolution": "720P",
        "ratio": "9:16",
        "duration": 10,
        "seed": 7,
    }
    first = _request_fingerprint(
        **base,
        ordered_asset_ids=["a", "b"],
        ordered_asset_hashes=["sha256:" + "1" * 64, "sha256:" + "2" * 64],
    )
    second = _request_fingerprint(
        **base,
        ordered_asset_ids=["b", "a"],
        ordered_asset_hashes=["sha256:" + "2" * 64, "sha256:" + "1" * 64],
    )

    assert first != second
