from __future__ import annotations

import re
import unicodedata
from typing import Any, Protocol

from app.memory.repository import MemoryRepository
from app.memory.types import (
    ApplyResult,
    MemoryCandidate,
    MemoryRecord,
    MemoryType,
    normalize_candidate,
    normalize_v2_preference_value,
    write_idempotency_key,
)
from app.models.embeddings import EmbeddingProvider


class HistoricalMemoryVectorStore(Protocol):
    """Read-only compatibility protocol for pre-v2 EPISODE lookup."""

    async def query(self, vector: list[float], *, user_id: int, limit: int = 10) -> list[str]: ...


_MEMORY_RELEVANT = re.compile(
    r"(?i)(?:recommend|suggest|book|read|author|topic|language|budget|喜欢|不喜欢|推荐|书|作者|主题|语言|预算)"
)


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        *,
        inferred_min_confidence: float = 0.85,
        vector_store: HistoricalMemoryVectorStore | None = None,
        embeddings: EmbeddingProvider | None = None,
        embedding_model: str = "",
        v2_mode: bool = False,
    ) -> None:
        self.repository = repository
        self.inferred_min_confidence = inferred_min_confidence
        self.vector_store = vector_store
        self.embeddings = embeddings
        self.embedding_model = embedding_model
        self.v2_mode = v2_mode

    async def apply_candidates(
        self,
        user_id: int,
        candidates: list[MemoryCandidate],
        *,
        conversation_id: str,
        message_id: str,
    ) -> list[ApplyResult]:
        if self.v2_mode:
            # v2 accepts only the explicit /preferences contract.  Automatic
            # extractor candidates (including EPISODE/index outbox writes)
            # are a retired online path.
            from app.memory.errors import MemoryValueInvalid

            raise MemoryValueInvalid("automatic memory candidate writes are disabled in v2")
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

    V2_PREFERENCE_KEYS = frozenset(
        {
            "PREFERRED_LANGUAGE",
            "PREFERRED_TOPIC",
            "PREFERRED_AUTHOR",
            "MAX_USUAL_BUDGET",
            "EXCLUDED_TOPIC",
            "EXCLUDED_AUTHOR",
        }
    )

    async def upsert_preference(self, user_id: int, key: str, value: str) -> tuple[MemoryRecord, bool]:
        """Write only an explicitly confirmed, structured v2 preference.

        This endpoint is intentionally separate from ``apply_candidates``:
        model extraction cannot reach it, and the old INFERRED/EPISODE schema
        remains available only for historical reads and deletion cleanup.
        """
        normalized_key = unicodedata.normalize("NFKC", key).strip()
        normalized_value = unicodedata.normalize("NFKC", value).strip()
        if normalized_key not in self.V2_PREFERENCE_KEYS:
            from app.memory.errors import MemoryKeyUnsupported
            raise MemoryKeyUnsupported("unsupported v2 preference key")
        if not normalized_value or len(normalized_value) > 255:
            from app.memory.errors import MemoryValueInvalid
            raise MemoryValueInvalid("preference value must contain 1 to 255 characters")
        normalized_value = normalize_v2_preference_value(normalized_key, normalized_value)
        # Explicit values are never allowed to carry credentials, contact
        # details, or live business facts into long-term context.
        from app.memory.types import _reject_sensitive
        _reject_sensitive({"value": normalized_value}, None)
        return await self.repository.upsert_preference(user_id, normalized_key, normalized_value)

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
        if not self.v2_mode and self.vector_store is not None and self.embeddings is not None:
            try:
                response = await self.embeddings.embed([request], self.embedding_model)
                if len(response.vectors) == 1 and response.vectors[0]:
                    candidates = await self.vector_store.query(response.vectors[0], user_id=user_id, limit=10)
                    episodes = (await self.repository.active_by_ids(user_id, candidates))[:5]
            except Exception:
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
