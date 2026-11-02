"""Versioned RAG-v2 evaluation entry point.

The fixture path is deterministic and provider-free.  The live path is an
explicit operation: it embeds the frozen cases, queries temporary Qdrant
collections, and always reports keyword/full-context/RAG baselines on the same
cases. It never changes online aliases or writes a runtime manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.embeddings import EmbeddingProvider, EmbeddingResponse
from app.rag.contracts import RagChunk, RagDocumentLoader, source_tree_hashes
from app.rag.evaluation import (
    ChunkingObservation,
    RAG_EVALUATION_LAYER,
    RetrievalObservation,
    retrieval_metrics,
    select_chunking,
    select_threshold,
    select_threshold_at,
)
from app.rag.indexer import embedding_space_identity
from app.rag.qdrant import QdrantRagStore


class RagEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    corpus: Literal["POLICY", "BOOK_GUIDE"]
    query: str = Field(min_length=1)
    catalog_filters: dict[str, Any] = Field(default_factory=dict)
    expected_document_ids: list[str] = Field(default_factory=list)
    expected_sections: list[str] = Field(default_factory=list)
    allowed_catalog_refs: list[str] = Field(default_factory=list)
    forbidden_catalog_refs: list[str] = Field(default_factory=list)
    allowed_book_ids: list[int] = Field(default_factory=list)
    forbidden_book_ids: list[int] = Field(default_factory=list)
    must_abstain: bool = False

    @model_validator(mode="after")
    def validate_contract(self) -> "RagEvalCase":
        if len(self.expected_document_ids) != len(set(self.expected_document_ids)):
            raise ValueError("expected_document_ids must be unique")
        if len(self.expected_sections) != len(set(self.expected_sections)):
            raise ValueError("expected_sections must be unique")
        if not self.must_abstain:
            if not self.expected_document_ids:
                raise ValueError("non-abstention case requires expected_document_ids")
            if not self.expected_sections:
                raise ValueError("non-abstention case requires expected_sections")
        prefix = "policy-" if self.corpus == "POLICY" else "book-guide-"
        if any(not value.startswith(prefix) for value in self.expected_document_ids):
            raise ValueError("expected_document_ids do not match corpus")
        if set(self.allowed_catalog_refs) & set(self.forbidden_catalog_refs):
            raise ValueError("allowed_catalog_refs and forbidden_catalog_refs overlap")
        if set(self.allowed_book_ids) & set(self.forbidden_book_ids):
            raise ValueError("allowed_book_ids and forbidden_book_ids overlap")
        permitted_filters = {"q", "author", "category", "min_price", "max_price", "language", "in_stock"}
        unknown = set(self.catalog_filters) - permitted_filters
        if unknown:
            raise ValueError(f"unknown catalog_filters: {sorted(unknown)}")
        low = self.catalog_filters.get("min_price")
        high = self.catalog_filters.get("max_price")
        if low is not None and (type(low) is not int or low < 0):
            raise ValueError("catalog_filters.min_price must be a non-negative integer")
        if high is not None and (type(high) is not int or high < 0):
            raise ValueError("catalog_filters.max_price must be a non-negative integer")
        if low is not None and high is not None and low > high:
            raise ValueError("catalog_filters.min_price must not exceed max_price")
        if "in_stock" in self.catalog_filters and type(self.catalog_filters["in_stock"]) is not bool:
            raise ValueError("catalog_filters.in_stock must be boolean")
        if any(type(item) is not int or item <= 0 for item in self.allowed_book_ids + self.forbidden_book_ids):
            raise ValueError("book IDs in an evaluation case must be positive integers")
        return self


class RagFixtureResult(BaseModel):
    """One frozen Qdrant chunk hit, not an already-aggregated document hit."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    section_path: str = Field(min_length=1)
    # The key is required even for POLICY (where its value must be null).  A
    # missing catalog identity would make a BOOK_GUIDE fixture unverifiable.
    catalog_ref: str | None
    score: float


@dataclass(frozen=True)
class RetrievedChunk:
    document_id: str
    section_path: str
    catalog_ref: str | None
    score: float


def _require_all_baseline(value: str) -> None:
    """Reject misleading single-baseline labels before any provider call."""
    if value != "all":
        raise ValueError(
            "RAG v2 evaluation always runs keyword, full-context, and RAG together; "
            "--baseline only accepts all"
        )


