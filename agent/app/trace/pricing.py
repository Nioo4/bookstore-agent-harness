from __future__ import annotations

from dataclasses import dataclass

from app.models.provider import ModelUsage
from app.trace.manifests import PricingManifest
from app.trace.types import CostStatus, TraceCost


@dataclass(frozen=True)
class PricedUsage:
    usage: ModelUsage
    cost: TraceCost


class PricingCalculator:
    def __init__(self, manifest: PricingManifest) -> None:
        self.manifest = manifest

    def price(self, model: str, usage: ModelUsage) -> TraceCost:
        if not usage.reported:
            return TraceCost(status=CostStatus.UNAVAILABLE)
        price = self.manifest.models.get(model)
        if price is None:
            return TraceCost(status=CostStatus.UNAVAILABLE)
        known = usage.prompt_tokens * price.input_microusd_per_million
        known += usage.completion_tokens * price.output_microusd_per_million
        partial = False
        if usage.cached_tokens is not None:
            if price.cached_microusd_per_million is None:
                partial = True
            else:
                known += usage.cached_tokens * price.cached_microusd_per_million
        if usage.reasoning_tokens is not None:
            if price.reasoning_microusd_per_million is None:
                partial = True
            else:
                known += usage.reasoning_tokens * price.reasoning_microusd_per_million
        # Rates are microusd per one million tokens; integer division deliberately avoids floating point.
        amount = known // 1_000_000
        return TraceCost(
            status=CostStatus.PARTIAL if partial else CostStatus.CALCULATED,
            known_microusd=amount,
            pricing_version=self.manifest.pricing_version,
        )


def aggregate_cost(costs: list[TraceCost], pricing_version: str) -> TraceCost:
    known = [cost.known_microusd for cost in costs if cost.known_microusd is not None]
    if not known:
        return TraceCost(status=CostStatus.UNAVAILABLE)
    fully_calculated = len(known) == len(costs) and all(
        cost.status is CostStatus.CALCULATED for cost in costs
    )
    return TraceCost(
        status=CostStatus.CALCULATED if fully_calculated else CostStatus.PARTIAL,
        known_microusd=sum(known),
        pricing_version=pricing_version,
    )
