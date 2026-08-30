from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Callable

from app.api.auth import AuthUser
from app.api.conversations import ConversationNotFound, ConversationStore
from app.tasks.errors import TaskDependencyUnavailable, TaskError
from app.tasks.graph import InventoryWatchRuntime
from app.tasks.repository import TaskRepository
from app.tasks.types import AgentTask, CheckpointStatus, InventoryWatchRequest, TaskStatus
from app.tasks.trace import InventoryWatchTrace
from app.tools.clients import CatalogClient
from app.tools.errors import ToolError
from app.tools.schemas import GetBookInput


class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        conversations: ConversationStore,
        catalog: CatalogClient,
        graph: InventoryWatchRuntime,
        *,
        now: Callable[[], datetime] | None = None,
        default_ttl: timedelta = timedelta(days=30),
        max_ttl: timedelta = timedelta(days=90),
        trace: InventoryWatchTrace | None = None,
    ) -> None:
        self.repository = repository
        self.conversations = conversations
        self.catalog = catalog
        self.graph = graph
        self.now = now or (lambda: datetime.now(UTC))
        self.default_ttl = default_ttl
        self.max_ttl = max_ttl
        self.trace = trace

    async def create(
        self,
        request: InventoryWatchRequest,
        *,
        idempotency_key: str,
        access_token: str,
        user: AuthUser,
    ) -> tuple[AgentTask, bool]:
        if not user.email_verified:
            raise TaskError("EMAIL_NOT_VERIFIED", "email must be verified")
        if not 16 <= len(idempotency_key) <= 128:
            raise TaskError("INVALID_TASK_REQUEST", "Idempotency-Key must contain 16 to 128 characters")
        try:
            await self.conversations.assert_owner(request.conversation_id, user.user_id)
        except ConversationNotFound as exc:
            raise TaskError("CONVERSATION_NOT_FOUND", "conversation not found") from exc
        now = self.now().astimezone(UTC)
        explicit_expiry, request_hash = self._normalized_request(request)
        existing = await self.repository.find_by_idempotency(user.user_id, idempotency_key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise TaskError("IDEMPOTENCY_CONFLICT", "idempotency key conflicts with another request")
            recorder = await self._start_trace(existing)
            existing = await self._ensure_checkpoint(existing)
            existing = await self._reconcile(existing)
            await self._sync_trace(recorder, existing)
            return existing, True
        expires_at = explicit_expiry or now + self.default_ttl
        if expires_at < now + timedelta(hours=1) or expires_at > now + self.max_ttl:
            raise TaskError("INVALID_TASK_REQUEST", "expires_at must be between 1 hour and 90 days")
        try:
            book = await self.catalog.get_book(GetBookInput(book_id=request.book_id), timeout=3.0)
        except ToolError as exc:
            if exc.code in {"BOOK_NOT_FOUND", "BOOK_INACTIVE"}:
                raise TaskError(exc.code, str(exc)) from exc
            raise TaskDependencyUnavailable("catalog is unavailable") from exc
        except Exception as exc:
            raise TaskDependencyUnavailable("catalog is unavailable") from exc
        if book.get("status") != "ACTIVE":
            raise TaskError("BOOK_INACTIVE", "book is inactive")
        available = int(book["available_stock"])
        if available >= request.desired_quantity:
            raise TaskError("INVENTORY_WATCH_NOT_NEEDED", "inventory already satisfies desired quantity")
        task = AgentTask(
            task_id=str(uuid.uuid4()),
            user_id=user.user_id,
            conversation_id=request.conversation_id,
            run_id=str(uuid.uuid4()),
            status=TaskStatus.ACTIVE,
            book_id=request.book_id,
            book_title=str(book["title"]),
            desired_quantity=request.desired_quantity,
            last_seen_inventory_version=int(book["version"]),
            last_seen_available_stock=available,
            create_idempotency_key=idempotency_key,
            request_hash=request_hash,
            checkpoint_status=CheckpointStatus.PENDING,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        task, replayed = await self.repository.create(task)
        recorder = await self._start_trace(task)
        task = await self._ensure_checkpoint(task)
        task = await self._reconcile(task)
        await self._sync_trace(recorder, task)
        return task, replayed

    async def list_tasks(self, user_id: int, task_type: str | None, status: TaskStatus | None) -> list[AgentTask]:
        return await self.repository.list(user_id, task_type, status)

    async def page_tasks(
        self, user_id: int, task_type: str | None, status: TaskStatus | None, cursor: str | None, limit: int
    ) -> tuple[list[AgentTask], str | None]:
        values = await self.list_tasks(user_id, task_type, status)
        return _page(values, "task_id", cursor, limit)

    async def get_task(self, user_id: int, task_id: str) -> AgentTask:
        return await self.repository.get(user_id, task_id)

    async def cancel(self, user_id: int, task_id: str) -> tuple[AgentTask, bool]:
        task, replayed = await self.repository.cancel(user_id, task_id, self.now().astimezone(UTC))
        if not replayed:
            try:
                await self.graph.finish(task, {"kind": "TaskCancelledEvent", "task_id": task.task_id})
            except Exception:
                await self.repository.mark_checkpoint(task.task_id, CheckpointStatus.FAILED)
            if self.trace is not None:
                await self.trace.terminal(
                    task,
                    {"kind": "TaskCancelledEvent", "task_id": task.task_id},
                )
        return task, replayed

    async def list_notifications(self, user_id: int, unread_only: bool):
        return await self.repository.list_notifications(user_id, unread_only)

    async def page_notifications(self, user_id: int, unread_only: bool, cursor: str | None, limit: int):
        values = await self.list_notifications(user_id, unread_only)
        return _page(values, "notification_id", cursor, limit)

    async def read_notification(self, user_id: int, notification_id: str):
        return await self.repository.read_notification(user_id, notification_id, self.now().astimezone(UTC))

    async def _ensure_checkpoint(self, task: AgentTask) -> AgentTask:
        if task.checkpoint_status is CheckpointStatus.READY:
            return task
        try:
            await self.graph.ensure_waiting(task)
            return await self.repository.mark_checkpoint(task.task_id, CheckpointStatus.READY)
        except Exception as exc:
            await self.repository.mark_checkpoint(task.task_id, CheckpointStatus.FAILED)
            raise TaskDependencyUnavailable("inventory watch checkpoint is unavailable") from exc

    async def _reconcile(self, task: AgentTask) -> AgentTask:
        try:
            current = await self.catalog.get_book(GetBookInput(book_id=task.book_id), timeout=3.0)
        except Exception as exc:
            raise TaskDependencyUnavailable("catalog recheck is unavailable") from exc
        task = await self.repository.reconcile_after_create(
            task,
            int(current["available_stock"]),
            int(current["version"]),
            self.now().astimezone(UTC),
        )
        if task.status is TaskStatus.TRIGGERED:
            try:
                await self.graph.finish(
                    task,
                    {
                        "kind": "InventoryStockEvent",
                        "event_id": task.trigger_event_id,
                        "book_id": task.book_id,
                        "available_stock": task.last_seen_available_stock,
                        "inventory_version": task.last_seen_inventory_version,
                    },
                )
                task = await self.repository.finalize_triggered_task(task.task_id, self.now().astimezone(UTC))
            except Exception as exc:
                raise TaskDependencyUnavailable("inventory watch finalize is unavailable") from exc
        return task

    async def _start_trace(self, task: AgentTask):
        if self.trace is None:
            return None
        try:
            return await self.trace.start_or_attach(task)
        except Exception as exc:
            raise TaskError(
                "TRACE_DEPENDENCY_UNAVAILABLE",
                "initial inventory-watch Trace could not be created",
                retryable=True,
            ) from exc

    async def _sync_trace(self, recorder, task: AgentTask) -> None:
        if self.trace is None or recorder is None:
            return
        if task.status is TaskStatus.ACTIVE:
            await self.trace.waiting(recorder, task)
            return
        if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}:
            await self.trace.terminal(
                task,
                {"kind": f"Task{task.status.value.title()}Event", "task_id": task.task_id},
            )

    def _normalized_request(self, request: InventoryWatchRequest) -> tuple[datetime | None, bytes]:
        if request.expires_at is None:
            expires_at = None
            hash_value = {
                "conversation_id": request.conversation_id,
                "book_id": request.book_id,
                "desired_quantity": request.desired_quantity,
                "expires_mode": "DEFAULT_30D",
            }
        else:
            if request.expires_at.tzinfo is None:
                raise TaskError("INVALID_TASK_REQUEST", "expires_at must include a timezone")
            expires_at = request.expires_at.astimezone(UTC).replace(
                microsecond=(request.expires_at.microsecond // 1000) * 1000
            )
            hash_value = {
                "conversation_id": request.conversation_id,
                "book_id": request.book_id,
                "desired_quantity": request.desired_quantity,
                "expires_mode": "EXPLICIT",
                "expires_at": expires_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            }
        canonical = json.dumps(hash_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return expires_at, hashlib.sha256(canonical.encode("utf-8")).digest()


def encode_cursor(created_at: datetime, object_id: str) -> str:
    value = json.dumps([created_at.isoformat(), object_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        return datetime.fromisoformat(decoded[0]), str(decoded[1])
    except Exception as exc:
        raise TaskError("INVALID_TASK_REQUEST", "invalid cursor") from exc


def _page(values, id_field: str, cursor: str | None, limit: int):
    if not 1 <= limit <= 50:
        raise TaskError("INVALID_TASK_REQUEST", "limit must be between 1 and 50")
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        values = [
            item
            for item in values
            if (item.created_at, getattr(item, id_field)) < (cursor_time, cursor_id)
        ]
    page = values[:limit]
    next_cursor = None
    if len(values) > limit and page:
        last = page[-1]
        next_cursor = encode_cursor(last.created_at, getattr(last, id_field))
    return page, next_cursor
