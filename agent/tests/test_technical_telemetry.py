from __future__ import annotations

import json
import logging

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import generate_latest

from app import telemetry
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.provider import ModelMessage, ModelRequest
from app.tools.clients import GrpcCatalogClient
from app.tools.schemas import SearchBooksInput
from rpc.check import check_pb2


@pytest.fixture
def spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "TRACER", provider.get_tracer("safe-test"))
    yield exporter
    provider.shutdown()


@pytest.mark.asyncio
async def test_http_model_agent_tool_and_rpc_share_trace_without_payload(spans):
    captured = {}
    async def model_transport(request):
        captured["model"] = request.headers["traceparent"]
        assert "baggage" not in request.headers and "tracestate" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "sensitive-answer"}}], "usage": {"prompt_tokens": 4, "completion_tokens": 3}})

    class Stub:
        async def searchBooks(self, request, timeout, metadata):
            captured["rpc"] = dict(metadata)["traceparent"]
            assert set(dict(metadata)) == {"traceparent"}
            return check_pb2.searchBooksResp()

    catalog = GrpcCatalogClient.__new__(GrpcCatalogClient)
    catalog._stub = Stub()
    async with httpx.AsyncClient(base_url="http://fixture", transport=httpx.MockTransport(model_transport)) as client:
        model = OpenAICompatibleProvider("http://fixture", "secret-key", client=client)
        async def application(scope, receive, send):
            with telemetry.node_span("catalog"):
                await model.complete(ModelRequest(model="private-model", messages=[ModelMessage(role="user", content="sensitive-prompt")]))
                with telemetry.tool_span("search_books"):
                    await catalog.search_books(SearchBooksInput(q="private-query"), timeout=1)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"sensitive-answer"})
        async def discard(_):
            pass
        parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        await telemetry.SafeHTTPMiddleware(application)({"type": "http", "path": "/api/chat/conversations/private-id/messages", "headers": [(b"traceparent", parent.encode()), (b"baggage", b"email=private@example.com")]}, None, discard)

    collected = spans.get_finished_spans()
    assert {span.context.trace_id for span in collected} == {int("a" * 32, 16)}
    assert {span.name for span in collected} == {"http.request", "agent.node", "model.complete", "tool.invoke", "rpc.catalog.search"}
    assert all(value.split("-")[1] == "a" * 32 for value in captured.values())
    serialized = "".join(span.to_json() for span in collected)
    for forbidden in ("sensitive-prompt", "sensitive-answer", "secret-key", "private-query", "private-id", "private@example.com", "private-model"):
        assert forbidden not in serialized


def test_exceptions_and_unknown_labels_never_leak(spans):
    with pytest.raises(RuntimeError):
        with telemetry.node_span("private-person@example.com"):
            with telemetry.tool_span("secret-tool-args"):
                raise RuntimeError("password=secret credential trace")
    serialized = "".join(span.to_json() for span in spans.get_finished_spans())
    assert all(not span.events and not span.status.description for span in spans.get_finished_spans())
    assert "password" not in serialized and "private-person" not in serialized
    metrics = generate_latest().decode()
    assert 'agent="other"' in metrics and 'tool="other"' in metrics
    assert "private-person" not in metrics and "secret-tool" not in metrics


def test_structured_logger_preserves_safe_event_and_drops_arguments():
    formatter = telemetry.SafeLogFormatter()
    record = logging.LogRecord("private-email", logging.ERROR, "secret-path", 1, "inventory event moved to DLQ", ("raw-password",), None)
    assert json.loads(formatter.format(record)) == {"level": "ERROR", "event": "INVENTORY_DEAD_LETTER"}
    record.msg = "password=raw-password"
    assert "raw-password" not in formatter.format(record)


def test_chat_failure_log_links_only_the_active_technical_trace(spans):
    from opentelemetry import trace
    record = logging.LogRecord("private-user@example.test", logging.WARNING, "private-path", 1,
        "chat run failed", ("secret-body",), None)
    with telemetry.technical_span("http.request"):
        expected = format(trace.get_current_span().get_span_context().trace_id, "032x")
        value = json.loads(telemetry.SafeLogFormatter().format(record))
    assert value == {"level": "WARNING", "event": "CHAT_RUN_FAILED", "trace_id": expected}
