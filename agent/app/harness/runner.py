from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict

from app.harness.budget import ToolCallBudget
from app.harness.checkpoint import (
    CheckpointStore,
    NodeCheckpoint,
    NodeStatus,
    RunCheckpoint,
    RunStatus,
)
from app.harness.errors import NodeExecutionError, ToolBudgetExceeded
from app.harness.plan import DagPlanNode, PlanLimits, ValidatedPlan
from app.tools.types import ToolEventSink
from app.tools.errors import ToolError
from app.telemetry import node_span


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**left, **right}


def _max_int(left: int, right: int) -> int:
    return max(left, right)


class HarnessState(TypedDict):
    run_id: str
    user_request: str
    outputs: Annotated[dict[str, Any], _merge_dicts]
    node_attempts: Annotated[dict[str, int], _merge_dicts]
    tool_calls_used: Annotated[int, _max_int]


@dataclass(frozen=True)
class NodeExecutionContext:
    run_id: str
    node_id: str
    tool_budget: ToolCallBudget


NodeHandler = Callable[[DagPlanNode, dict[str, Any], NodeExecutionContext], Awaitable[dict[str, Any]]]
ConfirmationHandler = Callable[[str, ToolCallBudget], Awaitable[dict[str, Any]]]

_CONFIRMATION_GATE = "__confirmation_gate__"


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    outputs: dict[str, Any]
    node_attempts: dict[str, int]
    tool_calls_used: int


