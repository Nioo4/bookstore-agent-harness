from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from app.memory.types import MemoryCandidate, MemoryType, episode_key_for
from app.models.provider import ModelMessage, ModelProvider, ModelRequest
from app.models.invocation import invoke_model


_CANDIDATE_SIGNAL = re.compile(
    r"(?i)(?:i (?:like|prefer|dislike|avoid|read|finished)|"
    r"我(?:喜欢|偏好|不喜欢|讨厌|读过|看过)|(?:长期|以后|我的).{0,12}偏好|"
    r"(?:请|帮我)?记住(?:[，,:：]|$)|保存.{0,16}?(?:偏好|记忆))"
)


class MemoryExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=10)


class MemoryExtractor:
    def __init__(self, provider: ModelProvider, model: str) -> None:
        self.provider = provider
        self.model = model

    @staticmethod
    def should_extract(content: str) -> bool:
        return _CANDIDATE_SIGNAL.search(content) is not None

    async def extract(self, content: str, *, source_message_id: str) -> list[MemoryCandidate]:
        if not self.should_extract(content):
            return []
        response = await invoke_model(self.provider, caller="memory_extractor",
            request=ModelRequest(
                model=self.model,
                temperature=0,
                response_schema=MemoryExtraction.model_json_schema(),
                messages=[
                    ModelMessage(
                        role="system",
                        content=(
                            "Extract only durable bookstore preferences or a compact reading episode from this one new "
                            "user message. Use only the documented keys and SET/ADD/REMOVE operations. EXPLICIT means "
                            "the user directly stated it and must use confidence 1.0; omit inferred candidates below "
                            "0.85. Never extract passwords, tokens, email, phone, address, payment data, live price, "
                            "stock, or order status. Evidence must be a redacted excerpt no longer than 512 characters. "
                            "For EPISODE use memory_key episode.AUTO; the harness computes the canonical SHA-256 key. "
                            "Return JSON only."
                        ),
                    ),
                    ModelMessage(role="user", content=content),
                ],
            )
        )
        extraction = MemoryExtraction.model_validate_json(response.content)
        normalized_keys: list[MemoryCandidate] = []
        for candidate in extraction.candidates:
            if candidate.memory_type is MemoryType.EPISODE:
                key, value = episode_key_for(candidate.value, source_message_id)
                candidate = candidate.model_copy(update={"memory_key": key, "value": value})
            normalized_keys.append(candidate)
        return normalized_keys


def extraction_trace(results: list[object]) -> str:
    return json.dumps({"candidate_count": len(results)}, separators=(",", ":"))
