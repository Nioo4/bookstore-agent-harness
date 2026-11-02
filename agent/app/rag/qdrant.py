from __future__ import annotations

"""Small Qdrant HTTP adapter for the v2 RAG build and read paths.

The adapter intentionally knows only the collection/alias operations required
by this repository.  It never creates an alias before a complete build has
been uploaded and audited.
"""

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

import httpx

from app.rag.contracts import RagChunk


class QdrantRagStore:
    def __init__(
        self,
        base_url: str,
        *,
        alias: str,
        dimensions: int = 1024,
        timeout: float = 10,
        client: httpx.AsyncClient | None = None,
        direct_collection: bool = False,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")
        self.alias = alias
        self.dimensions = dimensions
        self.direct_collection = direct_collection
        self.client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def create_physical_collection(self, collection: str) -> None:
        if collection == self.alias and not self.direct_collection:
            raise ValueError("physical collection must not equal its alias")
        response = await self.client.put(
            f"/collections/{collection}",
            json={"vectors": {"size": self.dimensions, "distance": "Cosine"}},
        )
        if response.status_code not in {200, 201, 409}:
            response.raise_for_status()
        await self.assert_collection(collection)

    async def assert_collection(self, collection: str) -> None:
        response = await self.client.get(f"/collections/{collection}")
        response.raise_for_status()
        result = response.json().get("result", {})
        vectors = result.get("config", {}).get("params", {}).get("vectors", {})
        if vectors.get("size") != self.dimensions or vectors.get("distance") != "Cosine":
            raise ValueError(f"Qdrant collection {collection} has incompatible vector space")

    async def assert_alias_compatible(self, collection: str) -> None:
        """Validate both the alias target and the target collection vector space."""
        await self.assert_collection(collection)
        response = await self.client.get("/aliases")
        response.raise_for_status()
        aliases = response.json().get("result", {}).get("aliases", [])
        target = next(
            (item.get("collection_name") for item in aliases if item.get("alias_name") == self.alias),
            None,
        )
        if target != collection:
            raise ValueError(f"Qdrant alias {self.alias} points to {target!r}, expected {collection!r}")

    async def create_payload_indexes(self, collection: str, fields: Iterable[str]) -> None:
        """Create idempotent keyword indexes required by filtered retrieval."""
        for field in fields:
            response = await self.client.put(
                f"/collections/{collection}/index",
                json={"field_name": field, "field_schema": "integer" if field == "book_id" else "keyword"},
            )
            if response.status_code not in {200, 201, 409}:
                response.raise_for_status()

    async def upsert(self, collection: str, chunks: Iterable[RagChunk], vectors: Iterable[list[float]]) -> int:
        rows = list(zip(chunks, vectors, strict=True))
        point_ids = [chunk.point_id for chunk, _ in rows]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("duplicate point_id in RAG build input")
        if any(len(vector) != self.dimensions for _, vector in rows):
            raise ValueError("embedding vector dimension mismatch")
        if any(not chunk.embedding_space for chunk, _ in rows):
            raise ValueError("RAG chunk embedding_space must be set before upsert")
        for offset in range(0, len(rows), 20):
            batch = rows[offset : offset + 20]
            response = await self.client.put(
                f"/collections/{collection}/points",
                params={"wait": "true"},
                json={
                    "points": [
                        {"id": chunk.point_id, "vector": vector, "payload": chunk.payload()}
                        for chunk, vector in batch
                    ]
                },
            )
            response.raise_for_status()
        return len(rows)

    async def scroll_payloads(self, collection: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        offset: Any = None
        while True:
            body: dict[str, Any] = {"limit": 100, "with_payload": True, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            response = await self.client.post(f"/collections/{collection}/points/scroll", json=body)
            response.raise_for_status()
            page = response.json().get("result", {})
            for point in page.get("points", []):
                try:
                    # Qdrant renders a 32-hex UUID with hyphens on read even
                    # when the canonical hex value was used on upsert.
                    point_id = UUID(str(point["id"])).hex
                except (ValueError, AttributeError) as exc:
                    raise ValueError("Qdrant audit returned a non-UUID point ID") from exc
                if point_id in result:
                    raise ValueError("Qdrant audit returned duplicate point IDs")
                payload = dict(point.get("payload") or {})
                if payload.get("point_id") != point_id:
                    raise ValueError("Qdrant point ID does not match its audited payload")
                payload["point_id"] = point_id
                result[point_id] = payload
            next_offset = page.get("next_page_offset")
            if next_offset is None:
                return result
            if next_offset == offset:
                raise ValueError("Qdrant audit cursor did not advance")
            offset = next_offset

    async def swap_alias(self, collection: str) -> None:
        await self.swap_aliases([(self.alias, collection)])

    async def swap_aliases(self, mappings: Iterable[tuple[str, str]]) -> None:
        """Atomically change one or more aliases in one Qdrant request."""
        actions: list[dict[str, Any]] = []
        for alias, collection in mappings:
            await self.assert_collection(collection)
            response = await self.client.get("/aliases")
            response.raise_for_status()
            existing = next(
                (item for item in response.json().get("result", {}).get("aliases", []) if item.get("alias_name") == alias),
                None,
            )
            if existing is not None:
                actions.append({"delete_alias": {"alias_name": alias}})
            actions.append({"create_alias": {"collection_name": collection, "alias_name": alias}})
        if not actions:
            return
        response = await self.client.post("/collections/aliases", json={"actions": actions})
        response.raise_for_status()

    async def list_collections(self, prefix: str) -> list[str]:
        response = await self.client.get("/collections")
        response.raise_for_status()
        names = [str(item.get("name")) for item in response.json().get("result", {}).get("collections", [])]
        return sorted(name for name in names if name.startswith(prefix))

    async def delete_collection(self, collection: str) -> None:
        if collection == self.alias and not self.direct_collection:
            raise ValueError("refusing to delete a logical RAG alias")
        response = await self.client.delete(f"/collections/{collection}")
        if response.status_code not in {200, 404}:
            response.raise_for_status()

    async def retain_current_and_previous(
        self,
        prefix: str,
        *,
        current: str,
        previous: str | None = None,
    ) -> list[str]:
        """Delete only superseded physical collections after a successful swap.

        Callers must invoke this after the new alias and manifest are durable;
        no build failure path calls this method.  Lexically sortable build IDs
        (the CLI uses UTC timestamps) make the fallback deterministic.
        """
        names = await self.list_collections(prefix)
        if current not in names:
            raise ValueError("retention target collection does not exist")
        keep = {current}
        if previous:
            keep.add(previous)
        for name in sorted((item for item in names if item not in keep), reverse=True)[: max(0, 2 - len(keep))]:
            keep.add(name)
        deleted: list[str] = []
        for name in names:
            if name not in keep:
                await self.delete_collection(name)
                deleted.append(name)
        return deleted

    async def query(
        self,
        vector: list[float],
        *,
        limit: int,
        score_threshold: float,
        must: list[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if len(vector) != self.dimensions:
            raise ValueError("query vector dimension mismatch")
        body: dict[str, Any] = {
            "query": vector,
            "limit": max(1, min(limit, 12)),
            "score_threshold": score_threshold,
            "with_payload": True,
        }
        if must:
            body["filter"] = {"must": list(must)}
        response = await self.client.post(f"/collections/{self.alias}/points/query", json=body)
        if response.status_code == 404:
            # A missing alias/collection is a dependency/configuration
            # failure.  An empty 200 result is the only valid no-match path.
            response.raise_for_status()
        response.raise_for_status()
        rows: list[dict[str, Any]] = []
        for point in response.json().get("result", {}).get("points", []):
            try:
                point_id = UUID(str(point["id"])).hex
            except (KeyError, ValueError, AttributeError) as exc:
                raise ValueError("Qdrant query returned a non-UUID point ID") from exc
            payload = dict(point.get("payload") or {})
            payload_point_id = payload.get("point_id")
            if payload_point_id is not None and payload_point_id != point_id:
                raise ValueError("Qdrant query point ID does not match its payload")
            payload["point_id"] = point_id
            payload["score"] = point.get("score", 0.0)
            rows.append(payload)
        return rows

    async def smoke_query(
        self,
        collection: str,
        vector: list[float],
        *,
        expected_point_ids: Iterable[str] = (),
        expected_document_ids: Iterable[str] = (),
    ) -> None:
        """Run a minimal query and prove the new collection returns its data."""
        if len(vector) != self.dimensions:
            raise ValueError("smoke query vector dimension mismatch")
        response = await self.client.post(
            f"/collections/{collection}/points/query",
            json={"query": vector, "limit": 1, "with_payload": True},
        )
        response.raise_for_status()
        points = response.json().get("result", {}).get("points", [])
        if not points:
            raise ValueError(f"RAG smoke query returned no points for {collection}")
        point_ids = {str(point.get("id")) for point in points}
        payload_document_ids = {
            str((point.get("payload") or {}).get("document_id"))
            for point in points
            if (point.get("payload") or {}).get("document_id") is not None
        }
        expected_points = {str(item) for item in expected_point_ids}
        expected_documents = {str(item) for item in expected_document_ids}
        if expected_points and not point_ids.intersection(expected_points):
            if not expected_documents or not payload_document_ids.intersection(expected_documents):
                raise ValueError(f"RAG smoke query returned an unexpected point for {collection}")
