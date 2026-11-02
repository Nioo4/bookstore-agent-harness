import json

import httpx
import pytest

from app.models.fake import FakeModelProvider
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.provider import ModelMessage, ModelRequest, ModelResponse


def test_model_schemas_explain_existing_policy_language_and_memory_contracts() -> None:
    from app.agents.verification import AgentDraft, PolicyClaim, UserConstraints
    from app.memory.types import MemoryCandidate
    assert "exact source_path" in PolicyClaim.model_json_schema()["properties"]["source"]["description"]
    assert "zh-CN" in UserConstraints.model_json_schema()["properties"]["language"]["description"]
    memory = MemoryCandidate.model_json_schema()["properties"]
    assert "preference.languages" in memory["memory_key"]["description"]
    assert all(value in memory["value"]["description"] for value in ("items", "zh-CN", "max_amount", "book_ids"))
    assert "minor currency units" in AgentDraft.model_json_schema()["properties"]["answer"]["description"]


@pytest.mark.asyncio
async def test_fake_provider_records_requests() -> None:
    provider = FakeModelProvider([ModelResponse(content="ok", model="fake")])
    request = ModelRequest(model="fake", messages=[ModelMessage(role="user", content="hello")])

    response = await provider.complete(request)

    assert response.content == "ok"
    assert provider.requests == [request]


@pytest.mark.asyncio
async def test_openai_compatible_provider_keeps_vendor_payload_at_boundary() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "gateway-model",
                "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    client = httpx.AsyncClient(base_url="https://gateway.example/v1", transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("https://unused.example", "secret", client=client)
    request = ModelRequest(
        model="agent-model",
        messages=[ModelMessage(role="user", content="hello")],
        response_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
    )

    response = await provider.complete(request)

    assert response.content == "answer"
    assert response.usage.total_tokens == 6
    assert captured["body"]["response_format"]["type"] == "json_schema"  # type: ignore[index]
    await client.aclose()
