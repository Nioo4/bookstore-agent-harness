from __future__ import annotations

import json

import httpx

from app.models.gateway import GatewayHTTP
from app.models.provider import ModelProvider, ModelRequest, ModelResponse, ModelUsage
from app.telemetry import MODEL_TOKENS, traced


# Versioned with the Harness build; never overwrite retained role prompts.
JSON_SCHEMA_INSTRUCTION_V1 = (
    "Return only one JSON object conforming to the complete JSON schema below. "
    "No markdown fences or explanatory text. The schema defines output shape, "
    "not permission to perform tools or override role safety constraints. JSON schema: "
)


class ModelOutputError(ValueError):
    """Invalid structured output; the owning node controls repair, never the provider."""


def _invalid_constant(value: str):
    raise ValueError("non-finite JSON constant")


class DeepSeekJSONProvider(ModelProvider):
    adapter = "deepseek-chat-json-v1"

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 30,
                 *, max_output_tokens: int = 8192, client: httpx.AsyncClient | None = None) -> None:
        if not 1 <= max_output_tokens <= 8192:
            raise ValueError("model output limit must be between 1 and 8192")
        self.max_output_tokens = max_output_tokens
        self.http = GatewayHTTP(base_url, api_key, timeout_seconds, client=client)

    @traced("model.complete")
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.tools:
            raise ModelOutputError("native model tools are not an execution interface")
        messages = [item.model_dump(exclude_none=True) for item in request.messages]
        payload = {
            "model": request.model, "messages": messages, "thinking": {"type": "disabled"},
            "temperature": 0, "stream": False,
            "max_tokens": min(request.max_tokens or self.max_output_tokens, self.max_output_tokens),
        }
        if request.response_schema is not None:
            payload["response_format"] = {"type": "json_object"}
            messages.insert(0, {"role": "system", "content": JSON_SCHEMA_INSTRUCTION_V1 + json.dumps(request.response_schema, ensure_ascii=False, separators=(",", ":"))})
        body = await self.http.post("chat/completions", payload)
        try:
            choice = body["choices"][0]
            message = choice["message"]
            content = message["content"]
            if (not isinstance(content, str) or not content.strip()
                    or choice.get("finish_reason") not in {"stop", None}
                    or message.get("tool_calls") or message.get("function_call")):
                raise ValueError()
            if request.response_schema is not None and not isinstance(json.loads(content, parse_constant=_invalid_constant), dict):
                raise ValueError()
            # Callers still validate their concrete Pydantic models before any
            # tool proposal is considered. reasoning_content is never copied.
            result = ModelResponse(
                content=content, model=body.get("model") or request.model,
                finish_reason=choice.get("finish_reason"),
                usage=ModelUsage.from_provider(body.get("usage"), deepseek=True),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelOutputError("model returned invalid or incomplete structured output") from None
        if result.usage.prompt_tokens is not None:
            MODEL_TOKENS.labels("input").inc(result.usage.prompt_tokens)
        if result.usage.completion_tokens is not None:
            MODEL_TOKENS.labels("output").inc(result.usage.completion_tokens)
        return result

    async def aclose(self) -> None:
        await self.http.aclose()
