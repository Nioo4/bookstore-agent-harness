"""The persisted model-call steps are the sole source of a run usage snapshot."""
from __future__ import annotations

import logging

from app.models.provider import MAX_TOKEN_COUNT
from app.trace.types import CostStatus, StepType, TokenUsage, TraceCost, UsageStatus


def aggregate_usage(steps, *, coverage_complete=True, observed_known=False):
    calls = {step.step_id: step for step in steps
             if step.step_type is StepType.SYSTEM and step.node_id.startswith("model:")}
    known = observed_known or any(s.token_usage.prompt is not None or s.token_usage.completion is not None
                                 for s in calls.values())
    if not coverage_complete:
        return TokenUsage(), UsageStatus.PARTIAL if known else UsageStatus.UNAVAILABLE
    if not calls:
        return TokenUsage.no_calls(), UsageStatus.NO_CALLS
    totals = {}
    for field in ("prompt", "completion", "cached", "reasoning"):
        counts = [getattr(s.token_usage, field) for s in calls.values()]
        total = sum(counts) if all(n is not None for n in counts) else None
        if total is not None and total > MAX_TOKEN_COUNT:
            logging.getLogger(__name__).warning("trace_usage_sum_overflow")
            total = None
        totals[field] = total
    usage = TokenUsage(**totals)
    status = UsageStatus.COMPLETE if usage.call_status() is UsageStatus.COMPLETE else (
        UsageStatus.PARTIAL if known else UsageStatus.UNAVAILABLE)
    return usage, status


def complete_cost(cost: TraceCost) -> TraceCost:
    return cost if cost.status is CostStatus.CALCULATED else TraceCost(status=CostStatus.UNAVAILABLE)


def aggregate_complete_cost(steps, pricing_version, *, coverage_complete=True):
    calls = {s.step_id: s for s in steps if s.step_type is StepType.SYSTEM and s.node_id.startswith("model:")}
    if not coverage_complete or any(s.cost.status is not CostStatus.CALCULATED for s in calls.values()):
        return TraceCost(status=CostStatus.UNAVAILABLE)
    return TraceCost(status=CostStatus.CALCULATED,
                     known_microusd=sum(s.cost.known_microusd for s in calls.values()),
                     pricing_version=pricing_version)


async def persisted_steps(repository, run_id):
    steps, after = [], None
    while True:
        page = await repository.list_steps(run_id, after, 200)
        steps.extend(page)
        if len(page) < 200:
            return steps
        after = page[-1].sequence_no
