"""Technical telemetry: fixed names/labels only; never serialize application data."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import contextmanager
from functools import wraps

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import Counter, Gauge, Histogram

TRACER = trace.get_tracer("bookstore.agent", "0.1.0")
PROPAGATOR = TraceContextTextMapPropagator()  # Never extract/forward baggage.
AGENTS = frozenset({"catalog", "recommendation", "knowledge", "order", "memory", "verifier"})
TOOLS = frozenset({"search_books", "get_book", "search_policy", "prepare_order", "create_order", "get_order", "list_my_orders"})
TASKS = Counter("bookstore_agent_tasks_total", "Chat executions by final delivery status", ["status"])
NODE_SECONDS = Histogram("bookstore_agent_node_duration_seconds", "Agent node execution duration", ["agent", "status"])
TOOL_ERRORS = Counter("bookstore_agent_tool_errors_total", "Failed tool invocation attempts", ["tool"])
VERIFIER_FAILURES = Counter("bookstore_agent_verifier_failures_total", "Rejected verifier passes", ["verifier"])
MODEL_TOKENS = Counter("bookstore_agent_model_tokens_total", "Provider-reported model token usage", ["direction"])
EVENT_RETRIES = Counter("bookstore_agent_event_retries_total", "Actual repeated inventory processing attempts")
EVENT_DEAD = Counter("bookstore_agent_event_dead_letters_total", "Inventory entries sent to dead letter queue")
TASK_BACKLOG = Gauge("bookstore_agent_task_backlog", "Authoritative active/triggered task count", ["status"])


def bounded(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "other"


@contextmanager
def technical_span(name: str, *, attributes=None, kind=SpanKind.INTERNAL, context=None):
    # All callers pass static names and allowlisted attributes. Exception text/stack
    # can contain credentials, so explicitly disable SDK exception recording.
    with TRACER.start_as_current_span(
        name, attributes=attributes, kind=kind, context=context,
        record_exception=False, set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except asyncio.CancelledError:
            span.set_attribute("operation.status", "CANCELLED")
            raise
        except Exception:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.code", "DEPENDENCY_FAILURE")
            raise


def traced(name: str):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with technical_span(name):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def node_span(agent: str):
    label = bounded(agent, AGENTS)
    started, status = time.monotonic(), "FAILED"
    try:
        with technical_span("agent.node", attributes={"agent.name": label}):
            yield
            status = "COMPLETED"
    finally:
        NODE_SECONDS.labels(label, status).observe(time.monotonic() - started)


@contextmanager
def tool_span(tool: str):
    label = bounded(tool, TOOLS)
    with technical_span("tool.invoke", attributes={"tool.name": label}):
        try:
            yield
        except Exception:
            TOOL_ERRORS.labels(label).inc()
            raise


def propagation_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    PROPAGATOR.inject(headers)
    # Trace state can contain vendor/user data. Only random trace context leaves.
    headers.pop("tracestate", None)
    return headers


def rpc_metadata() -> list[tuple[str, str]]:
    return list(propagation_headers().items())


class SafeHTTPMiddleware:
    """ASGI (not BaseHTTPMiddleware): spans include SSE execution/cancellation."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in {"/metrics", "/healthz"}:
            return await self.app(scope, receive, send)
        carrier = {"traceparent": dict(scope.get("headers", [])).get(b"traceparent", b"").decode("ascii", "ignore")}
        with technical_span("http.request", kind=SpanKind.SERVER, context=PROPAGATOR.extract(carrier)) as span:
            async def safe_send(message):
                if message["type"] == "http.response.start":
                    span.set_attribute("http.response.status_code", message["status"])
                    if message["status"] >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                    route = scope.get("route")
                    if route is not None:
                        span.set_attribute("http.route", route.path)
                await send(message)
            await self.app(scope, receive, safe_send)


class SafeLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # No raw message, argument, traceback, logger-supplied identity or secret.
        events = {
            "inventory event moved to DLQ": "INVENTORY_DEAD_LETTER",
            "inventory event processing failed; entry remains pending": "INVENTORY_RETRY_PENDING",
            "triggered task recovery deferred to startup recovery": "TASK_RECOVERY_DEFERRED",
            "inventory pending claim cursor did not advance": "INVENTORY_CLAIM_STALLED",
            "inventory task expiry scan failed": "TASK_EXPIRY_SCAN_FAILED",
            "inventory stream trim failed": "INVENTORY_TRIM_FAILED",
            "failed to persist replay terminal failure": "REPLAY_TERMINAL_WRITE_FAILED",
            "failed to purge expired trace state": "TRACE_PURGE_FAILED",
            "chat run failed": "CHAT_RUN_FAILED",
        }
        event = events.get(record.msg, "RUNTIME_LOG") if isinstance(record.msg, str) else "RUNTIME_LOG"
        projected = {"level": record.levelname, "event": event}
        if event == "CHAT_RUN_FAILED":
            context = trace.get_current_span().get_span_context()
            if context.is_valid:
                projected["trace_id"] = format(context.trace_id, "032x")
        return json.dumps(projected)


def configure_telemetry(service: str):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    handler = logging.StreamHandler()
    handler.setFormatter(SafeLogFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    provider = TracerProvider(resource=Resource({"service.name": service, "service.version": "0.1.0"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=5)))
    trace.set_tracer_provider(provider)
    return provider
