from pathlib import Path

from src.apps.jp_drama import EpisodePackage


EXAMPLE_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "jp_drama"
    / "minimal_episode_package.json"
)


def test_checked_in_example_is_valid_and_round_trips() -> None:
    package = EpisodePackage.model_validate_json(
        EXAMPLE_PATH.read_text(encoding="utf-8")
    )

    assert package.package_id == "pkg_example_001"
    assert package.shot_plan.total_duration_seconds == 45
    assert package.cost_plan.within_budget is True

    restored = EpisodePackage.model_validate_json(package.to_canonical_json())
    assert restored.package_id == package.package_id
