from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
import logging

MAX_TOKEN_COUNT = 2**64 - 1


class ModelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    messages: list[ModelMessage] = Field(min_length=1)
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    response_schema: dict[str, Any] | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)


class ModelUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    @field_validator("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens", mode="before")
    @classmethod
    def valid_count(cls, value):
        if value is None or (type(value) is int and 0 <= value <= MAX_TOKEN_COUNT):
            return value
        logging.getLogger(__name__).warning("model_usage_field_invalid")
        return None

    @property
    def reported(self) -> bool:
        return self.prompt_tokens is not None and self.completion_tokens is not None

    @property
    def total_tokens(self) -> int | None:
        return self.prompt_tokens + self.completion_tokens if self.reported else None

    @classmethod
    def from_provider(cls, value, *, deepseek: bool = False) -> "ModelUsage":
        if not isinstance(value, dict):
            if value is not None:
                logging.getLogger(__name__).warning("model_usage_object_invalid")
            value = {}
        prompt_details = value.get("prompt_tokens_details")
        completion_details = value.get("completion_tokens_details")
        return cls(
            prompt_tokens=value.get("prompt_tokens"), completion_tokens=value.get("completion_tokens"),
            cached_tokens=value.get("prompt_cache_hit_tokens") if deepseek else (
                prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None),
            reasoning_tokens=completion_details.get("reasoning_tokens") if isinstance(completion_details, dict) else None,
        )


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    model: str
    usage: ModelUsage = Field(default_factory=ModelUsage)
    finish_reason: str | None = None
    raw_tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ModelProvider(ABC):
    """Provider-neutral model boundary used by every Agent node."""

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError
