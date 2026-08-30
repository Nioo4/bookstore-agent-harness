from __future__ import annotations

from typing import Any

import httpx

from app.models.provider import ModelProvider, ModelRequest, ModelResponse, ModelUsage
from app.models.gateway import GatewayHTTP, GatewayRequestError
from app.telemetry import MODEL_TOKENS, propagation_headers, traced


class ModelProviderError(RuntimeError):
    pass


class OpenAICompatibleProvider(ModelProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30,
        *,
        client: httpx.AsyncClient | None = None,
        max_output_tokens: int = 8192,
    ) -> None:
        self.http = GatewayHTTP(base_url, api_key, timeout_seconds, client=client)
        self.max_output_tokens = max_output_tokens

    @traced("model.complete")
    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message.model_dump(exclude_none=True) for message in request.messages],
            "temperature": request.temperature,
            "stream": False,
            "max_tokens": min(request.max_tokens or self.max_output_tokens, self.max_output_tokens),
        }
        if request.tools:
            payload["tools"] = request.tools
        if request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "agent_output", "strict": True, "schema": request.response_schema},
            }

        try:
            body = await self.http.post("chat/completions", payload)
            choice = body["choices"][0]
            message = choice["message"]
            result = ModelResponse(
                content=message.get("content") or "",
                model=body.get("model", request.model),
                finish_reason=choice.get("finish_reason"),
                raw_tool_calls=message.get("tool_calls", []),
                usage=ModelUsage.from_provider(body.get("usage")),
            )
            if result.usage.prompt_tokens is not None:
                MODEL_TOKENS.labels("input").inc(result.usage.prompt_tokens)
            if result.usage.completion_tokens is not None:
                MODEL_TOKENS.labels("output").inc(result.usage.completion_tokens)
            return result
        except (GatewayRequestError, KeyError, IndexError, TypeError, ValueError):
            raise ModelProviderError("model gateway request failed") from None

    async def aclose(self) -> None:
        await self.http.aclose()
