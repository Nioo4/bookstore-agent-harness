from __future__ import annotations

from typing import Any
from datetime import timedelta

from app.tasks.types import AgentTask, TaskStatus
from app.trace.errors import TraceError
from app.trace.recorder import TraceRecorder, TraceRecorderFactory
from app.trace.types import RunStatus, StepStatus, StepType


class InventoryWatchTrace:
    graph_dag = {
        "version": "inventory_watch_v1",
        "nodes": [
            {
                "id": "wait_for_inventory",
                "type": "WAIT_EVENT",
                "terminal_events": [
                    "InventoryStockEvent",
                    "TaskCancelledEvent",
                    "TaskExpiredEvent",
                ],
            }
        ],
    }

    def __init__(self, factory: TraceRecorderFactory) -> None:
        self.factory = factory

    async def start_or_attach(self, task: AgentTask) -> TraceRecorder:
        try:
            return await self.factory.attach(task.run_id)
        except TraceError as exc:
            if exc.code != "TRACE_NOT_FOUND":
                raise
        try:
            return await self.factory.start_task(
                run_id=task.run_id,
                user_id=task.user_id,
                conversation_id=task.conversation_id,
                input_value={
                    "task_type": task.task_type,
                    "book_id": task.book_id,
                    "desired_quantity": task.desired_quantity,
                    "expires_at": task.expires_at.isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    ),
                },
                retention_expires_at=task.expires_at
                + timedelta(days=self.factory.retention_days),
            )
        except Exception:
            # A concurrent idempotent request may have created the same Run between attach and create.
            return await self.factory.attach(task.run_id)

    async def waiting(self, recorder: TraceRecorder, task: AgentTask) -> None:
        if recorder.run.status.terminal or recorder.run.status is RunStatus.WAITING_EVENT:
            return
        step_id = await recorder.start_step(
            node_id="inventory_watch.waiting",
            step_type=StepType.SYSTEM,
            input_value={
                "task_type": task.task_type,
                "book_id": task.book_id,
                "desired_quantity": task.desired_quantity,
                "checkpoint_status": task.checkpoint_status.value,
            },
            agent_name="inventory_watch",
            tool_version="inventory_watch_v1",
        )
        await recorder.finish_step(
            step_id,
            status=StepStatus.COMPLETED,
            output_value={"status": "WAITING_EVENT"},
        )
        await recorder.pause_for_event(dag=self.graph_dag)

    async def terminal(
        self,
        task: AgentTask,
        event: dict[str, Any],
    ) -> None:
        try:
            recorder = await self.start_or_attach(task)
        except Exception:
            # Once the task exists, Trace recovery is diagnostic and may not reverse task state.
            return
        if recorder.run.status.terminal:
            return
        step_id = await recorder.start_step(
            node_id="inventory_watch.terminal_event",
            step_type=StepType.SYSTEM,
            input_value={
                "event": _safe_event(event),
                "task_status": task.status.value,
            },
            agent_name="inventory_watch",
            tool_version="inventory_watch_v1",
        )
        await recorder.finish_step(
            step_id,
            status=StepStatus.COMPLETED,
            output_value={
                "task_status": task.status.value,
                "notification_created": task.status is TaskStatus.COMPLETED,
            },
        )
        await recorder.finish_run(
            status=(RunStatus.CANCELLED if task.status is TaskStatus.CANCELLED else RunStatus.COMPLETED),
            final_answer=_final_answer(task.status),
            dag=self.graph_dag,
            replayable=False,
            replay_block_reason="TASK_WORKFLOW_NOT_REPLAYABLE",
        )


def _safe_event(event: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "kind",
        "event_id",
        "book_id",
        "available_stock",
        "inventory_version",
        "task_id",
    }
    return {key: event[key] for key in sorted(allowed) if key in event}


def _final_answer(status: TaskStatus) -> str:
    return {
        TaskStatus.COMPLETED: "补货监听已完成并生成唯一站内通知。",
        TaskStatus.CANCELLED: "补货监听已取消。",
        TaskStatus.EXPIRED: "补货监听已到期。",
    }.get(status, "补货监听已结束。")
