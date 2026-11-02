from __future__ import annotations

"""MySQL repository for the single-agent task schema (migration 000011).

This module intentionally contains no checkpoint or graph column access.  The
inventory worker consumes the outbox and updates task state; the API only
performs user-scoped CRUD and idempotent creation/cancellation.
"""

import json
from datetime import UTC, datetime
from typing import Any

import asyncmy
from asyncmy.cursors import DictCursor

from app.memory.repository import _parse_mysql_url
from app.tasks.errors import TaskDependencyUnavailable, TaskError
from app.tasks.types import AgentTask, Notification, TaskStatus


class AsyncMySQLTaskRepositoryV2:
    def __init__(self, pool: asyncmy.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create_pool(cls, dsn: str) -> "AsyncMySQLTaskRepositoryV2":
        try:
            pool = await asyncmy.create_pool(minsize=1, maxsize=10, autocommit=False, **_parse_mysql_url(dsn))
            return cls(pool)
        except Exception as exc:
            raise TaskDependencyUnavailable("task database connection failed") from exc

    async def aclose(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()

    async def find_by_idempotency(self, user_id: int, key: str) -> AgentTask | None:
        rows = await self._query(
            "SELECT * FROM agent_task WHERE user_id=%s AND create_idempotency_key=%s LIMIT 1",
            (user_id, key),
        )
        return _task(rows[0]) if rows else None

    async def create(self, task: AgentTask) -> tuple[AgentTask, bool]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM agent_task WHERE user_id=%s AND create_idempotency_key=%s "
                            "LIMIT 1 FOR UPDATE",
                            (task.user_id, task.create_idempotency_key),
                        )
                        replay = await cursor.fetchone()
                        if replay is not None:
                            existing = _task(replay)
                            if existing.request_hash != task.request_hash:
                                raise TaskError("IDEMPOTENCY_CONFLICT", "idempotency key conflicts with another request")
                            await connection.commit()
                            return existing, True
                        await cursor.execute(
                            "SELECT task_id FROM agent_task WHERE user_id=%s AND task_type='INVENTORY_WATCH' "
                            "AND book_id=%s AND active_slot=1 LIMIT 1 FOR UPDATE",
                            (task.user_id, task.book_id),
                        )
                        if await cursor.fetchone() is not None:
                            raise TaskError("INVENTORY_WATCH_ALREADY_ACTIVE", "an inventory watch is already active")
                        await cursor.execute(
                            "INSERT INTO agent_task "
                            "(task_id,user_id,conversation_id,run_id,task_type,status,book_id,book_title_snapshot,"
                            "desired_quantity,last_seen_inventory_version,last_seen_available_stock,"
                            "create_idempotency_key,request_hash,active_slot,expires_at,created_at,updated_at) "
                            "VALUES (%s,%s,%s,%s,'INVENTORY_WATCH','ACTIVE',%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s)",
                            (
                                task.task_id,
                                task.user_id,
                                task.conversation_id,
                                task.run_id,
                                task.book_id,
                                task.book_title,
                                task.desired_quantity,
                                task.last_seen_inventory_version,
                                task.last_seen_available_stock,
                                task.create_idempotency_key,
                                task.request_hash,
                                task.expires_at,
                                task.created_at,
                                task.updated_at,
                            ),
                        )
                        await cursor.execute("SELECT * FROM agent_task WHERE task_id=%s", (task.task_id,))
                        inserted = await cursor.fetchone()
                    await connection.commit()
                    return _task(inserted), False
                except Exception:
                    await connection.rollback()
                    raise
        except TaskError:
            raise
        except asyncmy.errors.IntegrityError as exc:
            replay = await self.find_by_idempotency(task.user_id, task.create_idempotency_key)
            if replay is not None:
                if replay.request_hash != task.request_hash:
                    raise TaskError("IDEMPOTENCY_CONFLICT", "idempotency key conflicts with another request") from exc
                return replay, True
            raise TaskError("INVENTORY_WATCH_ALREADY_ACTIVE", "an inventory watch is already active") from exc
        except Exception as exc:
            raise TaskDependencyUnavailable("task create failed") from exc

    async def get(self, user_id: int, task_id: str) -> AgentTask:
        rows = await self._query(
            "SELECT * FROM agent_task WHERE user_id=%s AND task_id=%s LIMIT 1", (user_id, task_id)
        )
        if not rows:
            raise TaskError("TASK_NOT_FOUND", "task not found")
        return _task(rows[0])

    async def list(self, user_id: int, task_type: str | None, status: TaskStatus | None) -> list[AgentTask]:
        query = "SELECT * FROM agent_task WHERE user_id=%s"
        args: list[Any] = [user_id]
        if task_type is not None:
            query += " AND task_type=%s"
            args.append(task_type)
        if status is not None:
            query += " AND status=%s"
            args.append(status.value)
        query += " ORDER BY created_at DESC,task_id DESC LIMIT 1000"
        return [_task(row) for row in await self._query(query, tuple(args))]

    async def cancel(self, user_id: int, task_id: str, now: datetime) -> tuple[AgentTask, bool]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM agent_task WHERE user_id=%s AND task_id=%s LIMIT 1 FOR UPDATE",
                            (user_id, task_id),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            raise TaskError("TASK_NOT_FOUND", "task not found")
                        current = _task(row)
                        if current.status is TaskStatus.CANCELLED:
                            await connection.commit()
                            return current, True
                        if current.status is not TaskStatus.ACTIVE:
                            raise TaskError("TASK_NOT_CANCELLABLE", "task is not cancellable")
                        await cursor.execute(
                            "UPDATE agent_task SET status='CANCELLED',active_slot=NULL,cancelled_at=%s,updated_at=%s "
                            "WHERE task_id=%s AND user_id=%s AND status='ACTIVE'",
                            (now, now, task_id, user_id),
                        )
                        await cursor.execute("SELECT * FROM agent_task WHERE task_id=%s", (task_id,))
                        cancelled = await cursor.fetchone()
                    await connection.commit()
                    return _task(cancelled), False
                except Exception:
                    await connection.rollback()
                    raise
        except TaskError:
            raise
        except Exception as exc:
            raise TaskDependencyUnavailable("task cancellation failed") from exc

    async def list_notifications(self, user_id: int, unread_only: bool) -> list[Notification]:
        query = "SELECT * FROM notification WHERE user_id=%s"
        args: list[Any] = [user_id]
        if unread_only:
            query += " AND status='UNREAD'"
        query += " ORDER BY created_at DESC,notification_id DESC LIMIT 1000"
        return [_notification(row) for row in await self._query(query, tuple(args))]

    async def read_notification(self, user_id: int, notification_id: str, now: datetime) -> tuple[Notification, bool]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM notification WHERE user_id=%s AND notification_id=%s LIMIT 1 FOR UPDATE",
                            (user_id, notification_id),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            raise TaskError("NOTIFICATION_NOT_FOUND", "notification not found")
                        replayed = row["status"] == "READ"
                        if not replayed:
                            await cursor.execute(
                                "UPDATE notification SET status='READ',read_at=%s,updated_at=%s "
                                "WHERE notification_id=%s AND user_id=%s AND status='UNREAD'",
                                (now, now, notification_id, user_id),
                            )
                            await cursor.execute("SELECT * FROM notification WHERE notification_id=%s", (notification_id,))
                            row = await cursor.fetchone()
                    await connection.commit()
                    return _notification(row), replayed
                except Exception:
                    await connection.rollback()
                    raise
        except TaskError:
            raise
        except Exception as exc:
            raise TaskDependencyUnavailable("notification update failed") from exc

    async def _query(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(query, args)
                    return list(await cursor.fetchall())
        except Exception as exc:
            raise TaskDependencyUnavailable("task database read failed") from exc


class TaskRepositoryRuntime:
    """Lazy pool wrapper used by the v2 Agent API startup graph."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._repository: AsyncMySQLTaskRepositoryV2 | None = None

    async def startup(self) -> None:
        if self._repository is None:
            self._repository = await AsyncMySQLTaskRepositoryV2.create_pool(self._dsn)

    async def aclose(self) -> None:
        if self._repository is not None:
            await self._repository.aclose()
            self._repository = None

    def __getattr__(self, name: str) -> Any:
        if self._repository is None:
            raise TaskDependencyUnavailable("task repository is not started")
        return getattr(self._repository, name)


def _task(row: dict[str, Any] | None) -> AgentTask:
    if row is None:
        raise TaskDependencyUnavailable("task row was not returned")
    return AgentTask(
        task_id=str(row["task_id"]),
        user_id=int(row["user_id"]),
        conversation_id=str(row["conversation_id"]),
        run_id=str(row["run_id"]),
        task_type=str(row["task_type"]),
        status=row["status"],
        book_id=int(row["book_id"]),
        book_title=str(row["book_title_snapshot"]),
        desired_quantity=int(row["desired_quantity"]),
        last_seen_inventory_version=int(row["last_seen_inventory_version"]),
        last_seen_available_stock=int(row["last_seen_available_stock"]),
        trigger_event_id=row.get("trigger_event_id"),
        create_idempotency_key=str(row["create_idempotency_key"]),
        request_hash=bytes(row["request_hash"]),
        expires_at=_utc(row["expires_at"]),
        triggered_at=_utc(row.get("triggered_at")),
        completed_at=_utc(row.get("completed_at")),
        expired_at=_utc(row.get("expired_at")),
        cancelled_at=_utc(row.get("cancelled_at")),
        created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]),
    )


def _notification(row: dict[str, Any] | None) -> Notification:
    if row is None:
        raise TaskDependencyUnavailable("notification row was not returned")
    payload = row["payload_json"]
    if not isinstance(payload, dict):
        payload = json.loads(payload)
    return Notification(
        notification_id=str(row["notification_id"]),
        user_id=int(row["user_id"]),
        task_id=str(row["task_id"]),
        notification_type=str(row["notification_type"]),
        status=str(row["status"]),
        title=str(row["title"]),
        body=str(row["body"]),
        payload=payload,
        read_at=_utc(row.get("read_at")),
        created_at=_utc(row["created_at"]),
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
