"""Explicit offline collection migration. Never invoked automatically at startup."""
from __future__ import annotations

import argparse
import asyncio
from typing import Any

import httpx

from app.config import AgentSettings
from app.memory.reindex import reindex_active_episodes
from app.memory.repository import AsyncMySQLMemoryRepository
from app.memory.types import content_hash
from app.memory.vector import QdrantMemoryVectorStore
from app.models.configuration import AGENT_ROOT
from app.models.factory import ModelRuntime
from app.tools.knowledge import MarkdownKnowledgeLoader, PolicyKnowledgeService, QdrantPolicyVectorStore


async def collection_payloads(client: httpx.AsyncClient, collection: str) -> dict[str, dict[str, Any]]:
    points: dict[str, dict[str, Any]] = {}
    offset = None
    while True:
        response = await client.post(f"/collections/{collection}/points/scroll", json={
            "limit": 100, "with_payload": True, "with_vector": False,
            **({"offset": offset} if offset is not None else {}),
        })
        response.raise_for_status()
        result = response.json()["result"]
        for point in result["points"]:
            point_id = str(point["id"])
            if point_id in points:
                raise ValueError("vector audit returned duplicate points")
            points[point_id] = point["payload"]
        next_offset = result.get("next_page_offset")
        if next_offset is None:
            return points
        if next_offset == offset:
            raise ValueError("vector audit cursor did not advance")
        offset = next_offset


def verify_payloads(actual: dict[str, dict[str, Any]], expected: dict[str, dict[str, Any]]) -> None:
    # Check every point, not just an unfiltered collection count. This catches
    # wrong-user, stale-space, missing and extra records without logging values.
    if actual.keys() != expected.keys() or any(actual[key] != value for key, value in expected.items()):
        raise ValueError("vector collection count/version/ownership audit failed")


async def migrate(settings: AgentSettings, *, allow_provider_calls: bool) -> tuple[int, int]:
    if settings.environment != "test" and not allow_provider_calls:
        raise ValueError("explicit authorization for real embedding calls is required")
    runtime = ModelRuntime(settings)
    embeddings = runtime.embeddings()
    policy = QdrantPolicyVectorStore(settings.qdrant_url, settings.policy_collection, vector_space=runtime.vector_space)
    memory = QdrantMemoryVectorStore(settings.qdrant_url, settings.memory_collection, vector_space=runtime.vector_space)
    repository = None
    try:
        # Reject both incompatible collections before the first provider call.
        await policy.ensure_collection(settings.embedding_dimensions)
        await memory.ensure_collection(settings.embedding_dimensions)
        repository = await AsyncMySQLMemoryRepository.create(settings.agent_mysql_dsn.get_secret_value())
        chunks = MarkdownKnowledgeLoader().load_directory(AGENT_ROOT.parent / "knowledge")
        if len({chunk.source for chunk in chunks}) != 3:
            raise ValueError("expected the three approved knowledge documents")
        policy_count = await PolicyKnowledgeService(embeddings, policy, settings.embedding_model).index(chunks)
        await reindex_active_episodes(repository, memory, embeddings,
            model=settings.embedding_model, dimensions=settings.embedding_dimensions)
        space = {"embedding_space": runtime.vector_space.identity}
        expected_policy = {chunk.point_id: {**chunk.payload(), **space} for chunk in chunks}
        verify_payloads(await collection_payloads(policy.client, policy.collection), expected_policy)
        expected_memory: dict[str, dict[str, Any]] = {}
        after_id = ""
        while page := await repository.active_episode_page(after_id):
            for owned in page:
                record = owned.memory
                expected_memory[record.memory_id] = {
                    "memory_id": record.memory_id, "user_id": owned.user_id,
                    "memory_type": record.memory_type.value, "memory_key": record.memory_key,
                    "content_hash": content_hash(record.value), **space,
                }
            after_id = page[-1].memory.memory_id
        verify_payloads(await collection_payloads(memory.client, memory.collection), expected_memory)
        return policy_count, len(expected_memory)
    finally:
        if repository is not None:
            await repository.aclose()
        await embeddings.aclose()
        await policy.aclose()
        await memory.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reindex selected collections; retain old collections and MySQL/outbox.")
    parser.add_argument("--writers-stopped", action="store_true", help="Confirm all API memory loops and other writers are stopped")
    parser.add_argument("--allow-provider-calls", action="store_true", help="Explicitly authorize real embedding requests")
    args = parser.parse_args()
    if not args.writers_stopped:
        parser.error("stop all API memory loops and other writers, then use --writers-stopped")
    try:
        settings = AgentSettings()  # type: ignore[call-arg]
        policies, memories = asyncio.run(migrate(settings, allow_provider_calls=args.allow_provider_calls))
    except Exception:
        raise SystemExit("vector reindex failed; keep readers/writers stopped and check configuration/dependencies") from None
    print(f"vector reindex audited: policy_chunks={policies}, active_episodes={memories}; switch not performed")


if __name__ == "__main__":
    main()