class DagRunner:
    def __init__(
        self,
        handlers: dict[str, NodeHandler],
        checkpoint_store: CheckpointStore,
        langgraph_checkpointer: BaseCheckpointSaver[Any],
        limits: PlanLimits | None = None,
        event_sink: ToolEventSink | None = None,
        confirmation_handler: ConfirmationHandler | None = None,
    ) -> None:
        self.handlers = handlers
        self.checkpoint_store = checkpoint_store
        self.langgraph_checkpointer = langgraph_checkpointer
        self.limits = limits or PlanLimits()
        self.event_sink = event_sink
        self.confirmation_handler = confirmation_handler

    async def run(self, validated: ValidatedPlan, *, run_id: str, user_request: str) -> RunResult:
        await self.checkpoint_store.save_run(RunCheckpoint(run_id=run_id, status=RunStatus.RUNNING))
        tool_budget = ToolCallBudget(self.limits.max_tool_calls)
        compiled = self._compile(validated, tool_budget)
        initial: HarnessState = {
            "run_id": run_id,
            "user_request": user_request,
            "outputs": {},
            "node_attempts": {},
            "tool_calls_used": 0,
        }
        try:
            state = await compiled.ainvoke(
                initial,
                config=self._config(run_id),
                durability="sync",
            )
        except Exception as exc:
            await self.checkpoint_store.save_run(
                RunCheckpoint(run_id=run_id, status=RunStatus.FAILED, error=str(exc))
            )
            raise

        waiting = bool(state.get("__interrupt__"))
        status = RunStatus.WAITING_CONFIRMATION if waiting else RunStatus.COMPLETED
        await self.checkpoint_store.save_run(RunCheckpoint(run_id=run_id, status=status))
        return RunResult(
            run_id=run_id,
            status=status,
            outputs=state["outputs"],
            node_attempts=state["node_attempts"],
            tool_calls_used=state.get("tool_calls_used", tool_budget.used),
        )

    async def resume_confirmation(
        self,
        validated: ValidatedPlan,
        *,
        run_id: str,
        decision: str,
    ) -> RunResult:
        if decision not in {"APPROVE", "REJECT"}:
            raise ValueError("invalid confirmation decision")
        compiled = self._compile(
            validated, ToolCallBudget(self.limits.max_tool_calls), confirmation_decision=decision
        )
        config = self._config(run_id)
        snapshot = await compiled.aget_state(config)
        stored_result = self._stored_confirmation_result(snapshot.values)
        # A failed interrupt continuation can retain RESUME writes: LangGraph's
        # snapshot.next is then empty even though the gate task has an error.
        failed_gate = any(task.name == _CONFIRMATION_GATE and task.error is not None for task in snapshot.tasks)
        if not snapshot.next and not failed_gate:
            if stored_result is None:
                raise ToolError("CONFIRMATION_NOT_FOUND", "confirmation continuation not found")
            if stored_result.get("decision") != decision:
                raise ToolError("CONFIRMATION_ALREADY_DECIDED", "confirmation already decided")
            replay = {**stored_result, "replayed": True}
            status = RunStatus.COMPLETED if decision == "APPROVE" else RunStatus.CANCELLED
            return self._result_from_state(run_id, status, snapshot.values, replay)
        if _CONFIRMATION_GATE not in snapshot.next and not failed_gate:
            raise ToolError("CONFIRMATION_NOT_FOUND", "confirmation continuation not found")

        try:
            state = await compiled.ainvoke(
                Command(resume={"decision": decision}),
                config=config,
                durability="sync",
            )
        except Exception as exc:
            if not isinstance(exc, ToolError):
                await self.checkpoint_store.save_run(
                    RunCheckpoint(run_id=run_id, status=RunStatus.FAILED, error=str(exc))
                )
            raise
        if state.get("__interrupt__"):
            raise ToolError("CONFIRMATION_NOT_FOUND", "confirmation continuation did not resume")
        value = self._stored_confirmation_result(state)
        if value is None:
            raise ToolError("DEPENDENCY_UNAVAILABLE", "confirmation continuation returned no result", retryable=True)
        status = RunStatus.COMPLETED if decision == "APPROVE" else RunStatus.CANCELLED
        await self.checkpoint_store.save_run(RunCheckpoint(run_id=run_id, status=status))
        return self._result_from_state(run_id, status, state, value)

    def _compile(
        self, validated: ValidatedPlan, tool_budget: ToolCallBudget,
        *, confirmation_decision: str | None = None,
    ):
        semaphore = asyncio.Semaphore(self.limits.max_parallel)
        graph = StateGraph(HarnessState)
        for node in validated.plan.nodes:
            graph.add_node(node.id, self._node_callable(node, semaphore, tool_budget))
        graph.add_node(_CONFIRMATION_GATE, self._confirmation_gate_callable(confirmation_decision))
        for node in validated.plan.nodes:
            if node.depends_on:
                graph.add_edge(node.depends_on, node.id)
            else:
                graph.add_edge(START, node.id)
        depended_on = {dependency for node in validated.plan.nodes for dependency in node.depends_on}
        terminal_nodes = [node.id for node in validated.plan.nodes if node.id not in depended_on]
        graph.add_edge(terminal_nodes, _CONFIRMATION_GATE)
        graph.add_edge(_CONFIRMATION_GATE, END)
        return graph.compile(checkpointer=self.langgraph_checkpointer)

    def _confirmation_gate_callable(self, confirmation_decision: str | None = None):
        async def gate(state: HarnessState) -> dict[str, Any]:
            confirmations = self._confirmation_count(state["outputs"])
            if confirmations == 0:
                return {"outputs": {}, "tool_calls_used": state.get("tool_calls_used", 0)}
            if confirmations > 1:
                raise ToolError("AGENT_PLAN_INVALID", "a run may produce only one confirmation action")
            resumed = interrupt(
                {
                    "kind": "ORDER_CONFIRMATION",
                    "run_id": state["run_id"],
                    "action_type": "CREATE_ORDER",
                    "risk": "COMMIT",
                }
            )
            if not isinstance(resumed, dict) or resumed.get("decision") not in {"APPROVE", "REJECT"}:
                raise ToolError("CONFIRMATION_NOT_FOUND", "invalid confirmation continuation input")
            if self.confirmation_handler is None:
                raise ToolError("DEPENDENCY_UNAVAILABLE", "confirmation continuation handler is unavailable", retryable=True)
            budget = ToolCallBudget(
                self.limits.max_tool_calls,
                used=state.get("tool_calls_used", 0),
            )
            # Cached resume data may belong to a previous failed HTTP request.
            # The authenticated current request supplies the decision; the Order
            # transaction remains authoritative for terminal conflicts/replays.
            result = await self.confirmation_handler(confirmation_decision or str(resumed["decision"]), budget)
            return {
                "outputs": {"confirmation_result": result},
                "tool_calls_used": budget.used,
            }

        return gate

    def _config(self, run_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": run_id},
            "max_concurrency": self.limits.max_parallel,
            "recursion_limit": self.limits.max_nodes + 3,
        }

    @staticmethod
    def _confirmation_count(value: Any) -> int:
        if isinstance(value, dict):
            return int(value.get("confirmation_required") is True) + sum(
                DagRunner._confirmation_count(item)
                for key, item in value.items()
                if key != "confirmation_required"
            )
        if isinstance(value, list):
            return sum(DagRunner._confirmation_count(item) for item in value)
        return 0

    @staticmethod
    def _stored_confirmation_result(state: Any) -> dict[str, Any] | None:
        if not isinstance(state, dict):
            return None
        outputs = state.get("outputs")
        if not isinstance(outputs, dict):
            return None
        value = outputs.get("confirmation_result")
        return dict(value) if isinstance(value, dict) else None

    @staticmethod
    def _result_from_state(
        run_id: str,
        status: RunStatus,
        state: Any,
        confirmation_result: dict[str, Any],
    ) -> RunResult:
        values = state if isinstance(state, dict) else {}
        outputs = dict(values.get("outputs", {}))
        outputs["confirmation_result"] = confirmation_result
        return RunResult(
            run_id=run_id,
            status=status,
            outputs=outputs,
            node_attempts=dict(values.get("node_attempts", {})),
            tool_calls_used=int(values.get("tool_calls_used", 0)),
        )

    def _node_callable(
        self,
        node: DagPlanNode,
        semaphore: asyncio.Semaphore,
        tool_budget: ToolCallBudget,
    ) -> Callable[[HarnessState], Awaitable[dict[str, dict[str, Any]]]]:
        async def execute(state: HarnessState) -> dict[str, dict[str, Any]]:
            handler = self.handlers.get(node.agent)
            if handler is None:
                raise NodeExecutionError(node.id, RuntimeError(f"no handler registered for agent {node.agent}"))
            inputs = {
                reference: state["user_request"] if reference == "user_request" else state["outputs"][reference]
                for reference in node.input_refs
            }
            last_error: Exception | None = None
            for attempt in (1, 2):
                trace_step_id = await _trace_agent_started(node, inputs, attempt)
                if self.event_sink is not None:
                    await self.event_sink.emit(
                        "agent.started",
                        {"node_id": node.id, "agent": node.agent, "attempt": attempt},
                    )
                await self.checkpoint_store.save_node(
                    NodeCheckpoint(run_id=state["run_id"], node_id=node.id, status=NodeStatus.RUNNING, attempt=attempt)
                )
                try:
                    async with semaphore:
                        with node_span(node.agent):
                            output = await handler(
                                node,
                                inputs,
                                NodeExecutionContext(
                                    run_id=state["run_id"], node_id=node.id, tool_budget=tool_budget
                                ),
                            )
                    await self.checkpoint_store.save_node(
                        NodeCheckpoint(
                            run_id=state["run_id"],
                            node_id=node.id,
                            status=NodeStatus.COMPLETED,
                            attempt=attempt,
                            output=output,
                        )
                    )
                    if self.event_sink is not None:
                        await self.event_sink.emit(
                            "agent.completed",
                            {"node_id": node.id, "agent": node.agent, "attempt": attempt, "status": "COMPLETED"},
                        )
                    await _trace_agent_completed(trace_step_id, output)
                    return {
                        "outputs": {node.expected_output: output},
                        "node_attempts": {node.id: attempt},
                        "tool_calls_used": tool_budget.used,
                    }
                except Exception as exc:
                    last_error = exc
                    await _trace_agent_failed(trace_step_id, exc)
                    await self.checkpoint_store.save_node(
                        NodeCheckpoint(
                            run_id=state["run_id"],
                            node_id=node.id,
                            status=NodeStatus.FAILED,
                            attempt=attempt,
                            error=str(exc),
                        )
                    )
                    if self.event_sink is not None:
                        await self.event_sink.emit(
                            "agent.completed",
                            {"node_id": node.id, "agent": node.agent, "attempt": attempt, "status": "FAILED"},
                        )
                    if isinstance(exc, ToolBudgetExceeded) or (
                        isinstance(exc, ToolError) and not exc.retryable
                    ):
                        break
            assert last_error is not None
            raise NodeExecutionError(node.id, last_error)

        return execute


