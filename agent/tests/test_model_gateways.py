from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.models.configuration import ModelSettings
from app.models.deepseek import DeepSeekJSONProvider, ModelOutputError
from app.models.embeddings import OpenAICompatibleEmbeddingProvider
from app.models.factory import ModelRuntime
from app.models.gateway import GatewayRequestError, gateway_url
from app.models.provider import ModelMessage, ModelRequest
from app.trace.pricing import PricingCalculator


def settings(**overrides):
    return ModelSettings(_env_file=None, **{
        "model_base_url": "https://chat.example", "model_api_key": "synthetic-chat-key",
        "embedding_base_url": "https://ws-bl3h1eur581tnntn.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "embedding_api_key": "synthetic-vector-key", **overrides,
    })


def request(**overrides):
    return ModelRequest(**{"model": "deepseek-v4-flash", "messages": [ModelMessage(role="user", content="synthetic question")],
                           "response_schema": {"type":"object", "properties":{"ok":{"type":"boolean"}}, "required":["ok"], "additionalProperties":False}, **overrides})


def completion(content='{"ok":true}', **message):
    return {"model":"deepseek-v4-flash", "choices":[{"finish_reason":"stop", "message":{"content":content, **message}}]}


@pytest.mark.asyncio
async def test_factory_separates_credentials_preserves_workspace_and_schema():
    seen = []
    def transport(req):
        payload = json.loads(req.content)
        seen.append((str(req.url), req.headers["authorization"], payload))
        if req.url.path.endswith("embeddings"):
            return httpx.Response(200, json={"data":[{"index":0, "embedding":[.5]*1024}]})
        return httpx.Response(200, json=completion(reasoning_content="private-reasoning-must-not-escape"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        runtime = ModelRuntime(settings())
        chat, embedding = runtime.chat(client=client), runtime.embeddings(client=client)
        response = await chat.complete(request(max_tokens=99999))
        vector = await embedding.embed(["synthetic"], "qwen3.7-text-embedding")
        assert seen[0][:2] == ("https://chat.example/chat/completions", "Bearer synthetic-chat-key")
        assert seen[1][:2] == (settings().embedding_base_url + "/embeddings", "Bearer synthetic-vector-key")
        payload = seen[0][2]
        assert payload["response_format"] == {"type":"json_object"}
        assert payload["thinking"] == {"type":"disabled"} and payload["temperature"] == 0 and payload["stream"] is False
        assert payload["max_tokens"] == 8192 and "tools" not in payload
        assert json.dumps(request().response_schema, separators=(",", ":")) in payload["messages"][0]["content"]
        assert "synthetic question" == payload["messages"][1]["content"]
        assert seen[1][2]["dimensions"] == 1024 and seen[1][2]["encoding_format"] == "float"
        assert vector.prompt_tokens is None and not response.usage.reported
        assert "private-reasoning" not in response.model_dump_json()
        assert "authorization" not in client.headers  # No shared client mutation.


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [completion(""), completion("[]"), completion("invalid json"), completion('{"ok":NaN}'),
    completion(tool_calls=[{"id":"native"}]), {"choices":[]},
    {"choices":[{"finish_reason":"length", "message":{"content":'{"ok":true}'}}]}])
async def test_deepseek_rejects_unsafe_or_incomplete_output_without_provider_retry(body):
    calls = 0
    def transport(req):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = DeepSeekJSONProvider("https://chat.example", "synthetic", client=client)
        with pytest.raises(ModelOutputError): await provider.complete(request())
        assert calls == 1


@pytest.mark.asyncio
async def test_no_schema_and_lower_token_budget_are_preserved():
    def transport(req):
        payload = json.loads(req.content)
        assert "response_format" not in payload and payload["max_tokens"] == 100
        return httpx.Response(200, json=completion("plain text"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        assert (await DeepSeekJSONProvider("https://chat.example", "synthetic", client=client).complete(request(response_schema=None, max_tokens=100))).content == "plain text"


@pytest.mark.asyncio
async def test_gateway_does_not_follow_redirect_or_repeat_requests():
    calls = []
    def transport(req):
        calls.append(req.url.host)
        return httpx.Response(307, headers={"location":"https://untrusted.example/collect"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        provider = DeepSeekJSONProvider("https://chat.example", "synthetic", client=client)
        with pytest.raises(GatewayRequestError): await provider.complete(request())
        assert calls == ["chat.example"]


@pytest.mark.asyncio
async def test_total_deadline_and_external_cancellation_close_pending_request():
    cancelled = asyncio.Event()
    async def transport(req):
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = DeepSeekJSONProvider("https://chat.example", "synthetic", timeout_seconds=.03, client=client)
        with pytest.raises(GatewayRequestError): await provider.complete(request())
        assert cancelled.is_set()
        cancelled.clear()
        provider.http.timeout = 30
        task = asyncio.create_task(provider.complete(request()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert cancelled.is_set()


@pytest.mark.asyncio
async def test_embedding_batches_and_reorders_indices_and_empty_input_has_no_network():
    batches = []
    def transport(req):
        payload = json.loads(req.content); batch = payload["input"]; batches.append(len(batch))
        return httpx.Response(200, json={"data":[{"index":i,"embedding":[float(batch[i])]*1024} for i in reversed(range(len(batch)))],"usage":{"prompt_tokens":len(batch)}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = OpenAICompatibleEmbeddingProvider("https://vector.example/v1", "synthetic", client=client)
        assert (await provider.embed([], "qwen3.7-text-embedding")).vectors == [] and batches == []
        result = await provider.embed([str(i) for i in range(41)], "qwen3.7-text-embedding")
        assert batches == [20,20,1] and result.prompt_tokens == 41
        assert [v[0] for v in result.vectors] == list(range(41))


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [[], [{"index":1,"embedding":[0]*1024}], [{"index":True,"embedding":[0]*1024}],
    [{"index":0,"embedding":[0]*32}], [{"index":0,"embedding":[True]*1024}], [{"index":0,"embedding":["0"]*1024}],
    [{"index":0,"embedding":[0]*1024}, {"index":0,"embedding":[0]*1024}]])
async def test_invalid_embedding_response_is_never_returned_to_storage(data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200,json={"data":data}))) as client:
        provider = OpenAICompatibleEmbeddingProvider("https://vector.example/v1", "synthetic", client=client)
        with pytest.raises(RuntimeError): await provider.embed(["one"], "qwen3.7-text-embedding")


@pytest.mark.parametrize("url", ["ftp://chat.example", "https://user:pass@chat.example", "https://chat.example?key=x", "https://chat.example/#x", "https://chat.example/%2e%2e/secret", "https://chat.example/a\\b", "https:///missing"])
def test_gateway_rejects_unsafe_url(url):
    with pytest.raises(ValueError): gateway_url(url)


def test_configuration_and_selected_versions():
    for key in ("model_api_key", "embedding_api_key"):
        for value in ("", "replace-me", "replace-with-provider-key"):
            with pytest.raises(ValidationError): settings(**{key:value})
    with pytest.raises(ValidationError): settings(embedding_dimensions=32)
    with pytest.raises(ValidationError): settings(embedding_batch_size=21)
    with pytest.raises(ValueError, match="selected profile"): ModelRuntime(settings(agent_model="wrong-model"))
    runtime = ModelRuntime(settings())
    assert runtime.profile.name == "deepseek-flash-v2"
    assert runtime.versions.model_profile("default").agent == "agent-model"
    assert runtime.versions.prompt_text("v1", "catalog").startswith("Use only authoritative")
    assert runtime.versions.prompt_set_hash("v1") != runtime.versions.prompt_set_hash("v2-deepseek-json")
    assert runtime.versions.model_profile_hash("default") != runtime.versions.model_profile_hash("deepseek-flash-v1")
    assert "Return only the strict" in runtime.prompt("single_agent")
