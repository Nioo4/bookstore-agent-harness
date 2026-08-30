from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.models.provider import MAX_TOKEN_COUNT


class RunKind(StrEnum):
    LIVE = "LIVE"
    REPLAY = "REPLAY"
    EVALUATION = "EVALUATION"


class WorkflowType(StrEnum):
    CHAT = "CHAT"
    INVENTORY_WATCH = "INVENTORY_WATCH"
    EVALUATION_CASE = "EVALUATION_CASE"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    WAITING_EVENT = "WAITING_EVENT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


class TraceStatus(StrEnum):
    RECORDING = "RECORDING"
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class ManifestReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_profile: str
    model_manifest_hash: bytes
    prompt_set_version: str
    prompt_manifest_hash: bytes
    tool_registry_version: str
    pricing_version: str | None = None


class ReplayMode(StrEnum):
    NONE = "NONE"
    FIXED_TOOL_RESULTS = "FIXED_TOOL_RESULTS"


class CostStatus(StrEnum):
    CALCULATED = "CALCULATED"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


class StepType(StrEnum):
    PLAN = "PLAN"
    AGENT = "AGENT"
    TOOL = "TOOL"
    VERIFICATION = "VERIFICATION"
    REPAIR = "REPAIR"
    SYSTEM = "SYSTEM"


class StepStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class FixtureOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    BUSINESS_ERROR = "BUSINESS_ERROR"
    TECHNICAL_ERROR = "TECHNICAL_ERROR"


class UsageStatus(StrEnum):
    NO_CALLS = "NO_CALLS"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    LEGACY_UNKNOWN = "LEGACY_UNKNOWN"


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: int | None = Field(default=None, ge=0, le=MAX_TOKEN_COUNT, strict=True)
    completion: int | None = Field(default=None, ge=0, le=MAX_TOKEN_COUNT, strict=True)
    cached: int | None = Field(default=None, ge=0, le=MAX_TOKEN_COUNT, strict=True)
    reasoning: int | None = Field(default=None, ge=0, le=MAX_TOKEN_COUNT, strict=True)

    @classmethod
    def no_calls(cls) -> "TokenUsage":
        return cls(prompt=0, completion=0, cached=0, reasoning=0)

    def call_status(self) -> UsageStatus:
        if self.prompt is not None and self.completion is not None:
            return UsageStatus.COMPLETE
        if self.prompt is not None or self.completion is not None:
            return UsageStatus.PARTIAL
        return UsageStatus.UNAVAILABLE


def validate_usage(usage: TokenUsage, status: UsageStatus, *, step: bool = False) -> None:
    if status is UsageStatus.NO_CALLS and usage != TokenUsage.no_calls():
        raise ValueError("NO_CALLS requires four explicit zero fields")
    if status is UsageStatus.COMPLETE and usage.call_status() is not UsageStatus.COMPLETE:
        raise ValueError("COMPLETE requires both base counts")
    if status is UsageStatus.UNAVAILABLE and (usage.prompt is not None or usage.completion is not None):
        raise ValueError("UNAVAILABLE cannot publish base counts")
    if status is UsageStatus.PARTIAL and (usage.call_status() is UsageStatus.COMPLETE or (
            step and usage.call_status() is not UsageStatus.PARTIAL)):
        raise ValueError("invalid PARTIAL base counts")


def validate_step_usage(item) -> None:
    validate_usage(item.token_usage, item.usage_status, step=True)
    if item.usage_status is UsageStatus.LEGACY_UNKNOWN:
        return
    if item.node_id.startswith("model:"):
        if (item.step_type is not StepType.SYSTEM or len(item.node_id) <= 6
                or not item.model_profile or item.usage_status is UsageStatus.NO_CALLS):
            raise ValueError("invalid model call step")
    elif item.usage_status is not UsageStatus.NO_CALLS:
        raise ValueError("non-model steps cannot record direct model usage")


