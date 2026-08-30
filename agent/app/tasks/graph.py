from __future__ import annotations

from typing import Any, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.tasks.types import AgentTask


class InventoryWatchState(TypedDict):
    task_id: str
    run_id: str
    book_id: int
    desired_quantity: int
    status: str
    terminal_event: dict[str, Any] | None


class InventoryWatchRuntime(Protocol):
    async def ensure_waiting(self, task: AgentTask) -> None: ...
    async def finish(self, task: AgentTask, event: dict[str, Any]) -> None: ...


class InventoryWatchGraph:
    version = "inventory_watch_v1"

    def __init__(self, checkpointer: BaseCheckpointSaver[Any]) -> None:
        graph = StateGraph(InventoryWatchState)
        graph.add_node("wait_for_inventory", self._wait)
        graph.add_edge(START, "wait_for_inventory")
        graph.add_edge("wait_for_inventory", END)
        self._compiled = graph.compile(checkpointer=checkpointer)

    async def ensure_waiting(self, task: AgentTask) -> None:
        config = self._config(task.task_id)
        snapshot = await self._compiled.aget_state(config)
        if snapshot.next:
            return
        if snapshot.values and snapshot.values.get("status") != "WAITING_EVENT":
            return
        state: InventoryWatchState = {
            "task_id": task.task_id,
            "run_id": task.run_id,
            "book_id": task.book_id,
            "desired_quantity": task.desired_quantity,
            "status": "WAITING_EVENT",
            "terminal_event": None,
        }
        result = await self._compiled.ainvoke(state, config=config, durability="sync")
        if not result.get("__interrupt__"):
            raise RuntimeError("inventory watch graph did not enter WAITING_EVENT")

    async def finish(self, task: AgentTask, event: dict[str, Any]) -> None:
        config = self._config(task.task_id)
        snapshot = await self._compiled.aget_state(config)
        if not snapshot.next:
            if snapshot.values and snapshot.values.get("terminal_event") is not None:
                return
            await self.ensure_waiting(task)
        await self._compiled.ainvoke(Command(resume=event), config=config, durability="sync")

    @staticmethod
    def _wait(state: InventoryWatchState) -> dict[str, Any]:
        event = interrupt(
            {
                "kind": "INVENTORY_WATCH",
                "graph_version": InventoryWatchGraph.version,
                "task_id": state["task_id"],
                "run_id": state["run_id"],
            }
        )
        if not isinstance(event, dict) or event.get("kind") not in {
            "InventoryStockEvent",
            "TaskCancelledEvent",
            "TaskExpiredEvent",
        }:
            raise ValueError("invalid inventory watch continuation")
        return {"status": "COMPLETED", "terminal_event": event}

    @staticmethod
    def _config(task_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": task_id}, "recursion_limit": 4}
