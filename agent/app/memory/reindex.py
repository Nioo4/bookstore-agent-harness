from __future__ import annotations

from app.memory.repository import MemoryRepository
from app.memory.types import content_hash
from app.memory.vector import MemoryVectorStore
from app.models.embeddings import EmbeddingProvider


async def reindex_active_episodes(
    repository: MemoryRepository, store: MemoryVectorStore, embeddings: EmbeddingProvider,
    *, model: str, dimensions: int, page_size: int = 100,
) -> int:
    """Rebuild a selected collection with all writers stopped; do not consume outbox.

    Rechecks also protect retries and deletion during an embedding request. They
    are not a replacement for stopping every writer during the collection switch.
    """
    await store.ensure_collection(dimensions)
    after_id, indexed = "", 0
    while page := await repository.active_episode_page(after_id, page_size):
        for owned in page:
            memory_id = owned.memory.memory_id
            memory = await repository.episode_for_index(owned.user_id, memory_id)
            if memory is None:
                await store.delete(memory_id)
                continue
            response = await embeddings.embed([memory.value["summary"]], model)
            if len(response.vectors) != 1 or len(response.vectors[0]) != dimensions:
                raise ValueError("invalid episode reindex embedding")
            current = await repository.episode_for_index(owned.user_id, memory_id)
            if current is None:
                await store.delete(memory_id)
                continue
            digest = content_hash(memory.value)
            if content_hash(current.value) != digest:
                raise ValueError("episode changed while writers must be stopped")
            await store.upsert(
                memory_id, response.vectors[0], user_id=owned.user_id,
                memory_type=memory.memory_type.value, memory_key=memory.memory_key, content_hash=digest,
            )
            current = await repository.episode_for_index(owned.user_id, memory_id)
            if current is None:
                await store.delete(memory_id)
                continue
            if content_hash(current.value) != digest:
                await store.delete(memory_id)
                raise ValueError("episode changed while writers must be stopped")
            indexed += 1
        next_id = page[-1].memory.memory_id
        if next_id <= after_id:
            raise ValueError("episode index cursor did not advance")
        after_id = next_id
    return indexed
