"""Strict, response-only model scripts. No business outcomes or oracle inputs."""
from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.gateway import GatewayRequestError
from app.models.provider import ModelProvider, ModelRequest, ModelResponse
from app.models.invocation import current_model_invocation
from app.trace.errors import TraceError
from app.trace.types import sha256_json

Caller = Literal["intent_extractor", "supervisor", "catalog", "recommendation", "knowledge",
                 "order", "verifier", "answer_synthesizer", "memory_extractor"]


class ScriptedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    caller: Caller
    call_scope: str = Field(min_length=1, max_length=191)
    ordinal: int = Field(ge=1, strict=True)
    response_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["RESPONSE", "ERROR"]
    response: ModelResponse | None
    error_code: Literal["DEPENDENCY_UNAVAILABLE"] | None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ScriptedResponse":
        if self.outcome == "RESPONSE":
            if self.response is None or self.error_code is not None:
                raise ValueError("RESPONSE requires response and no error code")
        elif self.response is not None or self.error_code != "DEPENDENCY_UNAVAILABLE":
            raise ValueError("ERROR requires only a supported error code")
        return self


class FakeModelScriptV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["v2"]
    responses: list[ScriptedResponse]

    @model_validator(mode="after")
    def validate_ordinals(self) -> "FakeModelScriptV2":
        groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        for item in self.responses:
            groups[item.caller, item.call_scope].append(item.ordinal)
        if any(sorted(values) != list(range(1, len(values) + 1)) for values in groups.values()):
            raise ValueError("script ordinals must be unique and contiguous from one per scope")
        return self


class ScriptQueue:
    """Drivers bind identities explicitly at existing DI points, never from text.

    Each node has an independent sequence, so parallel scheduling cannot select
    another node's response. This class never opens a network client.
    """
    def __init__(self, script: FakeModelScriptV2) -> None:
        self._responses = {(item.caller, item.call_scope, item.ordinal): item for item in script.responses}
        self._next: dict[tuple[str, str], int] = defaultdict(lambda: 1)
        self.requests: list[tuple[str, str, int, str]] = []  # Safe metadata only.
        self._invalid = False

    def _fail(self, message: str):
        # Production nodes may safely catch a provider exception. The driver
        # must still surface a dataset error, even if the node returns fallback.
        self._invalid = True
        raise TraceError("EVAL_DATASET_INVALID", message)

    def bind(self, caller: Caller, call_scope: str) -> ModelProvider:
        if not any(key[:2] == (caller, call_scope) for key in self._responses):
            self._fail("unknown scripted model caller or scope")
        return _BoundProvider(self, caller, call_scope)

    def production_provider(self) -> ModelProvider:
        return _ProductionProvider(self)

    async def complete(self, caller: Caller, scope: str, request: ModelRequest) -> ModelResponse:
        ordinal = self._next[caller, scope]
        item = self._responses.get((caller, scope, ordinal))
        digest = sha256_json(request.response_schema).hex()
        if item is None or digest != item.response_schema_sha256:
            self._fail("script queue exhausted or response Schema hash mismatch")
        if request.model != "deterministic-fake-v2":
            self._fail("v2 scripts require the v2 deterministic model")
        self._next[caller, scope] += 1
        self.requests.append((caller, scope, ordinal, digest))
        if item.outcome == "ERROR":
            raise GatewayRequestError("synthetic model dependency unavailable")
        assert item.response is not None
        if item.response.model != request.model:
            self._fail("script model does not match the selected v2 profile")
        return item.response.model_copy(deep=True)

    def assert_consumed(self) -> None:
        if self._invalid:
            raise TraceError("EVAL_DATASET_INVALID", "scenario encountered a scripted provider data error")
        if any(ordinal >= self._next[caller, scope] for caller, scope, ordinal in self._responses):
            raise TraceError("EVAL_DATASET_INVALID", "scenario left unconsumed model responses")


class _BoundProvider(ModelProvider):
    def __init__(self, queue: ScriptQueue, caller: Caller, scope: str) -> None:
        self.queue, self.caller, self.scope = queue, caller, scope

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return await self.queue.complete(self.caller, self.scope, request)


class _ProductionProvider(ModelProvider):
    def __init__(self, queue: ScriptQueue) -> None:
        self.queue = queue

    async def complete(self, request: ModelRequest) -> ModelResponse:
        identity = current_model_invocation()
        if identity is None:
            self.queue._fail("production model call has no explicit caller/scope")
        return await self.queue.complete(identity.caller, identity.scope, request)
