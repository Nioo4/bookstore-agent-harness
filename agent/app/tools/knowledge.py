from __future__ import annotations

import hashlib
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx
import tiktoken
import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.models.embeddings import EmbeddingProvider


class KnowledgeDocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    version: str = Field(min_length=1)
    effective_date: date
    document_type: str = Field(min_length=1)
    locale: str = Field(min_length=1)


class KnowledgeChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str
    title: str
    section: str
    version: str
    effective_date: date
    text: str = Field(min_length=1)

    def payload(self) -> dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "source_path": f"knowledge/{self.source}",
            "document_title": self.title,
            "section": self.section,
            "version": self.version,
            "effective_date": self.effective_date.isoformat(),
            "content": self.text,
        }


class MarkdownKnowledgeLoader:
    def __init__(self, target_tokens: int = 500, overlap_tokens: int = 80) -> None:
        if target_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= target_tokens:
            raise ValueError("invalid chunk token limits")
        self.target_tokens = target_tokens
        self.overlap_tokens = overlap_tokens
        self.encoding = tiktoken.get_encoding("cl100k_base")

    def load_directory(self, directory: Path) -> list[KnowledgeChunk]:
        if not directory.exists():
            return []
        chunks: list[KnowledgeChunk] = []
        seen_hashes: set[str] = set()
        for path in sorted(directory.glob("*.md")):
            for chunk in self.load_file(path):
                if chunk.content_hash in seen_hashes:
                    continue
                seen_hashes.add(chunk.content_hash)
                chunks.append(chunk)
        return chunks

    def load_file(self, path: Path) -> list[KnowledgeChunk]:
        raw = path.read_text(encoding="utf-8")
        metadata, body = self._frontmatter(raw)
        sections = self._sections(body)
        chunks: list[KnowledgeChunk] = []
        for section, text in sections:
            tokens = self.encoding.encode(text)
            step = self.target_tokens - self.overlap_tokens
            for start in range(0, len(tokens), step):
                token_slice = tokens[start : start + self.target_tokens]
                if not token_slice:
                    continue
                chunk_text = self.encoding.decode(token_slice).strip()
                if not chunk_text:
                    continue
                normalized = "\n".join(line.rstrip() for line in chunk_text.splitlines()).strip()
                content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                chunks.append(
                    KnowledgeChunk(
                        point_id=str(uuid.UUID(hex=content_hash[:32])),
                        content_hash=content_hash,
                        source=path.name,
                        title=metadata.title,
                        section=section,
                        version=metadata.version,
                        effective_date=metadata.effective_date,
                        text=normalized,
                    )
                )
                if start + self.target_tokens >= len(tokens):
                    break
        return chunks

    @staticmethod
    def _frontmatter(raw: str) -> tuple[KnowledgeDocumentMetadata, str]:
        match = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", raw, flags=re.DOTALL)
        if match is None:
            raise ValueError("knowledge document must contain YAML frontmatter")
        metadata = KnowledgeDocumentMetadata.model_validate(yaml.safe_load(match.group(1)))
        return metadata, match.group(2)

    @staticmethod
    def _sections(body: str) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        current_lines: list[str] = []
        current_section: str | None = None
        document_title_seen = False
        for line in body.splitlines():
            heading = re.match(r"^##\s+(.+?)\s*$", line)
            if heading:
                if current_section is not None and any(part.strip() for part in current_lines):
                    sections.append((current_section, "\n".join(current_lines).strip()))
                current_section = heading.group(1).strip()
                current_lines = [line]
                continue
            if current_section is None:
                if not line.strip():
                    continue
                if not document_title_seen and re.match(r"^#(?!#)\s+\S", line):
                    document_title_seen = True
                    continue
                raise ValueError("knowledge document contains body text before the first H2 section")
            current_lines.append(line)
        if current_section is None:
            raise ValueError("knowledge document must contain at least one H2 section")
        if any(part.strip() for part in current_lines):
            sections.append((current_section, "\n".join(current_lines).strip()))
        return sections


class PolicyVectorStore(Protocol):
    async def ensure_collection(self, vector_size: int) -> None: ...

    async def upsert(self, chunks: list[KnowledgeChunk], vectors: list[list[float]]) -> None: ...

    async def query(self, vector: list[float], limit: int) -> list[dict[str, Any]]: ...


class QdrantPolicyVectorStore:
    def __init__(self, base_url: str, collection: str = "bookstore_policies", *, vector_space=None) -> None:
        self.collection = collection
        self.vector_space = vector_space
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=10)

    async def ensure_collection(self, vector_size: int) -> None:
        if self.vector_space is not None and (vector_size != self.vector_space.dimensions or self.collection != self.vector_space.policy_collection):
            raise ValueError("policy collection vector space mismatch")
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
        configured_size = (
            response.json().get("result", {}).get("config", {}).get("params", {}).get("vectors", {}).get("size")
        )
        if configured_size != vector_size:
            raise ValueError(f"Qdrant policy vector size is {configured_size}, expected {vector_size}")
        if response.json()["result"]["config"]["params"]["vectors"].get("distance") != "Cosine":
            raise ValueError("Qdrant policy collection distance must be Cosine")

    async def upsert(self, chunks: list[KnowledgeChunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunk/vector count mismatch")
        if not chunks:
            return
        response = await self.client.put(
            f"/collections/{self.collection}/points",
            params={"wait": "true"},
            json={
                "points": [
                    {"id": chunk.point_id, "vector": vector, "payload": {**chunk.payload(), **({"embedding_space": self.vector_space.identity} if self.vector_space else {})}}
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ]
            },
        )
        response.raise_for_status()

    async def query(self, vector: list[float], limit: int) -> list[dict[str, Any]]:
        response = await self.client.post(
            f"/collections/{self.collection}/points/query",
            json={"query": vector, "limit": min(limit, 5), "with_payload": True,
                **({"filter": {"must": [{"key": "embedding_space", "match": {"value": self.vector_space.identity}}]}} if self.vector_space else {})},
        )
        if response.status_code == 404:
            return []
        response.raise_for_status()
        return [
            {**{key: value for key, value in point.get("payload", {}).items() if key != "embedding_space"},
             "score": point.get("score", 0)}
            for point in response.json().get("result", {}).get("points", [])
        ]

    async def aclose(self) -> None:
        await self.client.aclose()


class PolicyKnowledgeService:
    def __init__(self, embeddings: EmbeddingProvider, store: PolicyVectorStore, embedding_model: str) -> None:
        self.embeddings = embeddings
        self.store = store
        self.embedding_model = embedding_model

    async def index(self, chunks: list[KnowledgeChunk]) -> int:
        if not chunks:
            return 0
        unique_chunks = list({chunk.content_hash: chunk for chunk in reversed(chunks)}.values())
        unique_chunks.reverse()
        response = await self.embeddings.embed([chunk.text for chunk in unique_chunks], self.embedding_model)
        if not response.vectors or not response.vectors[0]:
            raise ValueError("embedding provider returned an empty vector")
        await self.store.ensure_collection(len(response.vectors[0]))
        if any(len(vector) != len(response.vectors[0]) for vector in response.vectors):
            raise ValueError("embedding vectors have inconsistent dimensions")
        await self.store.upsert(unique_chunks, response.vectors)
        return len(unique_chunks)

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        response = await self.embeddings.embed([query], self.embedding_model)
        if len(response.vectors) != 1:
            raise ValueError("embedding provider returned an invalid query vector")
        return await self.store.query(response.vectors[0], min(limit, 5))
