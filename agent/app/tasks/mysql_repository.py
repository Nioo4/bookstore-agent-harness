from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncmy
from asyncmy.cursors import DictCursor

from app.memory.repository import _parse_mysql_url
from app.tasks.errors import TaskDependencyUnavailable, TaskError
from app.tasks.types import AgentTask, CheckpointStatus, Notification, TaskStatus


class AsyncMySQLTaskRepository:
    def __init__(self, pool: asyncmy.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create_pool(cls, dsn: str) -> "AsyncMySQLTaskRepository":
        try:
            pool = await asyncmy.create_pool(
                minsize=1, maxsize=10, autocommit=False, **_parse_mysql_url(dsn)
            )
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

    async def checkpoint_thread_id_for_run(self, run_id: str) -> str | None:
        rows = await self._query(
            "SELECT task_id FROM agent_task WHERE run_id=%s LIMIT 1",
            (run_id,),
        )
        return str(rows[0]["task_id"]) if rows else None

    async def create(self, task: AgentTask) -> tuple[AgentTask, bool]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM agent_task WHERE user_id=%s AND create_idempotency_key=%s LIMIT 1 FOR UPDATE",
                            (task.user_id, task.create_idempotency_key),
                        )
                        replay = await cursor.fetchone()
                        if replay is not None:
                            await connection.commit()
                            existing = _task(replay)
                            if existing.request_hash != task.request_hash:
                                raise TaskError("IDEMPOTENCY_CONFLICT", "idempotency key conflicts with another request")
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
                            "create_idempotency_key,request_hash,checkpoint_status,active_slot,expires_at,created_at,updated_at) "
                            "VALUES (%s,%s,%s,%s,'INVENTORY_WATCH','ACTIVE',%s,%s,%s,%s,%s,%s,%s,'PENDING',1,%s,%s,%s)",
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

    async def mark_checkpoint(self, task_id: str, status: CheckpointStatus) -> AgentTask:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "UPDATE agent_task SET checkpoint_status=%s,updated_at=UTC_TIMESTAMP(3) WHERE task_id=%s",
                        (status.value, task_id),
                    )
                    await cursor.execute("SELECT * FROM agent_task WHERE task_id=%s", (task_id,))
                    row = await cursor.fetchone()
                await connection.commit()
            return _task(row)
        except Exception as exc:
            raise TaskDependencyUnavailable("checkpoint status update failed") from exc

    async def get(self, user_id: int, task_id: str) -> AgentTask:
        rows = await self._query(
            "SELECT * FROM agent_task WHERE user_id=%s AND task_id=%s LIMIT 1", (user_id, task_id)
        )
        if not rows:
            raise TaskError("TASK_NOT_FOUND", "task not found")
        return _task(rows[0])

    async def list(
        self, user_id: int, task_type: str | None, status: TaskStatus | None
    ) -> list[AgentTask]:
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

    async def read_notification(
        self, user_id: int, notification_id: str, now: datetime
    ) -> tuple[Notification, bool]:
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
                            await cursor.execute(
                                "SELECT * FROM notification WHERE notification_id=%s", (notification_id,)
                            )
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

    async def begin_inventory_event(
        self,
        *,
        event_id: str,
        stream_entry_id: str,
        event_type: str,
        payload_hash: bytes,
        book_id: int,
        inventory_version: int,
    ) -> tuple[str, int]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM inventory_event_consumption WHERE event_id=%s LIMIT 1 FOR UPDATE",
                            (event_id,),
                        )
                        row = await cursor.fetchone()
                        if row is not None:
                            if bytes(row["payload_hash"]) != payload_hash:
                                raise TaskError("INVENTORY_EVENT_CONFLICT", "event id payload conflict")
                            if int(row["book_id"]) != book_id or int(row["inventory_version"]) != inventory_version:
                                raise TaskError("INVENTORY_EVENT_CONFLICT", "event identity conflict")
                            if row["status"] in {"COMPLETED", "DEAD_LETTER"}:
                                await connection.commit()
                                return str(row["status"]), int(row["attempt_count"])
                            await cursor.execute(
                                "UPDATE inventory_event_consumption SET attempt_count=attempt_count+1,last_error=NULL "
                                "WHERE event_id=%s AND status='PROCESSING'",
                                (event_id,),
                            )
                        else:
                            await cursor.execute(
                                "SELECT event_id,payload_hash FROM inventory_event_consumption "
                                "WHERE book_id=%s AND inventory_version=%s LIMIT 1 FOR UPDATE",
                                (book_id, inventory_version),
                            )
                            version_row = await cursor.fetchone()
                            if version_row is not None:
                                raise TaskError("INVENTORY_EVENT_CONFLICT", "book version maps to another event")
                            await cursor.execute(
                                "INSERT INTO inventory_event_consumption "
                                "(event_id,stream_entry_id,event_type,payload_hash,book_id,inventory_version,status,attempt_count) "
                                "VALUES (%s,%s,%s,%s,%s,%s,'PROCESSING',1)",
                                (event_id, stream_entry_id, event_type, payload_hash, book_id, inventory_version),
                            )
                        await cursor.execute(
                            "SELECT status,attempt_count FROM inventory_event_consumption WHERE event_id=%s",
                            (event_id,),
                        )
                        current = await cursor.fetchone()
                    await connection.commit()
                    return str(current["status"]), int(current["attempt_count"])
                except Exception:
                    await connection.rollback()
                    raise
        except TaskError:
            raise
        except Exception as exc:
            raise TaskDependencyUnavailable("inventory event claim failed") from exc

    async def apply_inventory_event(
        self,
        *,
        event_id: str,
        book_id: int,
        available_stock: int,
        inventory_version: int,
        now: datetime,
    ) -> tuple[list[AgentTask], list[AgentTask]]:
        triggered: list[AgentTask] = []
        expired: list[AgentTask] = []
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT status FROM inventory_event_consumption WHERE event_id=%s LIMIT 1 FOR UPDATE",
                            (event_id,),
                        )
                        consumption = await cursor.fetchone()
                        if consumption is None:
                            raise TaskError("INVENTORY_EVENT_CONFLICT", "event consumption record is missing")
                        if consumption["status"] == "COMPLETED":
                            await connection.commit()
                            return await self.tasks_triggered_by_event(event_id), []
                        await cursor.execute(
                            "SELECT * FROM agent_task WHERE task_type='INVENTORY_WATCH' AND book_id=%s "
                            "AND status='ACTIVE' AND active_slot=1 ORDER BY task_id FOR UPDATE",
                            (book_id,),
                        )
                        tasks = list(await cursor.fetchall())
                        for row in tasks:
                            task = _task(row)
                            if now >= task.expires_at:
                                await cursor.execute(
                                    "UPDATE agent_task SET status='EXPIRED',active_slot=NULL,expired_at=%s,updated_at=%s "
                                    "WHERE task_id=%s AND status='ACTIVE'",
                                    (now, now, task.task_id),
                                )
                                expired.append(
                                    task.model_copy(update={"status": TaskStatus.EXPIRED, "expired_at": now, "updated_at": now})
                                )
                            elif inventory_version <= task.last_seen_inventory_version:
                                continue
                            elif available_stock >= task.desired_quantity:
                                await cursor.execute(
                                    "UPDATE agent_task SET status='TRIGGERED',trigger_event_id=%s,triggered_at=%s,"
                                    "last_seen_inventory_version=%s,last_seen_available_stock=%s,updated_at=%s "
                                    "WHERE task_id=%s AND status='ACTIVE'",
                                    (event_id, now, inventory_version, available_stock, now, task.task_id),
                                )
                                triggered.append(
                                    task.model_copy(
                                        update={
                                            "status": TaskStatus.TRIGGERED,
                                            "trigger_event_id": event_id,
                                            "triggered_at": now,
                                            "last_seen_inventory_version": inventory_version,
                                            "last_seen_available_stock": available_stock,
                                            "updated_at": now,
                                        }
                                    )
                                )
                            else:
                                await cursor.execute(
                                    "UPDATE agent_task SET last_seen_inventory_version=%s,last_seen_available_stock=%s,updated_at=%s "
                                    "WHERE task_id=%s AND status='ACTIVE'",
                                    (inventory_version, available_stock, now, task.task_id),
                                )
                        await cursor.execute(
                            "UPDATE inventory_event_consumption SET status='COMPLETED',completed_at=%s,last_error=NULL "
                            "WHERE event_id=%s AND status='PROCESSING'",
                            (now, event_id),
                        )
                    await connection.commit()
                    return triggered, expired
                except Exception:
                    await connection.rollback()
                    raise
        except TaskError:
            raise
        except Exception as exc:
            raise TaskDependencyUnavailable("inventory event processing failed") from exc

    async def tasks_triggered_by_event(self, event_id: str) -> list[AgentTask]:
        rows = await self._query(
            "SELECT * FROM agent_task WHERE trigger_event_id=%s AND status='TRIGGERED' ORDER BY task_id",
            (event_id,),
        )
        return [_task(row) for row in rows]

    async def finalize_triggered_task(self, task_id: str, now: datetime) -> AgentTask:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute("SELECT * FROM agent_task WHERE task_id=%s LIMIT 1 FOR UPDATE", (task_id,))
                        row = await cursor.fetchone()
                        task = _task(row)
                        if task.status is TaskStatus.COMPLETED:
                            await connection.commit()
                            return task
                        if task.status is not TaskStatus.TRIGGERED:
                            raise TaskError("TASK_NOT_CANCELLABLE", "task is not triggered")
                        payload = json.dumps(
                            {
                                "book_id": task.book_id,
                                "book_title_snapshot": task.book_title,
                                "desired_quantity": task.desired_quantity,
                                "available_stock": task.last_seen_available_stock,
                                "inventory_version": task.last_seen_inventory_version,
                                "triggered_at": task.triggered_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        await cursor.execute(
                            "INSERT IGNORE INTO notification "
                            "(notification_id,user_id,task_id,notification_type,status,title,body,payload_json) "
                            "VALUES (%s,%s,%s,'INVENTORY_AVAILABLE','UNREAD',%s,%s,%s)",
                            (
                                str(uuid.uuid4()),
                                task.user_id,
                                task.task_id,
                                "补货提醒",
                                f"《{task.book_title}》在触发时库存已达到你监听的数量（触发时库存快照：{task.last_seen_available_stock}）。",
                                payload,
                            ),
                        )
                        await cursor.execute(
                            "UPDATE agent_task SET status='COMPLETED',active_slot=NULL,completed_at=%s,updated_at=%s "
                            "WHERE task_id=%s AND status='TRIGGERED'",
                            (now, now, task_id),
                        )
                        await cursor.execute("SELECT * FROM agent_task WHERE task_id=%s", (task_id,))
                        completed = await cursor.fetchone()
                    await connection.commit()
                    return _task(completed)
                except Exception:
                    await connection.rollback()
                    raise
        except TaskError:
            raise
        except Exception as exc:
            raise TaskDependencyUnavailable("task finalize failed") from exc

    async def mark_event_dead_letter(self, event_id: str, message: str, now: datetime) -> None:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE inventory_event_consumption SET status='DEAD_LETTER',last_error=%s,completed_at=%s "
                        "WHERE event_id=%s AND status='PROCESSING'",
                        (message[:1024], now, event_id),
                    )
                await connection.commit()
        except Exception as exc:
            raise TaskDependencyUnavailable("dead-letter state update failed") from exc

    async def expire_active_tasks(self, now: datetime, limit: int = 100) -> list[AgentTask]:
        expired: list[AgentTask] = []
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM agent_task WHERE status='ACTIVE' AND expires_at<=%s "
                            "ORDER BY expires_at,task_id LIMIT %s FOR UPDATE SKIP LOCKED",
                            (now, limit),
                        )
                        for row in await cursor.fetchall():
                            task = _task(row)
                            await cursor.execute(
                                "UPDATE agent_task SET status='EXPIRED',active_slot=NULL,expired_at=%s,updated_at=%s "
                                "WHERE task_id=%s AND status='ACTIVE'",
                                (now, now, task.task_id),
                            )
                            expired.append(
                                task.model_copy(update={"status": TaskStatus.EXPIRED, "expired_at": now, "updated_at": now})
                            )
                    await connection.commit()
                    return expired
                except Exception:
                    await connection.rollback()
                    raise
        except Exception as exc:
            raise TaskDependencyUnavailable("task expiry scan failed") from exc

    async def recoverable_tasks(self, limit: int = 100) -> list[AgentTask]:
        rows = await self._query(
            "SELECT * FROM agent_task WHERE status IN ('ACTIVE','TRIGGERED') "
            "ORDER BY updated_at,task_id LIMIT %s",
            (limit,),
        )
        return [_task(row) for row in rows]

    async def terminal_checkpoint_cleanup_tasks(self, limit: int = 100) -> list[AgentTask]:
        rows = await self._query(
            "SELECT * FROM agent_task WHERE status IN ('EXPIRED','CANCELLED') AND checkpoint_status='FAILED' "
            "ORDER BY updated_at,task_id LIMIT %s",
            (limit,),
        )
        return [_task(row) for row in rows]

    async def reconcile_after_create(
        self, task: AgentTask, available_stock: int, inventory_version: int, now: datetime
    ) -> AgentTask:
        if inventory_version <= task.last_seen_inventory_version:
            return task
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM agent_task WHERE task_id=%s LIMIT 1 FOR UPDATE", (task.task_id,)
                        )
                        current = _task(await cursor.fetchone())
                        if current.status is not TaskStatus.ACTIVE or inventory_version <= current.last_seen_inventory_version:
                            await connection.commit()
                            return current
                        if available_stock < current.desired_quantity:
                            await cursor.execute(
                                "UPDATE agent_task SET last_seen_inventory_version=%s,last_seen_available_stock=%s,updated_at=%s "
                                "WHERE task_id=%s AND status='ACTIVE'",
                                (inventory_version, available_stock, now, task.task_id),
                            )
                        else:
                            await cursor.execute(
                                "SELECT event_id FROM inventory_event_consumption "
                                "WHERE book_id=%s AND inventory_version=%s AND status='COMPLETED' LIMIT 1",
                                (task.book_id, inventory_version),
                            )
                            event = await cursor.fetchone()
                            if event is not None:
                                await cursor.execute(
                                    "UPDATE agent_task SET status='TRIGGERED',trigger_event_id=%s,triggered_at=%s,"
                                    "last_seen_inventory_version=%s,last_seen_available_stock=%s,updated_at=%s "
                                    "WHERE task_id=%s AND status='ACTIVE'",
                                    (
                                        event["event_id"],
                                        now,
                                        inventory_version,
                                        available_stock,
                                        now,
                                        task.task_id,
                                    ),
                                )
                        await cursor.execute("SELECT * FROM agent_task WHERE task_id=%s", (task.task_id,))
                        result = await cursor.fetchone()
                    await connection.commit()
                    return _task(result)
                except Exception:
                    await connection.rollback()
                    raise
        except Exception as exc:
            raise TaskDependencyUnavailable("post-create inventory reconciliation failed") from exc

    async def _query(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(query, args)
                    return list(await cursor.fetchall())
        except Exception as exc:
            raise TaskDependencyUnavailable("task database read failed") from exc


class TaskRepositoryRuntime:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._repository: AsyncMySQLTaskRepository | None = None

    async def startup(self) -> None:
        if self._repository is None:
            self._repository = await AsyncMySQLTaskRepository.create_pool(self._dsn)

    async def aclose(self) -> None:
        if self._repository is not None:
            await self._repository.aclose()
            self._repository = None

    def __getattr__(self, name: str):
        if self._repository is None:
            raise TaskDependencyUnavailable("task repository is not started")
        return getattr(self._repository, name)


def _task(row: dict[str, Any] | None) -> AgentTask:
    if row is None:
        raise TaskDependencyUnavailable("task row was not returned")
    return AgentTask(
        task_id=row["task_id"],
        user_id=int(row["user_id"]),
        conversation_id=row["conversation_id"],
        run_id=row["run_id"],
        task_type=row["task_type"],
        status=row["status"],
        book_id=int(row["book_id"]),
        book_title=row["book_title_snapshot"],
        desired_quantity=int(row["desired_quantity"]),
        last_seen_inventory_version=int(row["last_seen_inventory_version"]),
        last_seen_available_stock=int(row["last_seen_available_stock"]),
        trigger_event_id=row.get("trigger_event_id"),
        create_idempotency_key=row["create_idempotency_key"],
        request_hash=bytes(row["request_hash"]),
        checkpoint_status=row["checkpoint_status"],
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
        notification_id=row["notification_id"],
        user_id=int(row["user_id"]),
        task_id=row["task_id"],
        notification_type=row["notification_type"],
        status=row["status"],
        title=row["title"],
        body=row["body"],
        payload=payload,
        read_at=_utc(row.get("read_at")),
        created_at=_utc(row["created_at"]),
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
