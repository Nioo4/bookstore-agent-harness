from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.models.embeddings import EmbeddingProvider
from app.rag.evidence import evidence_from_chunk, validate_book_evidence
from app.rag.qdrant import QdrantRagStore


class RagSearchService:
    """Online, read-only RAG facade used by the single Agent.

    Catalog facts never come from this service.  The caller supplies the
    authoritative Catalog book IDs and the service uses them only as a Qdrant
    filter for BOOK_GUIDE retrieval.
    """

    def __init__(
        self,
        embeddings: EmbeddingProvider,
        policy_store: QdrantRagStore,
        book_store: QdrantRagStore,
        *,
        embedding_model: str,
        policy_threshold: float,
        book_threshold: float,
    ) -> None:
        self.embeddings = embeddings
        self.policy_store = policy_store
        self.book_store = book_store
        self.embedding_model = embedding_model
        if not 0 < policy_threshold <= 1 or not 0 < book_threshold <= 1:
            raise ValueError("RAG thresholds must be greater than 0 and at most 1")
        self.policy_threshold = policy_threshold
        self.book_threshold = book_threshold

    async def search_policy(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        response = await self.embeddings.embed([query.strip()], self.embedding_model)
        if len(response.vectors) != 1:
            raise ValueError("embedding provider returned an invalid query vector")
        rows = await self.policy_store.query(
            response.vectors[0], limit=min(max(limit, 1), 5), score_threshold=self.policy_threshold
        )
        return [evidence_from_chunk(row) for row in rows]

    async def search_book_knowledge(
        self,
        query: str,
        book_ids: Iterable[int],
        *,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(book_ids))
        if not ids or not query or not query.strip():
            return []
        if len(ids) > 200:
            raise ValueError("book knowledge filter cannot contain more than 200 IDs")
        response = await self.embeddings.embed([query.strip()], self.embedding_model)
        if len(response.vectors) != 1:
            raise ValueError("embedding provider returned an invalid query vector")
        rows = await self.book_store.query(
            response.vectors[0],
            limit=min(max(limit, 1), 12),
            score_threshold=self.book_threshold,
            must=[{"key": "book_id", "match": {"any": ids}}],
        )
        rows.sort(key=lambda row: (-float(row.get("score", 0.0)), str(row.get("point_id", row.get("content_hash", "")))))
        accepted: list[dict[str, Any]] = []
        per_book: dict[int, int] = {}
        for row in rows:
            validate_book_evidence(row, ids)
            book_id = row["book_id"]
            if per_book.get(book_id, 0) >= 2:
                continue
            per_book[book_id] = per_book.get(book_id, 0) + 1
            accepted.append(evidence_from_chunk(row))
            if len(accepted) >= 10 or len(per_book) >= 5 and len(accepted) >= 10:
                break
        return accepted
