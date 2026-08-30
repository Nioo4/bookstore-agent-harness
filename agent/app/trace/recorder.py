from __future__ import annotations

import asyncio
import contextvars
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.evidence import evidence_payload
from app.models.provider import ModelProvider, ModelRequest, ModelResponse
from app.tools.errors import ToolError
from app.tools.types import ReplayToolDefinition, RiskLevel, ToolResult, ToolSpec
from app.trace.errors import TraceDependencyUnavailable
from app.trace.manifests import VersionRegistry
from app.trace.pricing import PricingCalculator
from app.trace.usage import aggregate_usage, aggregate_complete_cost, complete_cost, persisted_steps
from app.trace.repository import TraceRepository
from app.trace.safety import TraceSafetyProjector
from app.trace.types import (
    CostStatus,
    EvidenceDraft,
    FixtureDraft,
    FixtureOutcome,
    RunKind,
    RunStatus,
    StepDraft,
    StepStatus,
    StepType,
    TokenUsage,
    UsageStatus,
    TraceCost,
    TraceRun,
    TraceStatus,
    WorkflowType,
    sha256_json,
)


_current_recorder: contextvars.ContextVar["TraceRecorder | None"] = contextvars.ContextVar(
    "trace_recorder", default=None
)
_current_parent_step: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_parent_step", default=None
)


