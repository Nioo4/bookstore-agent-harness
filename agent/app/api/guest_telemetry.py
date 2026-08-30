from __future__ import annotations

from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer


class GuestRequestSpan(Protocol):
    def complete(self) -> None: ...

    def fail(self, error_code: str) -> None: ...


class GuestTelemetry(Protocol):
    def request_span(self, *, model_profile: str) -> AbstractAsyncContextManager[GuestRequestSpan]: ...


@dataclass
class _OpenTelemetryGuestSpan:
    span: Span

    def complete(self) -> None:
        self.span.set_attribute("run.status", "COMPLETED")
        self.span.set_status(Status(StatusCode.OK))

    def fail(self, error_code: str) -> None:
        self.span.set_attribute("run.status", "FAILED")
        self.span.set_attribute("error.code", error_code[:64])
        self.span.set_status(Status(StatusCode.ERROR))


class OpenTelemetryGuestTelemetry:
    def __init__(self, tracer: Tracer | None = None) -> None:
        self.tracer = tracer or trace.get_tracer("bookstore.agent.guest", "0.1.0")

    @asynccontextmanager
    async def request_span(self, *, model_profile: str) -> AsyncIterator[GuestRequestSpan]:
        with self.tracer.start_as_current_span(
            "guest.chat",
            record_exception=False,
            set_status_on_exception=False,
            attributes={
                "service.name": "bookstore-agent",
                "http.route": "/api/chat/conversations/{conversation_id}/messages",
                "agent.model_profile": model_profile,
                "trace.persistent": False,
            },
        ) as span:
            yield _OpenTelemetryGuestSpan(span)
