from pathlib import Path

from src.apps.jp_drama.rendering.minimax_h3_config import MiniMaxH3ProviderConfig


def test_minimax_h3_defaults_to_china_v2_endpoint() -> None:
    config = MiniMaxH3ProviderConfig()

    assert config.base_url == "https://api.minimaxi.com"
    assert config.submit_url == "https://api.minimaxi.com/v2/video_generation"
    assert (
        config.query_url("task-123")
        == "https://api.minimaxi.com/v2/query/video_generation/task-123"
    )


def test_live_provider_example_uses_china_endpoint() -> None:
    config = MiniMaxH3ProviderConfig.load(
        Path("examples/jp_drama/minimax_h3_live_provider.json")
    )

    assert config.base_url == "https://api.minimaxi.com"
    assert config.model == "MiniMax-H3"
    assert config.resolution == "768P"
    assert config.ratio == "9:16"
