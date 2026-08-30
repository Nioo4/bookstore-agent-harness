from __future__ import annotations

from abc import ABC, abstractmethod
import math

import httpx
from pydantic import BaseModel, ConfigDict, Field
from app.telemetry import MODEL_TOKENS, traced
from app.models.gateway import GatewayHTTP


class EmbeddingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vectors: list[list[float]]
    prompt_tokens: int | None = Field(default=None, ge=0)


class EmbeddingProvider(ABC):
    @abstractmethod
    async def embed(self, texts: list[str], model: str) -> EmbeddingResponse:
        raise NotImplementedError


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 30, *,
                 dimensions: int = 1024, batch_size: int = 20, client: httpx.AsyncClient | None = None) -> None:
        if dimensions <= 0 or not 1 <= batch_size <= 20:
            raise ValueError("invalid embedding dimensions or batch size")
        self.dimensions, self.batch_size = dimensions, batch_size
        self.http = GatewayHTTP(base_url, api_key, timeout_seconds, client=client)

    @traced("model.embed")
    async def embed(self, texts: list[str], model: str) -> EmbeddingResponse:
        if not texts:
            return EmbeddingResponse(vectors=[], prompt_tokens=0)
        vectors: list[list[float]] = []
        tokens: int | None = 0
        try:
            for offset in range(0, len(texts), self.batch_size):
                batch = texts[offset:offset + self.batch_size]
                body = await self.http.post("embeddings", {"model": model, "input": batch,
                    "dimensions": self.dimensions, "encoding_format": "float"})
                data = body["data"]
                if (not isinstance(data, list) or len(data) != len(batch)
                        or any(type(item.get("index")) is not int for item in data)
                        or {item["index"] for item in data} != set(range(len(batch)))):
                    raise ValueError()
                for item in sorted(data, key=lambda item: item["index"]):
                    vector = item["embedding"]
                    if (not isinstance(vector, list) or len(vector) != self.dimensions
                            or any(type(v) not in {int, float} or not math.isfinite(v) for v in vector)):
                        raise ValueError()
                    vectors.append(vector)
                count = (body.get("usage") or {}).get("prompt_tokens")
                if type(count) is not int or count < 0:
                    tokens = None
                elif tokens is not None:
                    tokens += count
            result = EmbeddingResponse(vectors=vectors, prompt_tokens=tokens)
            if tokens is not None:
                MODEL_TOKENS.labels("embedding").inc(tokens)
            return result
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise RuntimeError("embedding gateway returned invalid vectors") from None

    async def aclose(self) -> None:
        await self.http.aclose()


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.requests: list[tuple[list[str], str]] = []

    async def embed(self, texts: list[str], model: str) -> EmbeddingResponse:
        self.requests.append((texts, model))
        if len(texts) != len(self.vectors):
            raise AssertionError("fake embedding count mismatch")
        return EmbeddingResponse(vectors=self.vectors)
