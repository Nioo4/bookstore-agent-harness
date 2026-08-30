from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Iterable

from pydantic import ValidationError

from app.tools.errors import ToolError, ToolPermissionError, UnknownToolError
from app.tools.types import ReplayToolDefinition, RiskLevel, ToolContext, ToolPolicy, ToolResult, ToolSpec
from app.telemetry import tool_span


class ToolRegistry:
    _GUEST_TOOLS = frozenset({"search_books", "get_book", "search_policy"})

    def __init__(self, specs: Iterable[ToolSpec] = (), *, policy: ToolPolicy | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._policy = policy
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise UnknownToolError(name) from exc

    def manifests(self) -> list[dict[str, object]]:
        return [self._specs[name].manifest() for name in sorted(self._specs)]

    @property
    def version(self) -> str:
        canonical = json.dumps(
            self.manifests(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def replay_definitions(self) -> list[ReplayToolDefinition]:
        definitions: list[ReplayToolDefinition] = []
        for name in sorted(self._specs):
            spec = self._specs[name]
            if spec.trace_projection is None:
                raise ValueError(f"business tool {name} has no trace projection")
            definitions.append(
                ReplayToolDefinition(
                    name=spec.name,
                    input_model=spec.input_model,
                    risk=spec.risk,
                    required_permissions=spec.required_permissions,
                    trace_projection=spec.trace_projection,
                )
            )
        return definitions

    async def invoke(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        agent_name: str,
        agent_tools: frozenset[str],
        permissions: frozenset[str],
        context: ToolContext,
    ) -> ToolResult:
        spec = self.get(name)
        recorder = _current_trace_recorder()
        trace_step_id: str | None = None
        if name not in agent_tools or not spec.required_permissions.issubset(permissions):
            raise ToolPermissionError(name)
        if context.guest_mode and (
            context.authenticated_user_id is not None
            or context.access_token is not None
            or name not in self._GUEST_TOOLS
        ):
            raise ToolError("AUTH_REQUIRED", "authentication is required for this capability")
        if spec.risk in {RiskLevel.USER_SCOPED_READ, RiskLevel.PREPARE, RiskLevel.CONFIRM_REQUIRED, RiskLevel.COMMIT}:
            if context.access_token is None or context.authenticated_user_id is None:
                raise ToolError("AUTH_REQUIRED", "authenticated user context is required")
        try:
            parsed = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            error = ToolError("INVALID_TOOL_ARGUMENTS", "tool arguments failed schema validation", details={"errors": exc.errors()})
            trace_step_id = await _trace_started(recorder, spec, arguments, 1)
            await _trace_failed(recorder, trace_step_id, spec, arguments, error)
            raise error from exc

        parsed_arguments = parsed.model_dump(mode="json")

        try:
            if self._policy is not None:
                await self._policy.enforce(name, spec.risk, parsed, context)
            elif spec.risk in {RiskLevel.CONFIRM_REQUIRED, RiskLevel.COMMIT}:
                raise ToolError("POLICY_REQUIRED", "a deterministic policy engine is required for write tools")
        except ToolError as error:
            trace_step_id = await _trace_started(recorder, spec, parsed_arguments, 1)
            await _trace_failed(recorder, trace_step_id, spec, parsed_arguments, error)
            raise

        attempts = 0
        while True:
            started = time.monotonic()
            trace_step_id = await _trace_started(recorder, spec, parsed_arguments, attempts + 1)
            try:
                if context.tool_budget is not None:
                    await context.tool_budget.consume()
                if context.event_sink is not None:
                    await context.event_sink.emit("tool.started", {"tool": name, "attempt": attempts + 1})
                with tool_span(name):
                    result = await asyncio.wait_for(spec.executor(parsed, context), timeout=spec.timeout_seconds)
                if context.event_sink is not None:
                    await context.event_sink.emit(
                        "tool.completed",
                        {
                            "tool": name,
                            "attempt": attempts + 1,
                            "status": "COMPLETED",
                            "duration_ms": round((time.monotonic() - started) * 1000),
                            "evidence_ids": result.evidence_ids,
                        },
                    )
                await _trace_completed(recorder, trace_step_id, spec, parsed_arguments, result)
                return result
            except TimeoutError as exc:
                error = ToolError("TOOL_TIMEOUT", "tool call timed out", retryable=True)
                await _trace_failed(recorder, trace_step_id, spec, parsed_arguments, error)
                if context.event_sink is not None:
                    await context.event_sink.emit("tool.completed", {"tool": name, "status": "FAILED", "code": error.code})
                if attempts >= spec.max_retries or not spec.idempotent:
                    raise error from exc
            except ToolError as error:
                await _trace_failed(recorder, trace_step_id, spec, parsed_arguments, error)
                if context.event_sink is not None:
                    await context.event_sink.emit("tool.completed", {"tool": name, "status": "FAILED", "code": error.code})
                if attempts >= spec.max_retries or not spec.idempotent or not error.retryable:
                    raise
            attempts += 1


def _current_trace_recorder():
    # Local import keeps the general-purpose tool package independent at import time.
    from app.trace.recorder import current_recorder

    return current_recorder()


async def _trace_started(recorder, spec: ToolSpec, arguments: dict[str, object], attempt: int):
    if recorder is None:
        return None
    try:
        return await recorder.tool_started(spec, arguments, attempt)
    except Exception:
        return None


async def _trace_completed(recorder, step_id, spec: ToolSpec, arguments: dict[str, object], result: ToolResult) -> None:
    if recorder is None:
        return
    try:
        await recorder.tool_completed(step_id, spec, arguments, result)
    except Exception:
        return


async def _trace_failed(recorder, step_id, spec: ToolSpec, arguments: dict[str, object], error: ToolError) -> None:
    if recorder is None:
        return
    try:
        await recorder.tool_failed(step_id, spec, arguments, error)
    except Exception:
        return
