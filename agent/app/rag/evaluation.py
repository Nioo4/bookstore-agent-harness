from __future__ import annotations

"""Deterministic threshold and retrieval metrics for the frozen RAG corpus."""

from dataclasses import dataclass
from typing import Any


# The evaluator deliberately stops at retrieval and Catalog hard-filtering.
# Keeping this value in the shared RAG module prevents reports and the index
# command from silently using different meanings for their metrics.
RAG_EVALUATION_LAYER = "retrieval_and_filtering"


@dataclass(frozen=True)
class RetrievalObservation:
    """One query's scored results and manually labelled relevant IDs."""

    result_ids: tuple[str, ...]
    scores: tuple[float, ...]
    relevant_ids: frozenset[str] = frozenset()
    must_abstain: bool = False

    def __post_init__(self) -> None:
        if len(self.result_ids) != len(self.scores):
            raise ValueError("result IDs and scores must have equal length")


@dataclass(frozen=True)
class ThresholdSelection:
    threshold: float
    recall_at_5: float
    mrr: float
    false_positive_rate: float
    passed: bool


@dataclass(frozen=True)
class ChunkingObservation:
    """Metrics for one of the three frozen chunk configurations."""

    target_tokens: int
    overlap_tokens: int
    recall_at_5: float
    mrr: float
    average_context_tokens: float
    duplicate_rate: float = 0.0


def select_chunking(observations: list[ChunkingObservation]) -> ChunkingObservation:
    """Select a candidate using the Spec's deterministic tie-break order."""
    allowed = {(256, 32), (384, 48), (500, 80)}
    if not observations:
        raise ValueError("at least one chunking observation is required")
    if any((item.target_tokens, item.overlap_tokens) not in allowed for item in observations):
        raise ValueError("unknown v2 chunk configuration")
    # First maximize Recall@5, then MRR, then minimize context and overlap.
    return sorted(
        observations,
        key=lambda item: (
            -item.recall_at_5,
            -item.mrr,
            item.average_context_tokens,
            item.overlap_tokens,
        ),
    )[0]


def select_threshold(observations: list[RetrievalObservation]) -> ThresholdSelection:
    """Choose only from observed scores, using the Spec's gate and tie-breaks.

    A score is accepted when it is greater than or equal to the threshold.
    A failed gate is returned as ``passed=False`` so callers can produce a
    manifest for debugging without claiming an acceptance pass.
    """
    if not observations:
        raise ValueError("at least one tuning observation is required")
    # A zero threshold is explicitly forbidden in the runtime manifest.  Do
    # not turn an all-zero/empty score set into an apparently valid setting.
    candidates = sorted({score for item in observations for score in item.scores if score > 0}, reverse=True)
    if not candidates:
        return ThresholdSelection(1.0, 0.0, 0.0, 1.0, False)
    scored = [_score_threshold(observations, threshold) for threshold in candidates]
    eligible = [item for item in scored if item.recall_at_5 >= 0.90 and item.false_positive_rate <= 0.05]
    pool = eligible or scored
    # Gate first; only after the gate is satisfied do MRR, false-positive rate
    # and the higher threshold decide.  Recall is the gate, not a secondary
    # optimization that can select a lower-threshold candidate.
    chosen = sorted(
        pool,
        key=lambda item: (item.mrr, -item.false_positive_rate, item.threshold),
        reverse=True,
    )[0]
    return ThresholdSelection(
        threshold=chosen.threshold,
        recall_at_5=chosen.recall_at_5,
        mrr=chosen.mrr,
        false_positive_rate=chosen.false_positive_rate,
        passed=bool(eligible),
    )


def retrieval_metrics(observations: list[RetrievalObservation], *, threshold: float = float("-inf")) -> dict[str, float]:
    selected = _score_threshold(observations, threshold)
    return {
        "recall_at_5": selected.recall_at_5,
        "mrr": selected.mrr,
        "false_positive_rate": selected.false_positive_rate,
    }


def select_threshold_at(observations: list[RetrievalObservation], threshold: float) -> ThresholdSelection:
    """Return document metrics at one explicit candidate threshold."""
    if not observations:
        raise ValueError("at least one tuning observation is required")
    if threshold <= 0:
        raise ValueError("threshold must be greater than 0")
    return _score_threshold(observations, threshold)


def _score_threshold(observations: list[RetrievalObservation], threshold: float) -> ThresholdSelection:
    positives = 0
    hits = 0
    reciprocal_total = 0.0
    negative = 0
    false_positive = 0
    for item in observations:
        ranked = [
            result_id
            for result_id, score in zip(item.result_ids, item.scores, strict=True)
            if score >= threshold
        ]
        if item.must_abstain:
            negative += 1
            false_positive += bool(ranked)
            continue
        positives += 1
        top_five = ranked[:5]
        if item.relevant_ids and any(result_id in item.relevant_ids for result_id in top_five):
            hits += 1
        for rank, result_id in enumerate(ranked, start=1):
            if result_id in item.relevant_ids:
                reciprocal_total += 1.0 / rank
                break
    return ThresholdSelection(
        threshold=threshold,
        recall_at_5=hits / positives if positives else 0.0,
        mrr=reciprocal_total / positives if positives else 0.0,
        false_positive_rate=false_positive / negative if negative else 0.0,
        passed=False,
    )