async def _trace_agent_started(node: DagPlanNode, inputs: dict[str, Any], attempt: int):
    from app.trace.recorder import current_recorder
    from app.trace.types import StepType

    recorder = current_recorder()
    if recorder is None:
        return None
    return await recorder.start_step(
        node_id=node.id,
        step_type=StepType.AGENT,
        input_value={
            "objective": node.objective,
            "inputs": {
                key: (
                    {
                        "content_hash": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                        "content_bytes": len(value.encode("utf-8")),
                    }
                    if key == "user_request" and isinstance(value, str)
                    else value
                )
                for key, value in inputs.items()
            },
        },
        agent_name=node.agent,
        attempt_no=attempt,
        prompt_version=recorder.run.prompt_set_version,
    )


async def _trace_agent_completed(step_id, output: dict[str, Any]) -> None:
    from app.trace.recorder import current_recorder
    from app.trace.types import StepStatus

    recorder = current_recorder()
    if recorder is not None:
        await recorder.finish_step(step_id, status=StepStatus.COMPLETED, output_value=output)


async def _trace_agent_failed(step_id, exc: Exception) -> None:
    from app.trace.recorder import current_recorder
    from app.trace.types import StepStatus

    recorder = current_recorder()
    if recorder is not None:
        await recorder.finish_step(
            step_id,
            status=StepStatus.FAILED,
            output_value=None,
            error_code="AGENT_NODE_FAILED",
            error_message=str(exc),
        )
