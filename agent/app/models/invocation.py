"""Request-local call identity for injected providers; not part of model payloads."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.models.provider import ModelProvider, ModelRequest, ModelResponse


@dataclass(frozen=True)
class ModelInvocation:
    caller: str
    scope: str


_round: ContextVar[str] = ContextVar("model_call_round", default="request")
_invocation: ContextVar[ModelInvocation | None] = ContextVar("model_call_identity", default=None)


@contextmanager
def model_call_round(value: str):
    """A fixed scenario driver supplies the round; never derived from model text."""
    token = _round.set(value)
    try:
        yield
    finally:
        _round.reset(token)


def current_model_invocation() -> ModelInvocation | None:
    return _invocation.get()


async def invoke_model(provider: ModelProvider, *, caller: str, request: ModelRequest,
                       node_scope: str | None = None) -> ModelResponse:
    # Node IDs are passed only from the validated DAG at the executor boundary.
    token = _invocation.set(ModelInvocation(caller, f"{_round.get()}/{node_scope or caller}"))
    try:
        return await provider.complete(request)
    finally:
        _invocation.reset(token)
