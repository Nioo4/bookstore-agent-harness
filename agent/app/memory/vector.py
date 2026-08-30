from __future__ import annotations

import asyncio
from typing import Any, Protocol

import httpx

from app.memory.repository import MemoryRepository
from app.models.embeddings import EmbeddingProvider


class MemoryVectorStore(Protocol):
    async def ensure_collection(self, vector_size: int) -> None: ...

    async def upsert(
        self,
        memory_id: str,
        vector: list[float],
        *,
        user_id: int,
        memory_type: str,
        memory_key: str,
        content_hash: str,
    ) -> None: ...

    async def delete(self, memory_id: str) -> None: ...

    async def query(self, vector: list[float], *, user_id: int, limit: int = 10) -> list[str]: ...


class QdrantMemoryVectorStore:
    def __init__(self, base_url: str, collection: str = "bookstore_memory", *, vector_space=None) -> None:
        self.collection = collection
        self.vector_space = vector_space
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=10)

    async def ensure_collection(self, vector_size: int) -> None:
        if self.vector_space is not None and (vector_size != self.vector_space.dimensions or self.collection != self.vector_space.memory_collection):
            raise ValueError("memory collection vector space mismatch")
        response = await self.client.get(f"/collections/{self.collection}")
        if response.status_code == 404:
            response = await self.client.put(
                f"/collections/{self.collection}",
                json={"vectors": {"size": vector_size, "distance": "Cosine"}},
            )
            if response.status_code not in {200, 201, 409}:
                response.raise_for_status()
            response = await self.client.get(f"/collections/{self.collection}")
        response.raise_for_status()
        vectors = response.json().get("result", {}).get("config", {}).get("params", {}).get("vectors", {})
        if vectors.get("size") != vector_size or vectors.get("distance") != "Cosine":
            raise ValueError("Qdrant memory collection size/distance mismatch")
        index = await self.client.put(
            f"/collections/{self.collection}/index",
            params={"wait": "true"},
            json={"field_name": "user_id", "field_schema": "integer"},
        )
        if index.status_code not in {200, 201, 409}:
            index.raise_for_status()

    async def upsert(
        self,
        memory_id: str,
        vector: list[float],
        *,
        user_id: int,
        memory_type: str,
        memory_key: str,
        content_hash: str,
    ) -> None:
        response = await self.client.put(
            f"/collections/{self.collection}/points",
            params={"wait": "true"},
            json={
                "points": [
                    {
                        "id": memory_id,
                        "vector": vector,
                        "payload": {
                            "memory_id": memory_id,
                            "user_id": user_id,
                            "memory_type": memory_type,
                            "memory_key": memory_key,
                            "content_hash": content_hash,
                            **({"embedding_space": self.vector_space.identity} if self.vector_space else {}),
                        },
                    }
                ]
            },
        )
        response.raise_for_status()

    async def delete(self, memory_id: str) -> None:
        response = await self.client.post(
            f"/collections/{self.collection}/points/delete",
            params={"wait": "true"},
            json={"points": [memory_id]},
        )
        if response.status_code == 404:
            return
        response.raise_for_status()

    async def query(self, vector: list[float], *, user_id: int, limit: int = 10) -> list[str]:
        response = await self.client.post(
            f"/collections/{self.collection}/points/query",
            json={
                "query": vector,
                "filter": {"must": [{"key": "user_id", "match": {"value": user_id}},
                    *([{"key":"embedding_space", "match":{"value":self.vector_space.identity}}] if self.vector_space else [])]},
                "limit": min(limit, 10),
                "with_payload": True,
            },
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        ids: list[str] = []
        for point in response.json().get("result", {}).get("points", []):
            payload = point.get("payload", {})
            memory_id = payload.get("memory_id")
            if isinstance(memory_id, str):
                ids.append(memory_id)
        return ids[:10]

    async def aclose(self) -> None:
        await self.client.aclose()


class MemoryOutboxWorker:
    def __init__(
        self,
        repository: MemoryRepository,
        store: MemoryVectorStore,
        embeddings: EmbeddingProvider,
        embedding_model: str,
    ) -> None:
        self.repository = repository
        self.store = store
        self.embeddings = embeddings
        self.embedding_model = embedding_model

    async def run_once(self, limit: int = 50) -> int:
        events = await self.repository.pending_outbox(limit)
        processed = 0
        for event in events:
            try:
                if event.event_type == "memory.index_requested.v1":
                    memory = await self.repository.episode_for_index(event.user_id, event.memory_id)
                    if memory is None:
                        await self.repository.mark_skipped(event.event_id)
                        processed += 1
                        continue
                    response = await self.embeddings.embed([memory.value["summary"]], self.embedding_model)
                    if len(response.vectors) != 1 or not response.vectors[0]:
                        raise ValueError("embedding provider returned an invalid memory vector")
                    vector = response.vectors[0]
                    await self.store.ensure_collection(len(vector))
                    await self.store.upsert(
                        memory.memory_id,
                        vector,
                        user_id=event.user_id,
                        memory_type=memory.memory_type.value,
                        memory_key=memory.memory_key,
                        content_hash=str(event.payload["content_hash"]),
                    )
                    await self.repository.mark_indexed(event.event_id, event.memory_id)
                elif event.event_type == "memory.delete_requested.v1":
                    await self.store.delete(event.memory_id)
                    await self.repository.mark_deleted(event.event_id, event.memory_id)
                else:
                    raise ValueError("unsupported memory outbox event type")
                processed += 1
            except Exception as exc:
                await self.repository.mark_failed(
                    event.event_id,
                    event.memory_id,
                    f"{type(exc).__name__}: memory vector operation failed",
                )
        return processed


class MemoryOutboxLoop:
    def __init__(self, worker: MemoryOutboxWorker, interval_seconds: float = 1.0) -> None:
        self.worker = worker
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="memory-outbox-worker")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.worker.run_once()
            except Exception:
                pass
            await asyncio.sleep(self.interval_seconds)
