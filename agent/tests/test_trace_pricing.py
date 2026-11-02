from pathlib import Path

from app.models.provider import ModelUsage
from app.trace.manifests import VersionRegistry
from app.trace.pricing import PricingCalculator, aggregate_cost
from app.trace.types import CostStatus, TraceCost


def calculator() -> PricingCalculator:
    root = Path(__file__).parents[1]
    versions = VersionRegistry.load(
        root=root,
        prompt_manifest_path=root / "prompts/manifest.yaml",
        model_manifest_path=root / "config/model-profiles.yaml",
        pricing_manifest_path=root / "pricing/pricing-v1.json",
    )
    return PricingCalculator(versions.pricing_manifest)


def test_pricing_calculated_partial_and_unavailable_are_distinct() -> None:
    pricing = calculator()
    usage = ModelUsage(
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        cached_tokens=1_000_000,
        reasoning_tokens=1_000_000,
        reported=True,
    )

    calculated = pricing.price("priced-model", usage)
    partial = pricing.price("partial-model", usage)
    unavailable_model = pricing.price("unlisted-model", usage)
    unavailable_usage = pricing.price("priced-model", ModelUsage())

    assert (calculated.status, calculated.known_microusd) == (
        CostStatus.CALCULATED,
        6_500_000,
    )
    assert (partial.status, partial.known_microusd) == (CostStatus.PARTIAL, 3_000_000)
    assert unavailable_model.status is CostStatus.UNAVAILABLE
    assert unavailable_usage.status is CostStatus.UNAVAILABLE


def test_cost_aggregation_never_labels_known_subtotal_as_total() -> None:
    calculated = TraceCost(
        status=CostStatus.CALCULATED,
        known_microusd=100,
        pricing_version="pricing-v1",
    )
    unavailable = TraceCost(status=CostStatus.UNAVAILABLE)

    assert aggregate_cost([calculated], "pricing-v1").status is CostStatus.CALCULATED
    subtotal = aggregate_cost([calculated, unavailable], "pricing-v1")
    assert (subtotal.status, subtotal.known_microusd) == (CostStatus.PARTIAL, 100)
