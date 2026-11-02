from __future__ import annotations

"""The only Qdrant operation retained for historical EPISODE cleanup.

The v2 API never creates or retrieves EPISODE vectors.  This deliberately
small adapter has no collection creation, upsert, query, or embedding path;
the MySQL Outbox worker can only delete a point after a user-memory delete
transaction has committed.
"""

import httpx


class LegacyMemoryDeletionStore:
    def __init__(self, base_url: str, collection: str) -> None:
        if not collection:
            raise ValueError("legacy memory collection is required")
        self.collection = collection
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=10)

    async def delete(self, memory_id: str) -> None:
        response = await self.client.post(
            f"/collections/{self.collection}/points/delete",
            params={"wait": "true"},
            json={"points": [memory_id]},
        )
        if response.status_code == 404:
            return
        response.raise_for_status()

    async def aclose(self) -> None:
        await self.client.aclose()
