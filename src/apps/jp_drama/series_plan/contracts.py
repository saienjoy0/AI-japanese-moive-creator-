"""Safe public wrappers around the low-level series compiler.

The low-level compiler preserves every source state verbatim. These wrappers
scope prop states to the current episode, apply the reviewed asset-catalogue
period style, and rebuild digests before artifacts leave the module.
"""

from __future__ import annotations

from ..generation.models import GenerationPlanEpisode
from ..preparation.models import PreparedEpisode
from ..rendering.provider_registry import ProviderRegistry
from .compiler import (
    build_prepared_episode as _build_prepared_episode,
    compile_episode_generation_plan as _compile_episode_generation_plan,
)
from .models import SeriesAssetCatalog, SeriesGenerationPlan


def build_prepared_episode(
    plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
    episode_id: str,
) -> PreparedEpisode:
    raw = _build_prepared_episode(plan, catalog, episode_id)
    prop_seeds = [
        seed.model_copy(
            update={
                "visual_prompt": _episode_prop_prompt(
                    plan,
                    catalog,
                    seed.source_prop_id,
                    episode_id,
                )
            }
        )
        for seed in raw.prop_seeds
    ]
    location_seeds = [
        seed.model_copy(
            update={
                "continuity_rules": [
                    f"Keep {seed.source_location_id} layout, furniture, windows, and light direction stable",
                    *[
                        rule
                        for rule in catalog.continuity_rules
                        if seed.source_location_id in rule
                    ],
                ]
            }
        )
        for seed in raw.location_seeds
    ]
    frames = [
        frame.model_copy(
            update={
                "visual_description": frame.visual_description.replace(
                    plan.production.visual_style,
                    catalog.visual_style,
                    1,
                )
            }
        )
        for frame in raw.storyboard_frame_drafts
    ]
    return raw.model_copy(
        update={
            "prop_seeds": prop_seeds,
            "location_seeds": location_seeds,
            "storyboard_frame_drafts": frames,
        }
    )


def compile_episode_generation_plan(
    source_plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
    prepared: PreparedEpisode,
    episode_id: str,
    *,
    route_id: str,
    registry: ProviderRegistry,
) -> GenerationPlanEpisode:
    raw = _compile_episode_generation_plan(
        source_plan,
        catalog,
        prepared,
        episode_id,
        route_id=route_id,
        registry=registry,
    )
    contracts = [
        contract.model_copy(
            update={
                "prop_state_locks": {
                    prop_id: _episode_prop_prompt(
                        source_plan,
                        catalog,
                        prop_id,
                        episode_id,
                    )
                    for prop_id in contract.prop_state_locks
                }
            }
        )
        for contract in raw.continuity_contracts
    ]
    payload = raw.model_dump(mode="python", exclude={"content_digest"})
    payload["continuity_contracts"] = contracts
    return GenerationPlanEpisode.build_with_digest(**payload)


def _episode_prop_prompt(
    plan: SeriesGenerationPlan,
    catalog: SeriesAssetCatalog,
    asset_id: str,
    episode_id: str,
) -> str:
    asset = catalog.by_id[asset_id]
    values: list[str] = []
    contract = plan.continuity_contract.prop_state_tracking.get(asset_id)
    last_episode_id = plan.episodes[-1].episode_id
    if contract is not None:
        for key, value in contract.states.items():
            belongs_to_episode = key == episode_id or key.startswith(f"{episode_id}_")
            final_for_last_episode = key == "final" and episode_id == last_episode_id
            if belongs_to_episode or final_for_last_episode:
                values.append(f"{key}: {value}")
    instance_rule = asset.instance_rules.get(episode_id)
    if instance_rule and instance_rule not in values:
        values.append(instance_rule)
    unique_values = list(dict.fromkeys(values))
    return asset.prompt + (
        " Continuity: " + " | ".join(unique_values)
        if unique_values
        else ""
    )