def load_cases(path: Path) -> list[RagEvalCase]:
    cases: list[RagEvalCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(RagEvalCase.model_validate_json(line))
        except Exception as exc:
            raise ValueError(f"invalid RAG evaluation JSONL at line {line_number}: {exc}") from None
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate RAG evaluation case_id")
    return cases


def validate_split(path: Path, *, expected_count: int) -> list[RagEvalCase]:
    cases = load_cases(path)
    if len(cases) != expected_count:
        raise ValueError(f"RAG split must contain {expected_count} cases, got {len(cases)}")
    return cases


def _load_fixture(path: Path, cases: Iterable[RagEvalCase]) -> dict[str, list[RagFixtureResult]]:
    expected = {case.case_id for case in cases}
    fixture: dict[str, list[RagFixtureResult]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid RAG fixture JSON at line {line_number}: {exc}") from None
        if not isinstance(row, dict) or not isinstance(row.get("case_id"), str):
            raise ValueError(f"RAG fixture line {line_number} must contain case_id")
        case_id = row["case_id"]
        if case_id not in expected:
            raise ValueError(f"RAG fixture contains unknown case_id: {case_id}")
        if case_id in fixture:
            raise ValueError(f"duplicate RAG fixture case_id: {case_id}")
        if set(row) != {"case_id", "results"}:
            raise ValueError(
                f"RAG fixture {case_id} must use exactly case_id/results; "
                "legacy result_ids/scores fixtures are not accepted"
            )
        if not isinstance(row["results"], list):
            raise ValueError(f"RAG fixture {case_id} results must be a list")
        try:
            parsed = [RagFixtureResult.model_validate(item) for item in row["results"]]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid RAG fixture results for {case_id}: {exc}") from None
        if len(parsed) > 12:
            raise ValueError(f"RAG fixture {case_id} has more than 12 chunk results")
        keys = [(item.document_id, item.section_path) for item in parsed]
        if len(keys) != len(set(keys)):
            raise ValueError(f"RAG fixture {case_id} contains duplicate document/section results")
        for item in parsed:
            if not math.isfinite(item.score) or not -1.0 <= item.score <= 1.0:
                raise ValueError(f"RAG fixture {case_id} score must be within cosine range [-1, 1]")
        fixture[case_id] = parsed
    missing = sorted(expected - set(fixture))
    if missing:
        raise ValueError(f"RAG fixture is missing cases: {missing}")
    return fixture


def _load_eval_documents() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    return {
        document.document_id: document
        for document in RagDocumentLoader().load_corpus(root / "knowledge", "all")
    }


def _load_manual_catalog() -> dict[str, dict[str, Any]]:
    """Load the immutable synthetic Catalog snapshot used by fixture checks."""
    path = Path(__file__).resolve().parents[2] / "deploy" / "datasets" / "manual-v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    records: dict[str, dict[str, Any]] = {}
    for item in raw.get("books", []):
        if not isinstance(item, dict) or not isinstance(item.get("alias"), str):
            continue
        record = dict(item)
        record["book_id"] = int(str(item["alias"])[1:])
        record["available_stock"] = item.get("available_stock", item.get("stock", 0))
        records[str(item["alias"])] = record
    return records


def _dataset_hash(path: Path) -> str:
    canonical = "\n".join(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _knowledge_source_hash(documents: dict[str, Any], loader: RagDocumentLoader) -> str:
    chunks: list[RagChunk] = []
    for document in documents.values():
        # book_id is deliberately absent from source_tree_hash material and
        # point IDs, so a stable synthetic ID is sufficient for evaluation.
        chunks.extend(loader.chunks_for(document, book_id=1 if document.document_type == "BOOK_GUIDE" else None))
    return source_tree_hashes(chunks)[2]


def _finalize_report(result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    payload.pop("report_hash", None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["report_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def _write_create_only_report(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    report = _finalize_report(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    finally:
        if descriptor != -1:
            os.close(descriptor)
    return report


def _document_sections(document: Any) -> set[str]:
    return {section for section, _ in RagDocumentLoader._sections(document.body)}


def _validate_fixture_semantics(
    cases: list[RagEvalCase],
    fixture: dict[str, list[RagFixtureResult]],
    documents: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> None:
    for case in cases:
        results = fixture[case.case_id]
        if case.must_abstain and results:
            raise ValueError(f"RAG fixture {case.case_id} must abstain with an empty results list")
        expected_sections = set(case.expected_sections)
        for result in results:
            document = documents.get(result.document_id)
            if document is None or document.document_type != case.corpus:
                raise ValueError(f"RAG fixture {case.case_id} references an unknown or wrong-corpus document")
            if result.section_path not in _document_sections(document):
                raise ValueError(f"RAG fixture {case.case_id} references an unknown section_path")
            if result.section_path not in expected_sections:
                raise ValueError(f"RAG fixture {case.case_id} returned an unexpected section_path")
            if case.corpus == "POLICY":
                if result.catalog_ref is not None:
                    raise ValueError(f"RAG fixture {case.case_id} policy result must have catalog_ref=null")
            else:
                if not result.catalog_ref:
                    raise ValueError(f"RAG fixture {case.case_id} book result requires catalog_ref")
                if document.catalog_ref != result.catalog_ref:
                    raise ValueError(f"RAG fixture {case.case_id} catalog_ref does not match document")
                if not _catalog_ref_allowed(case, result.catalog_ref, catalog):
                    raise ValueError(f"RAG fixture {case.case_id} violates Catalog hard filters")


def _aggregate_chunk_hits(
    case: RagEvalCase,
    hits: Iterable[RetrievedChunk],
    *,
    score_threshold: float = float("-inf"),
) -> RetrievalObservation:
    """Keep chunk hits for audit, then aggregate each document by max score."""
    selected = [hit for hit in hits if hit.score >= score_threshold]
    best: dict[str, tuple[float, int]] = {}
    for index, hit in enumerate(selected):
        current = best.get(hit.document_id)
        if current is None or hit.score > current[0]:
            best[hit.document_id] = (hit.score, index)
    ranked = sorted(best.items(), key=lambda item: (-item[1][0], item[0]))
    return RetrievalObservation(
        result_ids=tuple(document_id for document_id, _ in ranked[:12]),
        scores=tuple(score for _, (score, _) in ranked[:12]),
        relevant_ids=frozenset(case.expected_document_ids),
        must_abstain=case.must_abstain,
    )


def _quality_metrics(
    cases: list[RagEvalCase],
    chunks_by_case: dict[str, list[RetrievedChunk]],
    *,
    thresholds: dict[str, float] | None = None,
    catalog: dict[str, dict[str, Any]] | None = None,
) -> dict[str, float | int]:
    section_total = 0
    section_hit = 0
    forbidden_hits = 0
    hard_filter_violations = 0
    for case in cases:
        threshold = (thresholds or {}).get(case.corpus, float("-inf"))
        hits = [hit for hit in chunks_by_case.get(case.case_id, []) if hit.score >= threshold]
        expected_sections = set(case.expected_sections)
        if not case.must_abstain:
            section_total += 1
            returned_expected_sections = expected_sections & {
                hit.section_path
                for hit in hits
                if hit.document_id in set(case.expected_document_ids)
            }
            section_hit += bool(returned_expected_sections)
        for hit in hits:
            if hit.catalog_ref in set(case.forbidden_catalog_refs):
                forbidden_hits += 1
            if case.corpus == "BOOK_GUIDE" and catalog is not None and not _catalog_ref_allowed(case, hit.catalog_ref, catalog):
                hard_filter_violations += 1
    return {
        "section_recall": section_hit / section_total if section_total else 0.0,
        "forbidden_hits": forbidden_hits,
        "hard_filter_violations": hard_filter_violations,
    }


def _select_threshold_with_section_gate(
    cases: list[RagEvalCase],
    observations: dict[str, RetrievalObservation],
    chunks: dict[str, list[RetrievedChunk]],
) -> tuple[Any, float]:
    """Select a cutoff that also preserves the required relevant sections."""
    candidates = sorted(
        {
            hit.score
            for case in cases
            for hit in chunks.get(case.case_id, [])
            if hit.score > 0
        },
        reverse=True,
    )
    if not candidates:
        choice = select_threshold(list(observations.values()))
        return choice, 0.0
    eligible: list[tuple[Any, float]] = []
    scored: list[tuple[Any, float]] = []
    for threshold in candidates:
        choice = select_threshold_at(list(observations.values()), threshold)
        section_recall = float(
            _quality_metrics(cases, chunks, thresholds={case.corpus: threshold for case in cases})[
                "section_recall"
            ]
        )
        item = (choice, section_recall)
        scored.append(item)
        if (
            choice.recall_at_5 >= .90
            and choice.false_positive_rate <= .05
            and section_recall >= .90
        ):
            eligible.append(item)
    pool = eligible or scored
    choice, section_recall = sorted(
        pool,
        key=lambda item: (
            item[0].mrr,
            item[1],
            -item[0].false_positive_rate,
            item[0].threshold,
        ),
        reverse=True,
    )[0]
    return choice.__class__(
        threshold=choice.threshold,
        recall_at_5=choice.recall_at_5,
        mrr=choice.mrr,
        false_positive_rate=choice.false_positive_rate,
        passed=bool(eligible),
    ), section_recall


def evaluate_fixture(cases: list[RagEvalCase], fixture_path: Path) -> dict[str, Any]:
    """Evaluate frozen chunk-level retrieval output; never contacts a provider."""
    fixture = _load_fixture(fixture_path, cases)
    documents = _load_eval_documents()
    catalog = _load_manual_catalog()
    _validate_fixture_semantics(cases, fixture, documents, catalog)
    observations: list[RetrievalObservation] = []
    chunks_by_case: dict[str, list[RetrievedChunk]] = {}
    for case in cases:
        chunks = [RetrievedChunk(item.document_id, item.section_path, item.catalog_ref, item.score) for item in fixture[case.case_id]]
        chunks_by_case[case.case_id] = chunks
        observations.append(_aggregate_chunk_hits(case, chunks))
    base_loader = RagDocumentLoader()
    keyword = _lexical_rank(cases, documents, catalog, context_token_budget=6000, encoding=base_loader.encoding)
    full_context = _full_context_rank(cases, documents, catalog, context_token_budget=6000, encoding=base_loader.encoding)
    return {
        "mode": "FIXTURE",
        "evaluation_layer": RAG_EVALUATION_LAYER,
        "case_count": len(cases),
        "metrics": {
            **retrieval_metrics(observations),
            **_quality_metrics(cases, chunks_by_case, catalog=catalog),
        },
        "baselines": {
            "keyword": retrieval_metrics(list(keyword.values())),
            "full-context": retrieval_metrics(list(full_context.values())),
        },
        "context_tokens": {"budget": 6000},
        "answer_quality_status": "NOT_RUN",
        "answer_quality_reason": "This command evaluates retrieval and hard-filter evidence only; it does not execute final-answer generation.",
        "provider_calls": 0,
        "fixture_path": str(fixture_path),
    }


class _CountingEmbeddingProvider(EmbeddingProvider):
    def __init__(self, delegate: EmbeddingProvider) -> None:
        self.delegate = delegate
        self.calls = 0
        self.texts = 0

    async def embed(self, texts: list[str], model: str) -> EmbeddingResponse:
        self.calls += 1
        self.texts += len(texts)
        return await self.delegate.embed(texts, model)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]+", text.casefold()))


def _catalog_ref_allowed(case: RagEvalCase, catalog_ref: str | None, catalog: dict[str, dict[str, Any]] | None) -> bool:
    if case.corpus != "BOOK_GUIDE" or catalog is None:
        return True
    if not catalog_ref or catalog_ref not in catalog:
        return False
    if case.allowed_catalog_refs and catalog_ref not in case.allowed_catalog_refs:
        return False
    if catalog_ref in set(case.forbidden_catalog_refs):
        return False
    record = catalog[catalog_ref]
    if case.allowed_book_ids and int(record.get("book_id", 0)) not in case.allowed_book_ids:
        return False
    if int(record.get("book_id", 0)) in case.forbidden_book_ids:
        return False
    if record.get("status") != "ACTIVE":
        return False
    filters = case.catalog_filters
    if filters.get("language") and record.get("language_code") != filters["language"]:
        return False
    available_stock = int(record.get("available_stock", record.get("stock", record.get("stock_count", 0))) or 0)
    if filters.get("in_stock") is True and available_stock <= 0:
        return False
    if filters.get("in_stock") is False and available_stock > 0:
        return False
    if filters.get("min_price") is not None and int(record.get("price", 0)) < int(filters["min_price"]):
        return False
    if filters.get("max_price") is not None and int(record.get("price", 0)) > int(filters["max_price"]):
        return False
    if filters.get("author"):
        authors = record.get("authors", record.get("author", []))
        if isinstance(authors, str):
            authors = [authors]
        if filters["author"] not in authors:
            return False
    if filters.get("category"):
        categories = record.get("categories", record.get("category", []))
        if isinstance(categories, str):
            categories = [categories]
        if filters["category"] not in categories:
            return False
    if filters.get("q"):
        haystack = " ".join(str(record.get(key, "")) for key in ("title", "subtitle", "description", "isbn")).casefold()
        if str(filters["q"]).casefold() not in haystack:
            return False
    return True


def _lexical_rank(
    cases: list[RagEvalCase],
    documents: dict[str, Any],
    catalog: dict[str, dict[str, Any]] | None = None,
    *,
    context_token_budget: int = 6000,
    encoding: Any | None = None,
) -> dict[str, RetrievalObservation]:
    encoding = encoding or RagDocumentLoader().encoding
    output: dict[str, RetrievalObservation] = {}
    for case in cases:
        query_tokens = _tokens(case.query)
        candidates = []
        for document_id, document in documents.items():
            if case.corpus != document.document_type:
                continue
            if not _catalog_ref_allowed(case, document.catalog_ref, catalog):
                continue
            overlap = len(query_tokens & _tokens(document.body))
            candidates.append((overlap, document_id))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        selected: list[str] = []
        used_tokens = 0
        for _, document_id in candidates:
            tokens = len(encoding.encode(documents[document_id].body))
            if used_tokens + tokens > context_token_budget:
                continue
            selected.append(document_id)
            used_tokens += tokens
            if len(selected) >= 5:
                break
        score_by_id = {document_id: score for score, document_id in candidates}
        result_ids = tuple(selected)
        output[case.case_id] = RetrievalObservation(
            result_ids=result_ids,
            scores=tuple(float(score_by_id[item]) for item in result_ids),
            relevant_ids=frozenset(case.expected_document_ids),
            must_abstain=case.must_abstain,
        )
    return output


def _full_context_rank(
    cases: list[RagEvalCase],
    documents: dict[str, Any],
    catalog: dict[str, dict[str, Any]] | None = None,
    *,
    context_token_budget: int = 6000,
    encoding: Any | None = None,
) -> dict[str, RetrievalObservation]:
    encoding = encoding or RagDocumentLoader().encoding
    output: dict[str, RetrievalObservation] = {}
    for case in cases:
        candidates = [
            document_id
            for document_id, document in sorted(documents.items())
            if document.document_type == case.corpus
            and _catalog_ref_allowed(case, document.catalog_ref, catalog)
        ]
        selected: list[str] = []
        used_tokens = 0
        for document_id in candidates:
            tokens = len(encoding.encode(documents[document_id].body))
            if used_tokens + tokens > context_token_budget:
                continue
            selected.append(document_id)
            used_tokens += tokens
            if len(selected) >= 5:
                break
        result_ids = tuple(selected)
        output[case.case_id] = RetrievalObservation(
            result_ids=result_ids,
            scores=tuple(float(5 - index) for index in range(len(result_ids))),
            relevant_ids=frozenset(case.expected_document_ids),
            must_abstain=case.must_abstain,
        )
    return output


def _row_to_retrieved_chunk(row: dict[str, Any]) -> RetrievedChunk:
    """Validate the fields needed to audit a real Qdrant chunk hit."""
    result = RagFixtureResult.model_validate(
        {
            "document_id": row.get("document_id"),
            "section_path": row.get("section_path"),
            "catalog_ref": row.get("catalog_ref"),
            "score": row.get("score"),
        }
    )
    if not math.isfinite(result.score) or not -1.0 <= result.score <= 1.0:
        raise ValueError("Qdrant returned an invalid cosine score")
    return RetrievedChunk(result.document_id, result.section_path, result.catalog_ref, float(result.score))


def _cap_rows_to_context_budget(rows: list[dict[str, Any]], loader: RagDocumentLoader, budget: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used_tokens = 0
    for row in rows:
        tokens = len(loader.encoding.encode(str(row.get("content", ""))))
        if used_tokens + tokens > budget:
            continue
        selected.append(row)
        used_tokens += tokens
    return selected


async def _live_evaluate(args: argparse.Namespace, cases: list[RagEvalCase]) -> dict[str, Any]:
    """Run actual embeddings against temporary Qdrant collections."""
    _require_all_baseline(args.baseline)
    started_at = getattr(args, "_started_at", time.monotonic())
    from app.config import RagCommandSettings
    from app.models.configuration import duration_seconds
    from app.models.embeddings import OpenAICompatibleEmbeddingProvider
    from app.tools.clients import GrpcCatalogClient
    from app.tools.schemas import GetBookInput

    settings = RagCommandSettings()  # type: ignore[call-arg]
    # Evaluation intentionally exercises only the configured embedding
    # provider.  It must work in an isolated test workspace even when that
    # workspace is not one of the API's retained production vector spaces.
    embeddings = _CountingEmbeddingProvider(
        OpenAICompatibleEmbeddingProvider(
            settings.embedding_base_url,
            settings.embedding_api_key.get_secret_value(),
            duration_seconds(settings.embedding_timeout),
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
        )
    )
    catalog = GrpcCatalogClient(settings.business_grpc_addr)
    root = Path(__file__).resolve().parents[2]
    documents = {document.document_id: document for document in RagDocumentLoader().load_corpus(root / "knowledge", "all")}
    cases_by_corpus = {kind: [case for case in cases if case.corpus == kind] for kind in ("POLICY", "BOOK_GUIDE")}
    resolved_books: dict[str, int] = {}
    catalog_records: dict[str, dict[str, Any]] = {}
    try:
        for document in documents.values():
            if document.document_type == "BOOK_GUIDE" and document.isbn:
                value = await catalog.get_book(GetBookInput(isbn=document.isbn), timeout=5)
                if value.get("status") == "ACTIVE":
                    resolved_books[document.catalog_ref or ""] = int(value["book_id"])
                    catalog_records[document.catalog_ref or ""] = value
        manifest_for_holdout = None
        if args.split == "holdout":
            from app.rag.contracts import RagManifest
            manifest_path = Path(args.manifest or "runtime/rag-index-manifest.json")
            if not manifest_path.is_absolute():
                manifest_path = root / "agent" / manifest_path
            manifest_for_holdout = RagManifest.read(manifest_path)
            manifest_for_holdout.verify_hash()
            manifest_for_holdout.assert_compatible(model=settings.embedding_model, dimensions=settings.embedding_dimensions)
            configs = [(manifest_for_holdout.chunking.target_tokens, manifest_for_holdout.chunking.overlap_tokens)]
        else:
            configs = [(256, 32), (384, 48), (500, 80)]
        tuning_results: list[ChunkingObservation] = []
        selected_thresholds: dict[str, float] = {}
        threshold_selection: dict[str, dict[str, Any]] = {}
        selected_config = configs[0]
        all_rag_observations: dict[str, RetrievalObservation] = {}
        all_rag_chunks: dict[str, list[RetrievedChunk]] = {}
        observations_by_config: dict[tuple[int, int], dict[str, RetrievalObservation]] = {}
        chunks_by_config: dict[tuple[int, int], dict[str, list[RetrievedChunk]]] = {}
        qdrant_url = args.qdrant_url or settings.qdrant_url
        build_prefix = "rag_eval_" + uuid.uuid4().hex[:12]
        context_budget = getattr(settings, "rag_max_context_tokens", 6000)
        for target, overlap in configs:
            loader = RagDocumentLoader(target_tokens=target, overlap_tokens=overlap)
            chunks: list[RagChunk] = []
            for document in documents.values():
                book_id = resolved_books.get(document.catalog_ref or "") if document.document_type == "BOOK_GUIDE" else None
                if document.document_type == "BOOK_GUIDE" and book_id is None:
                    continue
                chunks.extend(loader.chunks_for(document, book_id=book_id))
            space = embedding_space_identity(settings.embedding_model, settings.embedding_dimensions)
            chunks = [chunk.with_embedding_space(space) for chunk in chunks]
            stores = {
                "POLICY": QdrantRagStore(qdrant_url, alias=f"{build_prefix}_policy_{target}_{overlap}", dimensions=settings.embedding_dimensions, direct_collection=True),
                "BOOK_GUIDE": QdrantRagStore(qdrant_url, alias=f"{build_prefix}_book_{target}_{overlap}", dimensions=settings.embedding_dimensions, direct_collection=True),
            }
            operation_error: BaseException | None = None
            cleanup_errors: list[str] = []
            try:
                for kind, store in stores.items():
                    rows = [chunk for chunk in chunks if chunk.document_type == kind]
                    await store.create_physical_collection(store.alias)
                    await store.create_payload_indexes(store.alias, ("document_id", "locale", "book_id") if kind == "BOOK_GUIDE" else ("document_id", "locale"))
                    for offset in range(0, len(rows), 20):
                        batch = rows[offset : offset + 20]
                        response = await embeddings.embed([chunk.content for chunk in batch], settings.embedding_model)
                        await store.upsert(store.alias, batch, response.vectors)
                observations_by_case: dict[str, RetrievalObservation] = {}
                chunks_by_case: dict[str, list[RetrievedChunk]] = {}
                context_tokens_by_case: dict[str, int] = {}
                for case in cases:
                    query_response = await embeddings.embed([case.query], settings.embedding_model)
                    store = stores[case.corpus]
                    must = None
                    if case.corpus == "BOOK_GUIDE":
                        allowed_ids = [
                            resolved_books[ref]
                            for ref in resolved_books
                            if _catalog_ref_allowed(case, ref, catalog_records)
                        ]
                        if allowed_ids:
                            must = [{"key": "book_id", "match": {"any": allowed_ids}}]
                        else:
                            # An empty hard-filter candidate set is a valid
                            # no-match result; do not query the unfiltered
                            # collection and leak an unrelated book.
                            rows = []
                            chunks_by_case[case.case_id] = []
                            context_tokens_by_case[case.case_id] = 0
                            observations_by_case[case.case_id] = RetrievalObservation(
                                (), (), frozenset(case.expected_document_ids), case.must_abstain
                            )
                            continue
                    rows = await store.query(query_response.vectors[0], limit=12, score_threshold=-1.0, must=must)
                    rows.sort(key=lambda row: (-float(row.get("score", 0.0)), str(row.get("point_id", ""))))
                    rows = _cap_rows_to_context_budget(rows, loader, context_budget)
                    context_tokens_by_case[case.case_id] = sum(
                        len(loader.encoding.encode(str(row.get("content", "")))) for row in rows
                    )
                    hits = [_row_to_retrieved_chunk(row) for row in rows]
                    chunks_by_case[case.case_id] = hits
                    observations_by_case[case.case_id] = _aggregate_chunk_hits(case, hits)
                if args.split == "tuning":
                    metrics = retrieval_metrics(list(observations_by_case.values()))
                    tuning_results.append(ChunkingObservation(
                        target,
                        overlap,
                        metrics["recall_at_5"],
                        metrics["mrr"],
                        float(sum(context_tokens_by_case.values()) / max(len(context_tokens_by_case), 1)),
                    ))
                observations_by_config[(target, overlap)] = observations_by_case
                chunks_by_config[(target, overlap)] = chunks_by_case
            except BaseException as exc:
                operation_error = exc
            finally:
                for store in stores.values():
                    try:
                        await store.delete_collection(store.alias)
                    except Exception as exc:
                        cleanup_errors.append(f"delete {store.alias}: {type(exc).__name__}")
                    try:
                        await store.aclose()
                    except Exception as exc:
                        cleanup_errors.append(f"close {store.alias}: {type(exc).__name__}")
            if operation_error is not None:
                if cleanup_errors:
                    operation_error.add_note("temporary Qdrant cleanup also failed: " + "; ".join(cleanup_errors))
                raise operation_error
            if cleanup_errors:
                raise RuntimeError("temporary Qdrant cleanup failed: " + "; ".join(cleanup_errors))
        if args.split == "tuning":
            selected = select_chunking(tuning_results)
            selected_config = (selected.target_tokens, selected.overlap_tokens)
            all_rag_observations = observations_by_config[selected_config]
            all_rag_chunks = chunks_by_config[selected_config]
            for corpus in ("POLICY", "BOOK_GUIDE"):
                corpus_cases = cases_by_corpus[corpus]
                choice, section_recall = _select_threshold_with_section_gate(
                    corpus_cases,
                    {case.case_id: all_rag_observations[case.case_id] for case in corpus_cases},
                    {case.case_id: all_rag_chunks[case.case_id] for case in corpus_cases},
                )
                if not choice.passed:
                    raise RuntimeError(
                        f"{corpus} tuning gate failed: no threshold satisfies "
                        "Recall@5>=0.90, Section Recall>=0.90, and FPR<=0.05"
                    )
                selected_thresholds[corpus] = choice.threshold
                threshold_selection[corpus] = {**choice.__dict__, "section_recall": section_recall}
        else:
            assert manifest_for_holdout is not None
            selected_config = (manifest_for_holdout.chunking.target_tokens, manifest_for_holdout.chunking.overlap_tokens)
            selected_thresholds = dict(manifest_for_holdout.thresholds)
            all_rag_observations = observations_by_config[selected_config]
            all_rag_chunks = chunks_by_config[selected_config]
        keyword = retrieval_metrics(list(_lexical_rank(
            cases, documents, catalog_records, context_token_budget=context_budget,
            encoding=RagDocumentLoader().encoding,
        ).values()))
        full_context = retrieval_metrics(list(_full_context_rank(
            cases, documents, catalog_records, context_token_budget=context_budget,
            encoding=RagDocumentLoader().encoding,
        ).values()))
        rag_observations: list[RetrievalObservation] = []
        for case in cases:
            item = all_rag_observations[case.case_id]
            threshold = selected_thresholds[case.corpus]
            rag_observations.append(_aggregate_chunk_hits(case, all_rag_chunks[case.case_id], score_threshold=threshold))
        rag_metrics = {
            **retrieval_metrics(rag_observations),
            **_quality_metrics(cases, all_rag_chunks, thresholds=selected_thresholds, catalog=catalog_records),
        }
        case_results = []
        for case in cases:
            threshold = selected_thresholds[case.corpus]
            accepted = [hit for hit in all_rag_chunks[case.case_id] if hit.score >= threshold]
            returned_documents = list(dict.fromkeys(hit.document_id for hit in accepted))
            returned_sections = sorted(
                {
                    hit.section_path
                    for hit in accepted
                    if hit.document_id in set(case.expected_document_ids)
                }
            )
            case_results.append(
                {
                    "case_id": case.case_id,
                    "must_abstain": case.must_abstain,
                    "threshold": threshold,
                    "returned_document_ids": returned_documents,
                    "returned_sections": returned_sections,
                    "missing_document_ids": sorted(set(case.expected_document_ids) - set(returned_documents)),
                    "missing_sections": sorted(set(case.expected_sections) - set(returned_sections)),
                    "accepted_hits": [
                        {
                            "document_id": hit.document_id,
                            "section_path": hit.section_path,
                            "catalog_ref": hit.catalog_ref,
                            "score": hit.score,
                        }
                        for hit in accepted
                    ],
                }
            )
        selected_loader = RagDocumentLoader(target_tokens=selected_config[0], overlap_tokens=selected_config[1])
        dataset_path = Path(getattr(args, "_dataset_path", root / "agent" / "evals" / "rag-v2" / f"{args.split}.jsonl"))
        report_metrics = {"keyword": keyword, "full-context": full_context, "rag": rag_metrics}
        return {
            "schema_version": 1,
            "mode": "REAL_PROVIDER",
            "evaluation_layer": RAG_EVALUATION_LAYER,
            "split": args.split.upper(),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "embedding": {
                "model": settings.embedding_model,
                "dimensions": settings.embedding_dimensions,
                "distance": "Cosine",
            },
            "dataset_hash": _dataset_hash(dataset_path),
            "source_tree_hash": _knowledge_source_hash(documents, selected_loader),
            "selected_chunking": {
                "tokenizer": "cl100k_base",
                "target_tokens": selected_config[0],
                "overlap_tokens": selected_config[1],
            },
            "baseline": args.baseline,
            "case_count": len(cases),
            "chunking": {"target_tokens": selected_config[0], "overlap_tokens": selected_config[1]},
            "chunk_candidates": [item.__dict__ for item in tuning_results],
            "thresholds": selected_thresholds,
            "threshold_selection": threshold_selection,
            "metrics": report_metrics,
            "baselines": report_metrics,
            "case_results": case_results,
            "context_tokens": {
                "budget": context_budget,
                "rag_average_result_documents": round(sum(len(item.result_ids) for item in rag_observations) / max(len(rag_observations), 1), 3),
            },
            "answer_quality_status": "NOT_RUN",
            "answer_quality_reason": "This command evaluates retrieval and hard-filter evidence only; it does not execute final-answer generation.",
            "provider_calls": embeddings.calls,
            "provider_inputs": embeddings.texts,
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "latency_ms": round((time.monotonic() - started_at) * 1000, 1),
        }
    finally:
        await catalog.aclose()
        await embeddings.delegate.aclose()  # type: ignore[attr-defined]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the versioned v2 RAG evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--split", choices=("tuning", "holdout"), required=True)
    run.add_argument(
        "--baseline",
        choices=("all",),
        default="all",
        help="run the fixed keyword, full-context, and RAG comparison together",
    )
    run.add_argument("--allow-provider-calls", action="store_true")
    run.add_argument("--cases-dir", default="evals/rag-v2")
    run.add_argument("--fixture", default=None)
    run.add_argument("--output", default=None, help="create-only JSON report path")
    run.add_argument("--manifest", default=None)
    run.add_argument("--qdrant-url", default=None)
    run.add_argument("--chunk-target", type=int, choices=(256, 384, 500), default=384)
    run.add_argument("--chunk-overlap", type=int, choices=(32, 48, 80), default=48)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command != "run":
        return
    try:
        _require_all_baseline(args.baseline)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    cases_path = Path(args.cases_dir) / f"{args.split}.jsonl"
    if not cases_path.exists():
        cases_path = Path(__file__).resolve().parents[1] / args.cases_dir / f"{args.split}.jsonl"
    expected = 24 if args.split == "tuning" else 56
    cases = validate_split(cases_path, expected_count=expected)
    if args.output is not None and Path(args.output).exists():
        raise SystemExit(f"refusing to overwrite existing RAG report: {args.output}")
    args._dataset_path = cases_path
    if not args.allow_provider_calls:
        if args.fixture is None:
            raise SystemExit("external RAG evaluation requires --allow-provider-calls; no provider was contacted")
        fixture_path = Path(args.fixture)
        if not fixture_path.exists():
            fixture_path = Path(__file__).resolve().parents[1] / args.fixture
        result = evaluate_fixture(cases, fixture_path)
        result.update({"baseline": args.baseline, "split": args.split.upper(), "schema_version": 1})
        if args.output is not None:
            result = _write_create_only_report(Path(args.output), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.split == "tuning" and args.output is None:
        raise SystemExit("real tuning requires --output for the create-only report")
    args._started_at = time.monotonic()
    try:
        result = asyncio.run(_live_evaluate(args, cases))
    except Exception as exc:
        raise SystemExit(f"real RAG evaluation failed: {exc}") from None
    if args.output is not None:
        result = _write_create_only_report(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=lambda value: value.__dict__))


if __name__ == "__main__":
    main()
