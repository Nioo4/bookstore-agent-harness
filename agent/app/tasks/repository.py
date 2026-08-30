from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from app.tasks.errors import TaskError
from app.tasks.types import AgentTask, CheckpointStatus, Notification, TaskStatus


class TaskRepository(Protocol):
    async def find_by_idempotency(self, user_id: int, key: str) -> AgentTask | None: ...
    async def create(self, task: AgentTask) -> tuple[AgentTask, bool]: ...
    async def mark_checkpoint(self, task_id: str, status: CheckpointStatus) -> AgentTask: ...
    async def get(self, user_id: int, task_id: str) -> AgentTask: ...
    async def list(self, user_id: int, task_type: str | None, status: TaskStatus | None) -> list[AgentTask]: ...
    async def cancel(self, user_id: int, task_id: str, now: datetime) -> tuple[AgentTask, bool]: ...
    async def list_notifications(self, user_id: int, unread_only: bool) -> list[Notification]: ...
    async def read_notification(self, user_id: int, notification_id: str, now: datetime) -> tuple[Notification, bool]: ...
    async def reconcile_after_create(
        self, task: AgentTask, available_stock: int, inventory_version: int, now: datetime
    ) -> AgentTask: ...
    async def finalize_triggered_task(self, task_id: str, now: datetime) -> AgentTask: ...
    async def checkpoint_thread_id_for_run(self, run_id: str) -> str | None: ...


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self.tasks: dict[str, AgentTask] = {}
        self.notifications: dict[str, Notification] = {}

    async def find_by_idempotency(self, user_id: int, key: str) -> AgentTask | None:
        return next(
            (item for item in self.tasks.values() if item.user_id == user_id and item.create_idempotency_key == key),
            None,
        )

    async def checkpoint_thread_id_for_run(self, run_id: str) -> str | None:
        task = next((item for item in self.tasks.values() if item.run_id == run_id), None)
        return task.task_id if task is not None else None

    async def create(self, task: AgentTask) -> tuple[AgentTask, bool]:
        replay = await self.find_by_idempotency(task.user_id, task.create_idempotency_key)
        if replay is not None:
            if replay.request_hash != task.request_hash:
                raise TaskError("IDEMPOTENCY_CONFLICT", "idempotency key conflicts with another request")
            return replay, True
        active = next(
            (
                item
                for item in self.tasks.values()
                if item.user_id == task.user_id
                and item.book_id == task.book_id
                and item.task_type == task.task_type
                and item.status in {TaskStatus.ACTIVE, TaskStatus.TRIGGERED}
            ),
            None,
        )
        if active is not None:
            raise TaskError("INVENTORY_WATCH_ALREADY_ACTIVE", "an inventory watch is already active")
        self.tasks[task.task_id] = task
        return task, False

    async def mark_checkpoint(self, task_id: str, status: CheckpointStatus) -> AgentTask:
        task = self.tasks[task_id].model_copy(update={"checkpoint_status": status, "updated_at": datetime.now(UTC)})
        self.tasks[task_id] = task
        return task

    async def get(self, user_id: int, task_id: str) -> AgentTask:
        task = self.tasks.get(task_id)
        if task is None or task.user_id != user_id:
            raise TaskError("TASK_NOT_FOUND", "task not found")
        return task

    async def list(self, user_id: int, task_type: str | None, status: TaskStatus | None) -> list[AgentTask]:
        values = [
            item
            for item in self.tasks.values()
            if item.user_id == user_id
            and (task_type is None or item.task_type == task_type)
            and (status is None or item.status == status)
        ]
        return sorted(values, key=lambda item: (item.created_at, item.task_id), reverse=True)

    async def cancel(self, user_id: int, task_id: str, now: datetime) -> tuple[AgentTask, bool]:
        task = await self.get(user_id, task_id)
        if task.status is TaskStatus.CANCELLED:
            return task, True
        if task.status is not TaskStatus.ACTIVE:
            raise TaskError("TASK_NOT_CANCELLABLE", "task is not cancellable")
        task = task.model_copy(
            update={"status": TaskStatus.CANCELLED, "cancelled_at": now, "updated_at": now}
        )
        self.tasks[task_id] = task
        return task, False

    async def list_notifications(self, user_id: int, unread_only: bool) -> list[Notification]:
        values = [
            item
            for item in self.notifications.values()
            if item.user_id == user_id and (not unread_only or item.status == "UNREAD")
        ]
        return sorted(values, key=lambda item: (item.created_at, item.notification_id), reverse=True)

    async def read_notification(self, user_id: int, notification_id: str, now: datetime) -> tuple[Notification, bool]:
        item = self.notifications.get(notification_id)
        if item is None or item.user_id != user_id:
            raise TaskError("NOTIFICATION_NOT_FOUND", "notification not found")
        if item.status == "READ":
            return item, True
        item = item.model_copy(update={"status": "READ", "read_at": now})
        self.notifications[notification_id] = item
        return item, False

    async def reconcile_after_create(
        self, task: AgentTask, available_stock: int, inventory_version: int, now: datetime
    ) -> AgentTask:
        if inventory_version <= task.last_seen_inventory_version:
            return task
        if available_stock < task.desired_quantity:
            task = task.model_copy(
                update={
                    "last_seen_inventory_version": inventory_version,
                    "last_seen_available_stock": available_stock,
                    "updated_at": now,
                }
            )
            self.tasks[task.task_id] = task
        return task

    async def finalize_triggered_task(self, task_id: str, now: datetime) -> AgentTask:
        task = self.tasks[task_id]
        if task.status is TaskStatus.COMPLETED:
            return task
        task = task.model_copy(update={"status": TaskStatus.COMPLETED, "completed_at": now, "updated_at": now})
        self.tasks[task_id] = task
        return task
