from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from pydantic import ValidationError

from app.tools.errors import ToolError, ToolPermissionError, UnknownToolError
from app.tools.types import ReplayToolDefinition, ToolContext, ToolResult
from app.trace.types import FixtureOutcome, ToolFixture, sha256_json


class ReplayToolRegistry:
    """Fixture-only tool boundary. It contains no executor, network client, or database handle."""

    def __init__(
        self,
        definitions: Iterable[ReplayToolDefinition],
        fixtures: Iterable[ToolFixture],
        *,
        evidence_ids_by_step: dict[str, list[str]] | None = None,
        ensure_active: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._definitions = {item.name: item for item in definitions}
        self._fixtures = {
            (item.tool_name, item.canonical_args_hash, item.occurrence_index): item
            for item in fixtures
        }
        self._evidence_ids_by_step = evidence_ids_by_step or {}
        self._occurrences: dict[tuple[str, bytes], int] = defaultdict(int)
        self.used_fixture_ids: set[str] = set()
        self.ensure_active = ensure_active

    @property
    def unused_fixture_count(self) -> int:
        return len(self._fixtures) - len(self.used_fixture_ids)

    def manifests(self) -> list[dict[str, Any]]:
        return [item.manifest() for item in sorted(self._definitions.values(), key=lambda definition: definition.name)]

    def get(self, name: str) -> ReplayToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise UnknownToolError(name) from exc

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
        del agent_name, context
        definition = self.get(name)
        if self.ensure_active is not None:
            await self.ensure_active()
        if name not in agent_tools or not definition.required_permissions.issubset(permissions):
            raise ToolPermissionError(name)
        try:
            parsed = definition.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolError(
                "INVALID_TOOL_ARGUMENTS",
                "tool arguments failed schema validation",
                details={"errors": exc.errors()},
            ) from exc
        projected = definition.trace_projection.project_request(parsed.model_dump(mode="json"))
        args_hash = sha256_json(projected)
        occurrence_key = (name, args_hash)
        occurrence = self._occurrences[occurrence_key]
        self._occurrences[occurrence_key] += 1
        fixture = self._fixtures.get((name, args_hash, occurrence))
        recorder = _current_trace_recorder()
        step_id = await _trace_started(recorder, definition, projected, occurrence + 1)
        if (
            fixture is None
            or not fixture.replayable
            or fixture.schema_version != definition.trace_projection.schema_version
        ):
            error = ToolError(
                "REPLAY_FIXTURE_MISMATCH",
                "fixed tool fixture does not match this call",
            )
            await _trace_failed(recorder, step_id, definition, projected, error)
            raise error
        self.used_fixture_ids.add(fixture.fixture_id)
        if fixture.outcome is not FixtureOutcome.SUCCESS:
            error = ToolError(
                fixture.error_code or "REPLAY_FIXTURE_MISMATCH",
                "fixed tool fixture reproduced an error",
                retryable=fixture.outcome is FixtureOutcome.TECHNICAL_ERROR,
            )
            await _trace_failed(recorder, step_id, definition, projected, error)
            raise error
        response = definition.trace_projection.project_response(
            fixture.response or {}, for_replay=True
        )
        result = ToolResult(
            value=response,
            evidence_ids=list(self._evidence_ids_by_step.get(fixture.source_step_id, [])),
        )
        await _trace_completed(recorder, step_id, definition, projected, result)
        return result

    def consume_system_fixture(self, tool_name: str) -> ToolFixture | None:
        candidates = sorted(
            (item for item in self._fixtures.values() if item.tool_name == tool_name),
            key=lambda item: item.occurrence_index,
        )
        for item in candidates:
            if item.fixture_id not in self.used_fixture_ids and item.replayable:
                self.used_fixture_ids.add(item.fixture_id)
                return item
        return None


def _current_trace_recorder():
    from app.trace.recorder import current_recorder

    return current_recorder()


async def _trace_started(recorder, definition, arguments, attempt):
    if recorder is None:
        return None
    try:
        return await recorder.tool_started(definition, arguments, attempt)
    except Exception:
        return None


async def _trace_completed(recorder, step_id, definition, arguments, result):
    if recorder is None:
        return
    try:
        await recorder.tool_completed(step_id, definition, arguments, result)
    except Exception:
        return


async def _trace_failed(recorder, step_id, definition, arguments, error):
    if recorder is None:
        return
    try:
        await recorder.tool_failed(step_id, definition, arguments, error)
    except Exception:
        return