class TraceCost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CostStatus
    known_microusd: int | None = Field(default=None, ge=0)
    currency: str = "USD"
    pricing_version: str | None = None

    @model_validator(mode="after")
    def validate_cost(self) -> "TraceCost":
        if self.currency != "USD":
            raise ValueError("trace cost currency must be USD")
        if self.status is CostStatus.CALCULATED and self.known_microusd is None:
            raise ValueError("calculated cost requires an amount")
        if self.status is CostStatus.PARTIAL and self.known_microusd is None:
            raise ValueError("partial cost requires a known subtotal")
        if self.status is CostStatus.UNAVAILABLE and self.known_microusd is not None:
            raise ValueError("unavailable cost must not contain an amount")
        return self


class TraceRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    source_run_id: str | None = None
    user_id: int | None = None
    conversation_id: str | None = None
    run_kind: RunKind
    workflow_type: WorkflowType
    status: RunStatus
    trace_status: TraceStatus
    replay_mode: ReplayMode = ReplayMode.NONE
    replayable: bool = False
    replay_block_reason: str | None = None
    input: dict[str, Any]
    dag: dict[str, Any] | None = None
    final_answer: str | None = None
    model_profile: str
    model_manifest_hash: bytes
    prompt_set_version: str
    prompt_manifest_hash: bytes
    tool_registry_version: str
    harness_version: str
    token_usage: TokenUsage = Field(default_factory=TokenUsage.no_calls)
    usage_status: UsageStatus = UsageStatus.NO_CALLS
    cost: TraceCost
    wall_duration_ms: int | None = Field(default=None, ge=0)
    active_duration_ms: int | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)
    unused_fixture_count: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    request_idempotency_key: str | None = None
    request_hash: bytes | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    retention_expires_at: datetime
    deleted_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_run_cost(self) -> "TraceRun":
        validate_usage(self.token_usage, self.usage_status)
        if self.cost.status is CostStatus.CALCULATED and self.cost.pricing_version is None:
            raise ValueError("a calculated run cost requires a pricing version")
        return self

    def public_summary(self) -> dict[str, Any]:
        unpublished = self.status in {RunStatus.QUEUED, RunStatus.RUNNING}
        legacy = self.usage_status is UsageStatus.LEGACY_UNKNOWN
        # input is the persisted safety projection, never the raw chat request.
        question = self.input.get("content")
        return {
            "run_id": self.run_id,
            "user_question": question if isinstance(question, str) and question.strip() else None,
            "source_run_id": self.source_run_id,
            "run_kind": self.run_kind.value,
            "workflow_type": self.workflow_type.value,
            "status": self.status.value,
            "trace_status": self.trace_status.value,
            "conversation_id": self.conversation_id,
            "model_profile": self.model_profile,
            "prompt_set_version": self.prompt_set_version,
            "tool_registry_version": self.tool_registry_version,
            "replayable": self.replayable,
            "token_usage": (TokenUsage() if unpublished or legacy else self.token_usage).model_dump(mode="json"),
            "usage_status": (UsageStatus.LEGACY_UNKNOWN if legacy else UsageStatus.UNAVAILABLE if unpublished else self.usage_status).value,
            "cost": (TraceCost(status=CostStatus.UNAVAILABLE) if unpublished or legacy else self.cost).model_dump(mode="json"),
            "wall_duration_ms": self.wall_duration_ms,
            "active_duration_ms": self.active_duration_ms,
            "retry_count": self.retry_count,
            "created_at": utc_text(self.created_at),
            "completed_at": utc_text(self.completed_at),
        }

    def public_detail(self) -> dict[str, Any]:
        value = self.public_summary()
        value.update(
            {
                "input": self.input,
                "dag": self.dag,
                "final_answer": self.final_answer,
                "error": (
                    {"code": self.error_code, "message": self.error_message}
                    if self.error_code or self.error_message
                    else None
                ),
                "unused_fixture_count": self.unused_fixture_count,
            }
        )
        return value


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    run_id: str
    parent_step_id: str | None = None
    sequence_no: int = Field(gt=0)
    node_id: str
    step_type: StepType
    agent_name: str | None = None
    tool_name: str | None = None
    status: StepStatus
    attempt_no: int = Field(default=1, gt=0)
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    model_profile: str | None = None
    prompt_version: str | None = None
    tool_version: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage.no_calls)
    usage_status: UsageStatus = UsageStatus.NO_CALLS
    cost: TraceCost
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    created_at: datetime

    @model_validator(mode="after")
    def validate_direct_usage(self) -> "TraceStep":
        validate_step_usage(self)
        return self

    def public(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "parent_step_id": self.parent_step_id,
            "sequence_no": self.sequence_no,
            "node_id": self.node_id,
            "step_type": self.step_type.value,
            "agent_name": self.agent_name,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "attempt_no": self.attempt_no,
            "input": self.input,
            "output": self.output,
            "evidence_ids": self.evidence_ids,
            "model_profile": self.model_profile,
            "prompt_version": self.prompt_version,
            "tool_version": self.tool_version,
            "token_usage": (TokenUsage() if self.usage_status is UsageStatus.LEGACY_UNKNOWN else self.token_usage).model_dump(mode="json"),
            "usage_status": self.usage_status.value,
            "cost": (TraceCost(status=CostStatus.UNAVAILABLE) if self.usage_status is UsageStatus.LEGACY_UNKNOWN else self.cost).model_dump(mode="json"),
            "error": (
                {"code": self.error_code, "message": self.error_message}
                if self.error_code or self.error_message
                else None
            ),
            "started_at": utc_text(self.started_at),
            "completed_at": utc_text(self.completed_at),
            "duration_ms": self.duration_ms,
        }


class StepDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    run_id: str
    parent_step_id: str | None = None
    node_id: str
    step_type: StepType
    agent_name: str | None = None
    tool_name: str | None = None
    attempt_no: int = Field(default=1, gt=0)
    input: dict[str, Any] | None = None
    model_profile: str | None = None
    prompt_version: str | None = None
    tool_version: str | None = None
    started_at: datetime
    token_usage: TokenUsage = Field(default_factory=TokenUsage.no_calls)
    usage_status: UsageStatus = UsageStatus.NO_CALLS

    @model_validator(mode="after")
    def validate_direct_usage(self) -> "StepDraft":
        validate_step_usage(self)
        return self


class EvidenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    evidence_id: str
    source_step_id: str
    evidence_type: str
    source_ref: str
    payload: dict[str, Any]
    content_hash: bytes
    created_at: datetime


class FixtureDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str
    source_run_id: str
    source_step_id: str
    tool_name: str
    risk_level: str
    canonical_args_hash: bytes
    occurrence_index: int = Field(ge=0)
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    outcome: FixtureOutcome
    error_code: str | None = None
    schema_version: str
    replayable: bool
    blocked_reason: str | None = None
    content_hash: bytes
    created_at: datetime


class TraceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    evidence_id: str
    source_step_id: str
    evidence_type: str
    source_ref: str
    payload: dict[str, Any]
    content_hash: bytes
    created_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_step_id": self.source_step_id,
            "evidence_type": self.evidence_type,
            "source_ref": self.source_ref,
            "payload": self.payload,
            "content_hash": self.content_hash.hex(),
            "created_at": utc_text(self.created_at),
        }


class ToolFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str
    source_run_id: str
    source_step_id: str
    tool_name: str
    risk_level: str
    canonical_args_hash: bytes
    occurrence_index: int = Field(ge=0)
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    outcome: FixtureOutcome
    error_code: str | None = None
    schema_version: str
    replayable: bool
    blocked_reason: str | None = None
    content_hash: bytes
    created_at: datetime


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> bytes:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).digest()


def utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utc_text(value: datetime | None) -> str | None:
    normalized = utc(value)
    if normalized is None:
        return None
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")
