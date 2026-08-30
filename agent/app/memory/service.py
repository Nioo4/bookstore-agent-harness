from __future__ import annotations

import re
from typing import Any

from app.memory.repository import MemoryRepository
from app.memory.types import (
    ApplyResult,
    MemoryCandidate,
    MemoryRecord,
    MemoryType,
    normalize_candidate,
    write_idempotency_key,
)
from app.memory.vector import MemoryVectorStore
from app.models.embeddings import EmbeddingProvider


_MEMORY_RELEVANT = re.compile(
    r"(?i)(?:recommend|suggest|book|read|author|topic|language|budget|喜欢|不喜欢|推荐|书|作者|主题|语言|预算)"
)


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        *,
        inferred_min_confidence: float = 0.85,
        vector_store: MemoryVectorStore | None = None,
        embeddings: EmbeddingProvider | None = None,
        embedding_model: str = "",
    ) -> None:
        self.repository = repository
        self.inferred_min_confidence = inferred_min_confidence
        self.vector_store = vector_store
        self.embeddings = embeddings
        self.embedding_model = embedding_model

    async def apply_candidates(
        self,
        user_id: int,
        candidates: list[MemoryCandidate],
        *,
        conversation_id: str,
        message_id: str,
    ) -> list[ApplyResult]:
        results: list[ApplyResult] = []
        for index, raw in enumerate(candidates):
            candidate = normalize_candidate(
                raw,
                source_message_id=message_id,
                inferred_min_confidence=self.inferred_min_confidence,
            )
            results.append(
                await self.repository.apply(
                    user_id,
                    candidate,
                    write_key=write_idempotency_key(user_id, conversation_id, message_id, index, candidate),
                    conversation_id=conversation_id,
                    message_id=message_id,
                )
            )
        return results

    async def list(self, user_id: int, memory_type: MemoryType | None, limit: int) -> list[MemoryRecord]:
        return await self.repository.list_active(user_id, memory_type, limit)

    async def delete_one(self, user_id: int, memory_id: str) -> dict[str, int]:
        deleted, scheduled = await self.repository.delete_one(user_id, memory_id)
        return {"deleted_count": deleted, "vector_cleanup_scheduled": scheduled}

    async def delete_all(self, user_id: int) -> dict[str, int]:
        deleted, scheduled = await self.repository.delete_all(user_id)
        return {"deleted_count": deleted, "vector_cleanup_scheduled": scheduled}

    async def context_for_request(self, user_id: int, request: str) -> list[dict[str, Any]]:
        context, _ = await self.context_for_request_with_status(user_id, request)
        return context

    async def context_for_request_with_status(
        self,
        user_id: int,
        request: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if _MEMORY_RELEVANT.search(request) is None:
            return [], None
        stable = await self.repository.list_stable(user_id, 20)
        episodes: list[MemoryRecord] = []
        degradation: str | None = None
        if self.vector_store is not None and self.embeddings is not None:
            try:
                response = await self.embeddings.embed([request], self.embedding_model)
                if len(response.vectors) == 1 and response.vectors[0]:
                    candidates = await self.vector_store.query(response.vectors[0], user_id=user_id, limit=10)
                    episodes = (await self.repository.active_by_ids(user_id, candidates))[:5]
            except Exception:
                # Stable preferences have already been read from MySQL. Episode
                # retrieval is optional and must not erase that valid context.
                degradation = "MEMORY_VECTOR_UNAVAILABLE"
        return [
            {
                "memory_id": memory.memory_id,
                "memory_type": memory.memory_type.value,
                "memory_key": memory.memory_key,
                "value": memory.value,
                "source": memory.source.value,
                "confidence": memory.confidence,
            }
            for memory in [*stable, *episodes]
        ], degradation