class TraceRecorder:
    def __init__(
        self,
        repository: TraceRepository,
        safety: TraceSafetyProjector,
        versions: VersionRegistry,
        pricing: PricingCalculator,
        *,
        run: TraceRun,
        max_run_bytes: int = 2_097_152,
    ) -> None:
        self.repository = repository
        self.safety = safety
        self.versions = versions
        self.pricing = pricing
        self.run = run
        self.max_run_bytes = max_run_bytes
        self.degraded = run.trace_status is TraceStatus.INCOMPLETE
        self._observed_known = False
        self._fixture_occurrences: dict[tuple[str, bytes], int] = {}
        self._memory_ids: set[str] = set()
        self._memory_values: set[str] = set()
        self._explicit_request_memory_values: set[str] = set()

    def register_memory_context(
        self, memory_context: list[dict[str, Any]], *, request_text: str | None = None
    ) -> None:
        for item in memory_context:
            self._collect_memory_strings(item.get("memory_id"), self._memory_ids)
            self._collect_memory_strings(item.get("value"), self._memory_values)
        if request_text:
            self._explicit_request_memory_values.update(
                value for value in self._memory_values if value in request_text
            )

    @classmethod
    async def start_live(
        cls,
        repository: TraceRepository,
        safety: TraceSafetyProjector,
        versions: VersionRegistry,
        pricing: PricingCalculator,
        *,
        run_id: str,
        user_id: int,
        conversation_id: str,
        user_request: str | None,
        input_value: dict[str, Any] | None = None,
        workflow_type: WorkflowType = WorkflowType.CHAT,
        model_profile: str,
        prompt_set_version: str,
        tool_registry_version: str,
        harness_version: str,
        retention_days: int,
        retention_expires_at: datetime | None = None,
        now: datetime | None = None,
        max_run_bytes: int = 2_097_152,
    ) -> "TraceRecorder":
        timestamp = now or datetime.now(UTC)
        if (user_request is None) == (input_value is None):
            raise ValueError("exactly one trace input representation is required")
        projected = safety.json(
            input_value if input_value is not None else {"content": user_request}
        )
        run = TraceRun(
            run_id=run_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_kind=RunKind.LIVE,
            workflow_type=workflow_type,
            status=RunStatus.RUNNING,
            trace_status=TraceStatus.RECORDING,
            replayable=False,
            replay_block_reason=("INPUT_REDACTED_OR_TRUNCATED" if not projected.replay_safe else None),
            input=projected.value,
            model_profile=model_profile,
            model_manifest_hash=versions.model_profile_hash(model_profile),
            prompt_set_version=prompt_set_version,
            prompt_manifest_hash=versions.prompt_set_hash(prompt_set_version),
            tool_registry_version=tool_registry_version,
            harness_version=harness_version,
            cost=TraceCost(status=CostStatus.UNAVAILABLE),
            retention_expires_at=retention_expires_at or timestamp + timedelta(days=retention_days),
            started_at=timestamp,
            created_at=timestamp,
            updated_at=timestamp,
        )
        try:
            await repository.create_run(run)
        except Exception as exc:
            raise TraceDependencyUnavailable("initial trace run create failed") from exc
        return cls(
            repository,
            safety,
            versions,
            pricing,
            run=run,
            max_run_bytes=max_run_bytes,
        )

    def bind(self) -> contextvars.Token["TraceRecorder | None"]:
        return _current_recorder.set(self)

    @staticmethod
    def reset(token: contextvars.Token["TraceRecorder | None"]) -> None:
        _current_recorder.reset(token)

    async def start_step(
        self,
        *,
        node_id: str,
        step_type: StepType,
        input_value: dict[str, Any] | None,
        agent_name: str | None = None,
        tool_name: str | None = None,
        attempt_no: int = 1,
        model_profile: str | None = None,
        prompt_version: str | None = None,
        tool_version: str | None = None,
        parent_step_id: str | None = None,
        now: datetime | None = None,
        _model_call: bool = False,
    ) -> str | None:
        if node_id.startswith("model:") != _model_call:
            raise ValueError("model: prefix is reserved for the model wrapper")
        if self.degraded:
            return None
        projected = (
            self.safety.json(self._without_memory(input_value))
            if input_value is not None
            else None
        )
        try:
            step = await self.repository.append_step(
                StepDraft(
                    step_id=str(uuid.uuid4()),
                    run_id=self.run.run_id,
                    parent_step_id=parent_step_id or _current_parent_step.get(),
                    node_id=node_id,
                    step_type=step_type,
                    agent_name=agent_name,
                    tool_name=tool_name,
                    attempt_no=attempt_no,
                    input=projected.value if projected is not None else None,
                    model_profile=model_profile,
                    prompt_version=prompt_version,
                    tool_version=tool_version,
                    started_at=now or datetime.now(UTC),
                    token_usage=TokenUsage() if _model_call else TokenUsage.no_calls(),
                    usage_status=UsageStatus.UNAVAILABLE if _model_call else UsageStatus.NO_CALLS,
                )
            )
            await self._check_size()
            return step.step_id
        except Exception:
            await self._degrade("STEP_WRITE_FAILED")
            return None

    async def finish_step(
        self,
        step_id: str | None,
        *,
        status: StepStatus,
        output_value: dict[str, Any] | None,
        evidence_ids: list[str] | None = None,
        usage: TokenUsage | None = None,
        usage_status: UsageStatus = UsageStatus.NO_CALLS,
        cost: TraceCost | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if self.degraded or step_id is None:
            return
        output = (
            self.safety.json(self._without_memory(output_value))
            if output_value is not None
            else None
        )
        safe_error = self.safety.text(error_message, kind="error").value if error_message else None
        try:
            await self.repository.finish_step(
                self.run.run_id,
                step_id,
                status=status,
                output=output.value if output is not None else None,
                evidence_ids=evidence_ids or [],
                token_usage=usage if usage is not None else TokenUsage.no_calls(),
                usage_status=usage_status,
                cost=cost or TraceCost(status=CostStatus.UNAVAILABLE),
                error_code=error_code,
                error_message=str(safe_error) if safe_error is not None else None,
                completed_at=now or datetime.now(UTC),
            )
            await self._check_size()
        except Exception:
            await self._degrade("STEP_WRITE_FAILED")

    async def model_started(self, request: ModelRequest) -> str | None:
        # System/developer prompt bodies are deliberately represented only by versions and roles.
        safe_messages = [
            {
                "role": message.role,
                "name": message.name,
                **(
                    {
                        "content_hash": sha256_json({"content": message.content}).hex(),
                        "content_bytes": len(message.content.encode("utf-8")),
                    }
                    if message.role in {"user", "tool"}
                    else {"content": "[VERSIONED_PROMPT]"}
                ),
            }
            for message in request.messages
        ]
        return await self.start_step(
            node_id=f"model:{request.model}",
            _model_call=True,
            step_type=StepType.SYSTEM,
            input_value={
                "model": request.model,
                "messages": safe_messages,
                "response_schema_hash": sha256_json(request.response_schema).hex()
                if request.response_schema is not None
                else None,
            },
            model_profile=self.run.model_profile,
            prompt_version=self.run.prompt_set_version,
        )

    async def model_completed(self, step_id: str | None, response: ModelResponse) -> None:
        usage = TokenUsage(
            prompt=response.usage.prompt_tokens,
            completion=response.usage.completion_tokens,
            cached=response.usage.cached_tokens,
            reasoning=response.usage.reasoning_tokens,
        )
        cost = complete_cost(self.pricing.price(response.model, response.usage))
        self._observed_known |= usage.prompt is not None or usage.completion is not None
        projected = self.safety.text(response.content).value
        await self.finish_step(
            step_id,
            status=StepStatus.COMPLETED,
            output_value={
                "content": projected,
                "model": response.model,
                "finish_reason": response.finish_reason,
            },
            usage=usage,
            usage_status=usage.call_status(),
            cost=cost,
        )

    async def model_failed(self, step_id: str | None) -> None:
        await self.finish_step(
            step_id,
            status=StepStatus.FAILED,
            output_value=None,
            usage=TokenUsage(),
            usage_status=UsageStatus.UNAVAILABLE,
            error_code="MODEL_DEPENDENCY_UNAVAILABLE",
            error_message="model call failed",
        )

    async def tool_started(self, spec: ToolSpec | ReplayToolDefinition, request: dict[str, Any], attempt: int) -> str | None:
        projection = spec.trace_projection
        safe_request = projection.project_request(request) if projection is not None else {}
        return await self.start_step(
            node_id=f"tool:{spec.name}",
            step_type=StepType.TOOL,
            input_value=safe_request,
            tool_name=spec.name,
            attempt_no=attempt,
            tool_version=projection.schema_version if projection is not None else None,
        )

    async def tool_completed(
        self,
        step_id: str | None,
        spec: ToolSpec | ReplayToolDefinition,
        request: dict[str, Any],
        result: ToolResult,
    ) -> None:
        projection = spec.trace_projection
        if projection is None:
            await self._degrade("TOOL_PROJECTION_MISSING")
            return
        projected_request = self.safety.json(projection.project_request(request))
        projected_response = self.safety.json(projection.project_response(result.value))
        args_hash = sha256_json(projected_request.value)
        occurrence_key = (spec.name, args_hash)
        occurrence = self._fixture_occurrences.get(occurrence_key, 0)
        self._fixture_occurrences[occurrence_key] = occurrence + 1
        replayable = (
            projected_request.replay_safe
            and projected_response.replay_safe
            and projection.schema_version != ""
        )
        blocked_reason = None if replayable else "FIXTURE_REDACTED_OR_TRUNCATED"
        timestamp = datetime.now(UTC)
        try:
            if step_id is not None:
                for evidence_id in result.evidence_ids:
                    evidence_projection = self.safety.evidence(evidence_payload(spec.name, result, evidence_id))
                    await self.repository.insert_evidence(
                        EvidenceDraft(
                            run_id=self.run.run_id,
                            evidence_id=evidence_id,
                            source_step_id=step_id,
                            evidence_type=_evidence_type(evidence_id),
                            source_ref=f"{spec.name}:{evidence_id}",
                            payload=evidence_projection.value,
                            content_hash=bytes.fromhex(evidence_projection.content_hash),
                            created_at=timestamp,
                        )
                    )
                if self.run.run_kind is RunKind.LIVE:
                    await self.repository.insert_fixture(
                        FixtureDraft(
                        fixture_id=str(uuid.uuid4()),
                        source_run_id=self.run.run_id,
                        source_step_id=step_id,
                        tool_name=spec.name,
                        risk_level=_trace_risk(spec.risk),
                        canonical_args_hash=args_hash,
                        occurrence_index=occurrence,
                        request=projected_request.value,
                        response=projected_response.value,
                        outcome=FixtureOutcome.SUCCESS,
                        schema_version=projection.schema_version,
                        replayable=replayable,
                        blocked_reason=blocked_reason,
                        content_hash=sha256_json(
                            {"request": projected_request.value, "response": projected_response.value}
                        ),
                        created_at=timestamp,
                        )
                    )
            await self.finish_step(
                step_id,
                status=StepStatus.COMPLETED,
                output_value=result.value,
                evidence_ids=result.evidence_ids,
            )
            if not replayable:
                await self.repository.mark_unreplayable(
                    self.run.run_id, blocked_reason or "FIXTURE_UNSAFE"
                )
                self.run = self.run.model_copy(
                    update={"replayable": False, "replay_block_reason": blocked_reason}
                )
        except Exception:
            await self._degrade("FIXTURE_WRITE_FAILED")

    async def tool_failed(
        self,
        step_id: str | None,
        spec: ToolSpec | ReplayToolDefinition,
        request: dict[str, Any],
        error: ToolError,
    ) -> None:
        projection = spec.trace_projection
        if projection is None:
            await self._degrade("TOOL_PROJECTION_MISSING")
            return
        projected_request = self.safety.json(projection.project_request(request))
        replayable = projected_request.replay_safe and projection.schema_version != ""
        blocked_reason = None if replayable else "FIXTURE_REDACTED_OR_TRUNCATED"
        args_hash = sha256_json(projected_request.value)
        occurrence_key = (spec.name, args_hash)
        occurrence = self._fixture_occurrences.get(occurrence_key, 0)
        self._fixture_occurrences[occurrence_key] = occurrence + 1
        try:
            if step_id is not None:
                if self.run.run_kind is RunKind.LIVE:
                    await self.repository.insert_fixture(
                        FixtureDraft(
                        fixture_id=str(uuid.uuid4()),
                        source_run_id=self.run.run_id,
                        source_step_id=step_id,
                        tool_name=spec.name,
                        risk_level=_trace_risk(spec.risk),
                        canonical_args_hash=args_hash,
                        occurrence_index=occurrence,
                        request=projected_request.value,
                        response=None,
                        outcome=(
                            FixtureOutcome.TECHNICAL_ERROR
                            if error.retryable
                            else FixtureOutcome.BUSINESS_ERROR
                        ),
                        error_code=error.code,
                        schema_version=projection.schema_version,
                        replayable=replayable,
                        blocked_reason=blocked_reason,
                        content_hash=sha256_json(
                            {"request": projected_request.value, "error_code": error.code}
                        ),
                        created_at=datetime.now(UTC),
                        )
                    )
            await self.finish_step(
                step_id,
                status=StepStatus.FAILED,
                output_value=None,
                error_code=error.code,
                error_message="tool call failed",
            )
            if not replayable:
                await self.repository.mark_unreplayable(
                    self.run.run_id, blocked_reason or "FIXTURE_UNSAFE"
                )
                self.run = self.run.model_copy(
                    update={"replayable": False, "replay_block_reason": blocked_reason}
                )
        except Exception:
            await self._degrade("FIXTURE_WRITE_FAILED")

    async def record_confirmation_decision(self, decision: str, status: str) -> None:
        step_id = await self.start_step(
            node_id="confirmation.decision",
            step_type=StepType.SYSTEM,
            input_value={"decision": decision, "action_type": "CREATE_ORDER", "risk": "COMMIT"},
            agent_name="order",
            tool_version="confirmation-decision-v1",
        )
        if step_id is None:
            return
        request = {"decision": decision, "action_type": "CREATE_ORDER", "risk": "COMMIT"}
        response = {"status": status}
        try:
            if self.run.run_kind is RunKind.LIVE:
                await self.repository.insert_fixture(
                    FixtureDraft(
                        fixture_id=str(uuid.uuid4()),
                        source_run_id=self.run.run_id,
                        source_step_id=step_id,
                        tool_name="confirmation_decision",
                        risk_level="COMMIT",
                        canonical_args_hash=sha256_json(request),
                        occurrence_index=0,
                        request=request,
                        response=response,
                        outcome=FixtureOutcome.SUCCESS,
                        schema_version="confirmation-decision-v1",
                        replayable=True,
                        content_hash=sha256_json({"request": request, "response": response}),
                        created_at=datetime.now(UTC),
                    )
                )
            await self.finish_step(
                step_id,
                status=StepStatus.COMPLETED,
                output_value=response,
            )
        except Exception:
            await self._degrade("CONFIRMATION_TRACE_WRITE_FAILED")

    async def finish_run(
        self,
        *,
        status: RunStatus,
        final_answer: str | None,
        dag: dict[str, Any] | None,
        error_code: str | None = None,
        error_message: str | None = None,
        replayable: bool = True,
        replay_block_reason: str | None = None,
        unused_fixture_count: int | None = None,
    ) -> TraceRun:
        answer_value = self._without_memory(final_answer) if final_answer is not None else None
        answer = self.safety.text(str(answer_value)).value if answer_value is not None else None
        safe_error = self.safety.text(error_message, kind="error").value if error_message else None
        usage, usage_status, cost = await self.usage_and_cost()
        prior_block_reason = (
            None if self.run.replay_block_reason == "RUN_NOT_TERMINAL" else self.run.replay_block_reason
        )
        effective_replayable = (
            replayable
            and not self.degraded
            and prior_block_reason is None
            and replay_block_reason is None
            and status in {RunStatus.COMPLETED, RunStatus.CANCELLED}
        )
        trace_status = TraceStatus.INCOMPLETE if self.degraded else TraceStatus.COMPLETE
        try:
            completed = await self.repository.finish_run(
                self.run.run_id,
                status=status,
                trace_status=trace_status,
                replayable=effective_replayable,
                replay_block_reason=(
                    prior_block_reason
                    or replay_block_reason
                    or ("TRACE_INCOMPLETE" if self.degraded else None)
                    or ("RUN_FAILED" if status is RunStatus.FAILED else None)
                ),
                final_answer=str(answer) if answer is not None else None,
                dag=self.safety.json(dag).value if dag is not None else None,
                token_usage=usage,
                usage_status=usage_status,
                cost=cost,
                unused_fixture_count=unused_fixture_count,
                error_code=error_code,
                error_message=str(safe_error) if safe_error is not None else None,
                completed_at=datetime.now(UTC),
            )
            self.run = completed
            return completed
        except Exception:
            self.degraded = True
            return self.run.model_copy(
                update={
                    "status": status,
                    "trace_status": TraceStatus.INCOMPLETE,
                    "replayable": False,
                    "replay_block_reason": "FINAL_TRACE_WRITE_FAILED",
                }
            )

    async def pause_for_confirmation(
        self, *, final_answer: str, dag: dict[str, Any]
    ) -> TraceRun:
        answer = self.safety.text(final_answer).value
        usage, usage_status, cost = await self.usage_and_cost()
        try:
            paused = await self.repository.pause_run(
                self.run.run_id,
                status=RunStatus.WAITING_CONFIRMATION,
                trace_status=TraceStatus.INCOMPLETE if self.degraded else TraceStatus.COMPLETE,
                replay_block_reason="RUN_NOT_TERMINAL",
                final_answer=str(answer),
                dag=self.safety.json(dag).value,
                token_usage=usage,
                usage_status=usage_status,
                cost=cost,
                updated_at=datetime.now(UTC),
            )
            self.run = paused
            return paused
        except Exception:
            await self._degrade("TRACE_PAUSE_FAILED")
            return self.run.model_copy(
                update={
                    "status": RunStatus.WAITING_CONFIRMATION,
                    "trace_status": TraceStatus.INCOMPLETE,
                    "replayable": False,
                }
            )

    async def pause_for_event(self, *, dag: dict[str, Any]) -> TraceRun:
        usage, usage_status, cost = await self.usage_and_cost()
        try:
            paused = await self.repository.pause_run(
                self.run.run_id,
                status=RunStatus.WAITING_EVENT,
                trace_status=TraceStatus.INCOMPLETE if self.degraded else TraceStatus.COMPLETE,
                replay_block_reason="RUN_NOT_TERMINAL",
                final_answer=None,
                dag=self.safety.json(dag).value,
                token_usage=usage,
                usage_status=usage_status,
                cost=cost,
                updated_at=datetime.now(UTC),
            )
            self.run = paused
            return paused
        except Exception:
            await self._degrade("TRACE_PAUSE_FAILED")
            return self.run.model_copy(
                update={
                    "status": RunStatus.WAITING_EVENT,
                    "trace_status": TraceStatus.INCOMPLETE,
                    "replayable": False,
                }
            )

    async def usage_and_cost(self) -> tuple[TokenUsage, UsageStatus, TraceCost]:
        if self.run.usage_status is UsageStatus.LEGACY_UNKNOWN:
            return self.run.token_usage, self.run.usage_status, self.run.cost
        try:
            steps = await persisted_steps(self.repository, self.run.run_id)
        except Exception:
            await self._degrade("USAGE_READ_FAILED")
            steps = []
        usage, status = aggregate_usage(steps, coverage_complete=not self.degraded,
                                        observed_known=self._observed_known)
        cost = aggregate_complete_cost(steps, self.pricing.manifest.pricing_version,
                                       coverage_complete=not self.degraded)
        return usage, status, cost

    async def _check_size(self) -> None:
        if await self.repository.trace_bytes(self.run.run_id) > self.max_run_bytes:
            await self._degrade("TRACE_SIZE_LIMIT")

    async def _degrade(self, reason: str) -> None:
        if self.degraded:
            return
        self.degraded = True
        self.run = self.run.model_copy(
            update={
                "trace_status": TraceStatus.INCOMPLETE,
                "replayable": False,
                "replay_block_reason": reason[:64],
            }
        )
        try:
            await self.repository.mark_incomplete(self.run.run_id, reason)
        except Exception:
            pass

    def _collect_memory_strings(self, value: Any, target: set[str]) -> None:
        if isinstance(value, str):
            if value:
                target.add(value)
            return
        if isinstance(value, dict):
            for item in value.values():
                self._collect_memory_strings(item, target)
            return
        if isinstance(value, list):
            for item in value:
                self._collect_memory_strings(item, target)

    def _without_memory(self, value: Any) -> Any:
        if isinstance(value, str):
            safe = value
            protected_values = self._memory_ids | (
                self._memory_values - self._explicit_request_memory_values
            )
            for memory_value in sorted(protected_values, key=len, reverse=True):
                safe = safe.replace(memory_value, "[REDACTED_MEMORY]")
            return safe
        if isinstance(value, dict):
            return {key: self._without_memory(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._without_memory(item) for item in value]
        return value


class TracingModelProvider(ModelProvider):
    def __init__(self, inner: ModelProvider) -> None:
        self.inner = inner

    async def complete(self, request: ModelRequest) -> ModelResponse:
        recorder = _current_recorder.get()
        step_id = await recorder.model_started(request) if recorder is not None else None
        try:
            response = await self.inner.complete(request)
        except (Exception, asyncio.CancelledError):
            if recorder is not None:
                await recorder.model_failed(step_id)
            raise
        if recorder is not None:
            await recorder.model_completed(step_id, response)
        return response


class TraceRecorderFactory:
    def __init__(
        self,
        repository: TraceRepository,
        safety: TraceSafetyProjector,
        versions: VersionRegistry,
        pricing: PricingCalculator,
        *,
        model_profile: str,
        prompt_set_version: str,
        tool_registry_version: str,
        harness_version: str,
        retention_days: int,
        max_run_bytes: int,
    ) -> None:
        self.repository = repository
        self.safety = safety
        self.versions = versions
        self.pricing = pricing
        self.model_profile = model_profile
        self.prompt_set_version = prompt_set_version
        self.tool_registry_version = tool_registry_version
        self.harness_version = harness_version
        self.retention_days = retention_days
        self.max_run_bytes = max_run_bytes

    async def start(
        self, *, user_id: int, conversation_id: str, user_request: str
    ) -> TraceRecorder:
        return await TraceRecorder.start_live(
            self.repository,
            self.safety,
            self.versions,
            self.pricing,
            run_id=str(uuid.uuid4()),
            user_id=user_id,
            conversation_id=conversation_id,
            user_request=user_request,
            model_profile=self.model_profile,
            prompt_set_version=self.prompt_set_version,
            tool_registry_version=self.tool_registry_version,
            harness_version=self.harness_version,
            retention_days=self.retention_days,
            max_run_bytes=self.max_run_bytes,
        )

    async def start_task(
        self,
        *,
        run_id: str,
        user_id: int,
        conversation_id: str,
        input_value: dict[str, Any],
        retention_expires_at: datetime | None = None,
    ) -> TraceRecorder:
        return await TraceRecorder.start_live(
            self.repository,
            self.safety,
            self.versions,
            self.pricing,
            run_id=run_id,
            user_id=user_id,
            conversation_id=conversation_id,
            user_request=None,
            input_value=input_value,
            workflow_type=WorkflowType.INVENTORY_WATCH,
            model_profile=self.model_profile,
            prompt_set_version=self.prompt_set_version,
            tool_registry_version=self.tool_registry_version,
            harness_version=self.harness_version,
            retention_days=self.retention_days,
            retention_expires_at=retention_expires_at,
            max_run_bytes=self.max_run_bytes,
        )

    async def attach(self, run_id: str) -> TraceRecorder:
        run = await self.repository.get_worker_run(run_id)
        recorder = TraceRecorder(
            self.repository,
            self.safety,
            self.versions,
            self.pricing,
            run=run,
            max_run_bytes=self.max_run_bytes,
        )
        if run.status is RunStatus.RUNNING and run.workflow_type is not WorkflowType.INVENTORY_WATCH:
            await recorder._degrade("RECOVERY_MODEL_COVERAGE_UNKNOWN")
        return recorder


def current_recorder() -> TraceRecorder | None:
    return _current_recorder.get()


def _trace_risk(risk: RiskLevel) -> str:
    if risk in {RiskLevel.READ_ONLY, RiskLevel.USER_SCOPED_READ}:
        return "READ"
    if risk is RiskLevel.PREPARE:
        return "PREPARE"
    return "COMMIT"


def _evidence_type(evidence_id: str) -> str:
    prefix = evidence_id.partition(":")[0].casefold()
    return {"book": "BOOK", "policy": "POLICY", "order": "ORDER"}.get(prefix, "TOOL_RESULT")
