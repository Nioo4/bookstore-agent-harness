from __future__ import annotations

"""Durable v2 background worker.

The worker consumes MySQL rows directly.  Redis is intentionally absent from
this module: an outbox row plus a lease is the delivery queue and the database
transaction is the idempotency boundary.
"""

import asyncio
import hashlib
import json
import os
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncmy
from asyncmy.cursors import DictCursor

from app.memory.repository import _parse_mysql_url
from app.memory.legacy_delete import LegacyMemoryDeletionStore


# Only these events are owned by this worker.  Unknown future events must stay
# in the outbox for the owner that understands them; they must not be leased
# and eventually marked FAILED by the inventory worker.
SUPPORTED_OUTBOX_EVENT_TYPES = (
    "inventory.stock_changed.v1",
    "order.placed.v1",
    "order.confirmation_rejected.v1",
)


def retry_delay_seconds(attempt_count: int) -> int:
    """Return the v2 attempt-1 exponential retry delay, capped at five minutes."""
    return min(2 ** max(attempt_count - 1, 0), 300)


class MySQLAgentWorker:
    def __init__(
        self,
        dsn: str,
        *,
        qdrant_memory: LegacyMemoryDeletionStore | None = None,
        poll_interval: float = 1.0,
        batch_size: int = 50,
        lease_seconds: int = 30,
        max_attempts: int = 10,
        retention_scan_interval: float = 60.0,
    ) -> None:
        if not dsn:
            raise ValueError("WORKER_MYSQL_DSN is required")
        self.dsn = dsn
        self.qdrant_memory = qdrant_memory
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        if retention_scan_interval <= 0:
            raise ValueError("retention_scan_interval must be positive")
        self.retention_scan_interval = retention_scan_interval
        self._next_retention_scan = 0.0
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        self.pool: asyncmy.Pool | None = None

    async def startup(self) -> None:
        if self.pool is None:
            self.pool = await asyncmy.create_pool(
                minsize=1, maxsize=4, autocommit=False, **_parse_mysql_url(self.dsn)
            )

    async def aclose(self) -> None:
        if self.pool is not None:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None

    async def run_forever(self) -> None:
        await self.startup()
        while True:
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(self.poll_interval)

    async def run_once(self) -> int:
        if self.pool is None:
            await self.startup()
        assert self.pool is not None
        events = await self._claim_outbox()
        processed = 0
        for event in events:
            try:
                if event["event_type"] == "inventory.stock_changed.v1":
                    await self._process_inventory_event(event)
                else:
                    await self._process_order_event(event)
                processed += 1
            except Exception as exc:
                # The SELECT snapshot precedes the lease UPDATE, therefore
                # the row's attempt count in the snapshot is one lower than
                # the count that is now persisted.
                attempt = int(event.get("attempt_count") or 0) + 1
                await self._release(
                    event["event_id"],
                    f"{type(exc).__name__}: event processing failed",
                    attempt_count=attempt,
                )
        processed += await self._process_memory_deletes()
        now = time.monotonic()
        if now >= self._next_retention_scan:
            await self.cleanup_trace_retention()
            self._next_retention_scan = now + self.retention_scan_interval
        return processed

    async def cleanup_trace_retention(self, *, batch_size: int = 100) -> tuple[int, int]:
        """Soft/hard delete only expired terminal Trace rows in bounded batches."""
        if self.pool is None:
            return 0, 0
        now = datetime.now(UTC).replace(tzinfo=None)
        soft = hard = 0
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "SELECT run_id FROM agent_run WHERE deleted_at IS NULL "
                        "AND retention_expires_at<=%s AND status IN ('COMPLETED','FAILED','CANCELLED') "
                        "ORDER BY retention_expires_at,run_id LIMIT %s FOR UPDATE SKIP LOCKED",
                        (now, batch_size),
                    )
                    rows = list(await cursor.fetchall())
                    if rows:
                        ids = [row["run_id"] for row in rows]
                        placeholders = ",".join(["%s"] * len(ids))
                        await cursor.execute(
                            f"UPDATE agent_run SET deleted_at=%s,updated_at=%s WHERE run_id IN ({placeholders})",
                            (now, now, *ids),
                        )
                        soft = cursor.rowcount
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        # A second bounded transaction removes rows whose children are gone;
        # FK CASCADE removes steps/evidence/fixtures.  Select parent IDs first
        # instead of relying on a multi-table DELETE with LIMIT: that form is
        # rejected by some MySQL-compatible servers and makes the batch bound
        # harder to audit.
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "SELECT parent.run_id FROM agent_run parent LEFT JOIN agent_run child "
                        "ON child.source_run_id=parent.run_id "
                        "WHERE parent.deleted_at IS NOT NULL "
                        "AND parent.deleted_at<=UTC_TIMESTAMP(3)-INTERVAL 24 HOUR "
                        "AND child.run_id IS NULL ORDER BY parent.deleted_at,parent.run_id "
                        "LIMIT %s FOR UPDATE SKIP LOCKED",
                        (batch_size,),
                    )
                    ids = [row["run_id"] for row in await cursor.fetchall()]
                    if ids:
                        placeholders = ",".join(["%s"] * len(ids))
                        await cursor.execute(
                            f"DELETE FROM agent_run WHERE run_id IN ({placeholders})",
                            tuple(ids),
                        )
                        hard = cursor.rowcount
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        return soft, hard

    async def _claim_outbox(self) -> list[dict[str, Any]]:
        assert self.pool is not None
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor(DictCursor) as cursor:
                    placeholders = ",".join(["%s"] * len(SUPPORTED_OUTBOX_EVENT_TYPES))
                    await cursor.execute(
                        "SELECT * FROM outbox_event WHERE event_type IN (" + placeholders + ") AND "
                        "attempt_count < %s AND "
                        "((status='PENDING' AND available_at<=UTC_TIMESTAMP(3)) OR "
                        "(status='PROCESSING' AND lease_expires_at<=UTC_TIMESTAMP(3))) "
                        "ORDER BY available_at,created_at,event_id LIMIT %s FOR UPDATE SKIP LOCKED",
                        (*SUPPORTED_OUTBOX_EVENT_TYPES, self.max_attempts, self.batch_size),
                    )
                    rows = list(await cursor.fetchall())
                    claimed: list[dict[str, Any]] = []
                    for row in rows:
                        await cursor.execute(
                            "UPDATE outbox_event SET status='PROCESSING',lease_owner=%s,"
                            "lease_expires_at=UTC_TIMESTAMP(3)+INTERVAL %s SECOND,attempt_count=attempt_count+1 "
                            "WHERE event_id=%s AND (status='PENDING' OR (status='PROCESSING' AND lease_expires_at<=UTC_TIMESTAMP(3)))",
                            (self.owner, self.lease_seconds, row["event_id"]),
                        )
                        if cursor.rowcount == 1:
                            claimed.append(row)
                await connection.commit()
                return claimed
            except Exception:
                await connection.rollback()
                raise

    async def _process_inventory_event(self, event: dict[str, Any]) -> None:
        payload = _validate_inventory_event(event)
        book_id = int(payload["book_id"])
        version = int(payload["inventory_version"])
        available = int(payload["available_stock"])
        event_id = str(event["event_id"])
        payload_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).digest()
        assert self.pool is not None
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "SELECT event_type,payload_hash,book_id,inventory_version,status "
                        "FROM inventory_event_consumption WHERE event_id=%s FOR UPDATE",
                        (event_id,),
                    )
                    existing = await cursor.fetchone()
                    if existing is not None:
                        if (
                            str(existing["event_type"]) != str(event["event_type"])
                            or bytes(existing["payload_hash"]) != payload_hash
                            or int(existing["book_id"]) != book_id
                            or int(existing["inventory_version"]) != version
                        ):
                            raise ValueError("inventory event identity or payload conflict")
                        if existing["status"] == "COMPLETED":
                            await self._mark_processed(cursor, event_id)
                            await connection.commit()
                            return
                    if existing is None:
                        await cursor.execute(
                            "INSERT INTO inventory_event_consumption "
                            "(event_id,source_event_ref,event_type,payload_hash,book_id,inventory_version,status,attempt_count) "
                            "VALUES (%s,%s,%s,%s,%s,%s,'PROCESSING',1)",
                            (event_id, event_id, event["event_type"], payload_hash, book_id, version),
                        )
                    await cursor.execute(
                        "SELECT * FROM agent_task WHERE task_type='INVENTORY_WATCH' AND status='ACTIVE' "
                        "AND book_id=%s AND desired_quantity<=%s AND last_seen_inventory_version<%s FOR UPDATE",
                        (book_id, available, version),
                    )
                    tasks = list(await cursor.fetchall())
                    now = datetime.now(UTC).replace(tzinfo=None)
                    for task in tasks:
                        await cursor.execute(
                            "UPDATE agent_task SET status='COMPLETED',active_slot=NULL,trigger_event_id=%s,"
                            "triggered_at=%s,completed_at=%s,last_seen_inventory_version=%s,"
                            "last_seen_available_stock=%s,updated_at=%s WHERE task_id=%s AND status='ACTIVE'",
                            (event_id, now, now, version, available, now, task["task_id"]),
                        )
                        notification_payload = json.dumps(
                            {
                                "book_id": book_id,
                                "book_title_snapshot": task["book_title_snapshot"],
                                "desired_quantity": int(task["desired_quantity"]),
                                "available_stock": available,
                                "inventory_version": version,
                                "triggered_at": now.isoformat(),
                            }, ensure_ascii=False, separators=(",", ":"),
                        )
                        await cursor.execute(
                            "INSERT IGNORE INTO notification "
                            "(notification_id,user_id,task_id,notification_type,status,title,body,payload_json,created_at,updated_at) "
                            "VALUES (%s,%s,%s,'INVENTORY_AVAILABLE','UNREAD',%s,%s,%s,%s,%s)",
                            (
                                str(uuid.uuid4()), int(task["user_id"]), task["task_id"],
                                "书籍已补货", f"《{task['book_title_snapshot']}》已满足监听数量。",
                                notification_payload, now, now,
                            ),
                        )
                    await cursor.execute(
                        "UPDATE inventory_event_consumption SET status='COMPLETED',completed_at=%s,last_error=NULL WHERE event_id=%s",
                        (now, event_id),
                    )
                    await self._mark_processed(cursor, event_id)
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

    async def _process_order_event(self, event: dict[str, Any]) -> None:
        """Acknowledge transactional order audit events without side effects."""
        event_type = event.get("event_type")
        if event_type not in {"order.placed.v1", "order.confirmation_rejected.v1"}:
            raise ValueError("unsupported order event type")
        _validate_order_audit_event(event)
        assert self.pool is not None
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor() as cursor:
                    await self._mark_processed(cursor, str(event["event_id"]))
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise

    async def _release(
        self,
        event_id: str,
        message: str,
        *,
        attempt_count: int = 1,
    ) -> None:
        if self.pool is None:
            return
        safe = message[:512]
        delay = retry_delay_seconds(attempt_count)
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE outbox_event SET status=CASE WHEN attempt_count >= %s THEN 'FAILED' ELSE 'PENDING' END,"
                        "available_at=CASE WHEN attempt_count >= %s THEN available_at ELSE UTC_TIMESTAMP(3)+INTERVAL %s SECOND END,"
                        "lease_owner=NULL,lease_expires_at=NULL,last_error=%s WHERE event_id=%s AND lease_owner=%s",
                        (self.max_attempts, self.max_attempts, delay, safe, event_id, self.owner),
                    )
                await connection.commit()
            except Exception:
                await connection.rollback()

    async def _process_memory_deletes(self) -> int:
        """Delete only legacy EPISODE vectors; never index new memory."""
        if self.pool is None or self.qdrant_memory is None:
            return 0
        async with self.pool.acquire() as connection:
            await connection.begin()
            try:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "SELECT * FROM memory_outbox WHERE event_type='memory.delete_requested.v1' "
                        "AND status IN ('PENDING','FAILED') AND attempt_count < %s "
                        "AND available_at<=UTC_TIMESTAMP(3) "
                        "ORDER BY created_at,event_id LIMIT %s FOR UPDATE SKIP LOCKED",
                        (self.max_attempts, self.batch_size),
                    )
                    rows = list(await cursor.fetchall())
                    for row in rows:
                        await cursor.execute(
                            "UPDATE memory_outbox SET available_at=UTC_TIMESTAMP(3)+INTERVAL 1 MINUTE,attempt_count=attempt_count+1 WHERE event_id=%s",
                            (row["event_id"],),
                        )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        completed = 0
        for row in rows:
            try:
                await self.qdrant_memory.delete(str(row["memory_id"]))
                async with self.pool.acquire() as connection:
                    async with connection.cursor() as cursor:
                        await cursor.execute(
                            "UPDATE memory_outbox SET status='PUBLISHED',published_at=UTC_TIMESTAMP(3),last_error=NULL WHERE event_id=%s",
                            (row["event_id"],),
                        )
                        await cursor.execute(
                            "UPDATE user_memory SET vector_status='DELETED',updated_at=UTC_TIMESTAMP(3) WHERE memory_id=%s AND status='DELETED'",
                            (row["memory_id"],),
                        )
                    await connection.commit()
                completed += 1
            except Exception as exc:
                async with self.pool.acquire() as connection:
                    async with connection.cursor() as cursor:
                        terminal = int(row["attempt_count"]) + 1 >= self.max_attempts
                        await cursor.execute(
                            "UPDATE memory_outbox SET status=%s,last_error=%s WHERE event_id=%s",
                            ("FAILED" if terminal else "PENDING", f"{type(exc).__name__}: vector delete failed"[:512], row["event_id"]),
                        )
                        if terminal:
                            await cursor.execute(
                                "UPDATE user_memory SET vector_status='FAILED',updated_at=UTC_TIMESTAMP(3) "
                                "WHERE memory_id=%s AND status='DELETED'",
                                (row["memory_id"],),
                            )
                    await connection.commit()
        return completed

    async def _mark_processed(self, cursor: Any, event_id: str) -> None:
        await cursor.execute(
            "UPDATE outbox_event SET status='PROCESSED',processed_at=UTC_TIMESTAMP(3),lease_owner=NULL,lease_expires_at=NULL,last_error=NULL "
            "WHERE event_id=%s AND lease_owner=%s",
            (event_id, self.owner),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("outbox lease lost before commit")


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("event payload must be a JSON object")


def _validate_inventory_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the producer identity before any consumption-side effect.

    The current Go Order transaction writes ``aggregate_type=INVENTORY`` and
    the book ID as ``aggregate_id``.  Payload identity is checked as well so a
    malformed or cross-book event cannot be acknowledged as a valid stock
    change.
    """
    event_id = event.get("event_id")
    event_type = event.get("event_type")
    if event_type != "inventory.stock_changed.v1":
        raise ValueError("unsupported outbox event type")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("inventory event row has no event_id")
    try:
        uuid.UUID(event_id)
    except (ValueError, AttributeError):
        raise ValueError("inventory event_id must be a UUID") from None
    payload = _json(event.get("payload_json"))
    if payload.get("event_id") != event_id:
        raise ValueError("inventory payload event_id does not match row")
    if payload.get("event_type") != event_type:
        raise ValueError("inventory payload event_type does not match row")
    _require_rfc3339(payload.get("occurred_at"))
    book_id = _positive_int(payload.get("book_id"), "book_id")
    available_stock = payload.get("available_stock")
    if isinstance(available_stock, bool) or not isinstance(available_stock, int) or available_stock < 0:
        raise ValueError("inventory available_stock must be a non-negative integer")
    inventory_version = _positive_int(payload.get("inventory_version"), "inventory_version")
    if str(event.get("aggregate_type") or "") != "INVENTORY" or str(event.get("aggregate_id") or "") != str(book_id):
        raise ValueError("inventory aggregate identity does not match payload")
    return payload


def _validate_order_audit_event(event: dict[str, Any]) -> None:
    """Validate an order audit payload before acknowledging its outbox row.

    These events have no worker side effect, but acknowledging malformed data
    would hide a producer/schema regression.  The row is therefore retried
    through the normal lease path and eventually becomes FAILED at the
    configured attempt limit.
    """
    event_id = event.get("event_id")
    event_type = event.get("event_type")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("order audit row has no event_id")
    payload = _json(event.get("payload_json"))
    if payload.get("event_id") != event_id:
        raise ValueError("order audit payload event_id does not match row")
    _require_rfc3339(payload.get("occurred_at"))

    aggregate_type = str(event.get("aggregate_type") or "")
    aggregate_id = str(event.get("aggregate_id") or "")
    if event_type == "order.placed.v1":
        order_id = _positive_int(payload.get("order_id"), "order_id")
        _positive_int(payload.get("user_id"), "user_id")
        if payload.get("status") != "PLACED":
            raise ValueError("order.placed payload status must be PLACED")
        if aggregate_type != "order" or aggregate_id != str(order_id):
            raise ValueError("order.placed aggregate identity does not match payload")
        return

    confirmation_id = payload.get("confirmation_id")
    conversation_id = payload.get("conversation_id")
    if not isinstance(confirmation_id, str) or not confirmation_id:
        raise ValueError("rejection payload confirmation_id is required")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("rejection payload conversation_id is required")
    if payload.get("decision") != "REJECT":
        raise ValueError("rejection payload decision must be REJECT")
    if aggregate_type != "pending_action" or aggregate_id != confirmation_id:
        raise ValueError("rejection aggregate identity does not match payload")


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"event {field} must be a positive integer")
    return value


def _require_rfc3339(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("event occurred_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("event occurred_at must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("event occurred_at must be RFC3339 with a timezone")
