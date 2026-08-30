from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.config import AgentSettings
from app.models.factory import ModelRuntime
from app.models.configuration import AGENT_ROOT
from app.models.embeddings import OpenAICompatibleEmbeddingProvider
from app.tools.knowledge import MarkdownKnowledgeLoader, PolicyKnowledgeService, QdrantPolicyVectorStore


async def main() -> None:
    settings = AgentSettings()  # type: ignore[call-arg]
    runtime = ModelRuntime(settings)
    embeddings = runtime.embeddings()
    store = QdrantPolicyVectorStore(settings.qdrant_url, settings.policy_collection, vector_space=runtime.vector_space)
    try:
        # Qdrant's image has no portable curl/wget health probe. Wait for its
        # real readiness endpoint before indexing; the API waits for this job.
        async with httpx.AsyncClient(timeout=3) as client:
            for attempt in range(60):
                try:
                    (await client.get(settings.qdrant_url.rstrip("/") + "/readyz")).raise_for_status()
                    break
                except httpx.HTTPError:
                    if attempt == 59:
                        raise RuntimeError("KNOWLEDGE_DEPENDENCY_UNAVAILABLE") from None
                    await asyncio.sleep(1)
        await store.ensure_collection(settings.embedding_dimensions)
        chunks = MarkdownKnowledgeLoader().load_directory(AGENT_ROOT.parent / "knowledge")
        count = await PolicyKnowledgeService(embeddings, store, settings.embedding_model).index(chunks)
        print(f"knowledge chunks indexed={count}")
    finally:
        await embeddings.aclose()
        await store.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("knowledge indexing failed; check Qdrant readiness and model configuration") from None
