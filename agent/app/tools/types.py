from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    USER_SCOPED_READ = "USER_SCOPED_READ"
    PREPARE = "PREPARE"
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
    COMMIT = "COMMIT"


@runtime_checkable
class ToolCallBudget(Protocol):
    async def consume(self) -> None: ...


@runtime_checkable
class ToolPolicy(Protocol):
    async def enforce(
        self,
        tool_name: str,
        risk: RiskLevel,
        arguments: BaseModel,
        context: "ToolContext",
    ) -> None: ...


@runtime_checkable
class ToolEventSink(Protocol):
    async def emit(self, event_type: str, data: dict[str, Any]) -> None: ...


class ToolContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    trace_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    access_token: str | None = None
    authenticated_user_id: int | None = Field(default=None, gt=0)
    email_verified: bool = False
    user_confirmed: bool = False
    guest_mode: bool = False
    tool_budget: ToolCallBudget | None = Field(default=None, exclude=True)
    event_sink: ToolEventSink | None = Field(default=None, exclude=True)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: dict[str, Any]
    evidence_ids: list[str] = Field(default_factory=list)


ToolExecutor = Callable[[BaseModel, ToolContext], Awaitable[ToolResult]]


class TraceProjectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(min_length=1, max_length=64)
    request_fields: frozenset[str]
    response_fields: frozenset[str]
    simulated_on_replay: bool = False

    def project_request(self, value: dict[str, Any]) -> dict[str, Any]:
        return _allow(value, self.request_fields)

    def project_response(self, value: dict[str, Any], *, for_replay: bool = False) -> dict[str, Any]:
        projected = _allow(value, self.response_fields)
        if for_replay and self.simulated_on_replay:
            projected["simulated"] = True
        return projected


class ReplayToolDefinition(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)

    name: str
    input_model: type[BaseModel]
    risk: RiskLevel
    required_permissions: frozenset[str]
    trace_projection: TraceProjectionSpec

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "input_schema": self.input_model.model_json_schema(),
            "risk": self.risk.value,
            "permissions": sorted(self.required_permissions),
            "trace_projection": {
                "schema_version": self.trace_projection.schema_version,
                "request_fields": sorted(self.trace_projection.request_fields),
                "response_fields": sorted(self.trace_projection.response_fields),
                "simulated_on_replay": self.trace_projection.simulated_on_replay,
            },
        }


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    input_model: type[BaseModel]
    risk: RiskLevel
    timeout_seconds: float = Field(gt=0, le=60)
    max_retries: int = Field(ge=0, le=3)
    idempotent: bool
    required_permissions: frozenset[str]
    error_codes: frozenset[str] = Field(min_length=1)
    trace_projection: TraceProjectionSpec | None = None
    executor: ToolExecutor = Field(exclude=True)

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "risk": self.risk.value,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "idempotent": self.idempotent,
            "permissions": sorted(self.required_permissions),
            "error_codes": sorted(self.error_codes),
            "trace_projection": (
                {
                    "schema_version": self.trace_projection.schema_version,
                    "request_fields": sorted(self.trace_projection.request_fields),
                    "response_fields": sorted(self.trace_projection.response_fields),
                    "simulated_on_replay": self.trace_projection.simulated_on_replay,
                }
                if self.trace_projection is not None
                else None
            ),
        }


def _allow(value: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    if "*" in fields:
        return dict(value)
    return {name: value[name] for name in sorted(fields) if name in value}
