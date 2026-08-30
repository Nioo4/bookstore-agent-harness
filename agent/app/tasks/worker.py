from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.tasks.errors import TaskError
from app.tasks.graph import InventoryWatchRuntime
from app.tasks.types import CheckpointStatus
from app.tasks.trace import InventoryWatchTrace
from app.telemetry import EVENT_RETRIES, EVENT_DEAD, TASK_BACKLOG, traced


INVENTORY_EVENT_TYPE = "inventory.stock_changed.v1"
PAYLOAD_KEYS = {
    "event_id",
    "event_type",
    "occurred_at",
    "book_id",
    "available_stock",
    "inventory_version",
}


class InventoryEventWorker:
    def __init__(
        self,
        repository: Any,
        graph: InventoryWatchRuntime,
        redis: Redis,
        *,
        stream: str = "inventory.events",
        group: str = "inventory-watch-workers",
        dlq: str = "inventory.events.dlq",
        consumer: str | None = None,
        block_ms: int = 5000,
        batch_size: int = 20,
        claim_idle_ms: int = 60_000,
        max_attempts: int = 10,
        retention_seconds: int = 7 * 24 * 60 * 60,
        expiry_scan_seconds: int = 60,
        trace: InventoryWatchTrace | None = None,
    ) -> None:
        self.repository = repository
        self.graph = graph
        self.redis = redis
        self.stream = stream
        self.group = group
        self.dlq = dlq
        self.consumer = consumer or f"{socket.gethostname()}-{uuid.uuid4()}"
        self.block_ms = block_ms
        self.batch_size = batch_size
        self.claim_idle_ms = claim_idle_ms
        self.max_attempts = max_attempts
        self.retention_seconds = retention_seconds
        self.expiry_scan_seconds = expiry_scan_seconds
        self.trace = trace
        self.log = logging.getLogger("bookstore.inventory-worker")

    async def startup(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        await self.recover_tasks()

    async def run(self) -> None:
        await self.startup()
        expiry = asyncio.create_task(self._expiry_loop())
        trim = asyncio.create_task(self._trim_loop())
        try:
            while True:
                await self._consume_claimed()
                messages = await self.redis.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream: ">"},
                    count=self.batch_size,
                    block=self.block_ms,
                )
                for _, entries in messages:
                    for entry_id, fields in entries:
                        await self.process_message(_text(entry_id), fields)
        finally:
            expiry.cancel()
            trim.cancel()
            await asyncio.gather(expiry, trim, return_exceptions=True)

    @traced("inventory.consume")
    async def process_message(self, entry_id: str, fields: dict[Any, Any]) -> None:
        event_id = ""
        try:
            event, payload_hash = _parse_event(fields)
            event_id = event["event_id"]
            status, attempts = await self.repository.begin_inventory_event(
                event_id=event_id,
                stream_entry_id=entry_id,
                event_type=event["event_type"],
                payload_hash=payload_hash,
                book_id=event["book_id"],
                inventory_version=event["inventory_version"],
            )
            if status == "DEAD_LETTER":
                await self.redis.xack(self.stream, self.group, entry_id)
                return
            if status == "COMPLETED":
                try:
                    await self._recover_event_tasks(event_id)
                except Exception:
                    self.log.warning("triggered task recovery deferred to startup recovery")
                await self.redis.xack(self.stream, self.group, entry_id)
                return
            if attempts > 1:
                EVENT_RETRIES.inc()
            triggered, expired = await self.repository.apply_inventory_event(
                event_id=event_id,
                book_id=event["book_id"],
                available_stock=event["available_stock"],
                inventory_version=event["inventory_version"],
                now=datetime.now(UTC),
            )
            for task in expired:
                await self._finish_terminal(task, {"kind": "TaskExpiredEvent", "task_id": task.task_id})
            for task in triggered:
                await self._finish_triggered(task)
            await self.redis.xack(self.stream, self.group, entry_id)
        except TaskError as exc:
            if exc.code == "INVENTORY_EVENT_CONFLICT":
                await self._dead_letter(entry_id, event_id, "inventory event conflict")
                return
            await self._retry_or_dead_letter(entry_id, event_id, exc)
        except Exception as exc:
            await self._retry_or_dead_letter(entry_id, event_id, exc)

    async def recover_tasks(self) -> None:
        for task in await self.repository.recoverable_tasks():
            if task.status.value == "ACTIVE":
                try:
                    await self.graph.ensure_waiting(task)
                    task = await self.repository.mark_checkpoint(task.task_id, CheckpointStatus.READY)
                    if self.trace is not None:
                        recorder = await self.trace.start_or_attach(task)
                        await self.trace.waiting(recorder, task)
                except Exception:
                    await self.repository.mark_checkpoint(task.task_id, CheckpointStatus.FAILED)
            elif task.status.value == "TRIGGERED":
                await self._finish_triggered(task)
        await self.cleanup_terminal_checkpoints()

    async def cleanup_terminal_checkpoints(self) -> None:
        for task in await self.repository.terminal_checkpoint_cleanup_tasks():
            kind = "TaskCancelledEvent" if task.status.value == "CANCELLED" else "TaskExpiredEvent"
            await self._finish_terminal(task, {"kind": kind, "task_id": task.task_id})

    async def expire_once(self) -> None:
        for task in await self.repository.expire_active_tasks(datetime.now(UTC)):
            await self._finish_terminal(task, {"kind": "TaskExpiredEvent", "task_id": task.task_id})

    async def trim_once(self, *, now_ms: int | None = None) -> int:
        groups = await self.redis.xinfo_groups(self.stream)
        group = next((item for item in groups if _text(item.get("name")) == self.group), None)
        if group is None:
            return 0
        current_ms = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
        cutoff = f"{current_ms - self.retention_seconds * 1000}-0"
        pending = await self.redis.xpending(self.stream, self.group)
        if int(pending.get("pending", 0)) > 0:
            watermark = _text(pending.get("min"))
        else:
            watermark = _text(group.get("last-delivered-id"))
        if not watermark or watermark == "0-0":
            return 0
        safe = min((cutoff, watermark), key=_stream_id_tuple)
        return int(await self.redis.xtrim(self.stream, minid=safe, approximate=False))

    async def _consume_claimed(self) -> None:
        start = "0-0"
        while True:
            previous = start
            result = await self.redis.xautoclaim(
                self.stream,
                self.group,
                self.consumer,
                min_idle_time=self.claim_idle_ms,
                start_id=start,
                count=self.batch_size,
            )
            start, entries = _text(result[0]), result[1]
            for entry_id, fields in entries:
                await self.process_message(_text(entry_id), fields)
            if start == "0-0":
                return
            if not entries and start == previous:
                self.log.error("inventory pending claim cursor did not advance")
                return

    async def _recover_event_tasks(self, event_id: str) -> None:
        for task in await self.repository.tasks_triggered_by_event(event_id):
            await self._finish_triggered(task)

    async def _finish_triggered(self, task) -> None:
        event = {
            "kind": "InventoryStockEvent",
            "event_id": task.trigger_event_id,
            "book_id": task.book_id,
            "available_stock": task.last_seen_available_stock,
            "inventory_version": task.last_seen_inventory_version,
        }
        await self.graph.finish(
            task,
            event,
        )
        task = await self.repository.finalize_triggered_task(task.task_id, datetime.now(UTC))
        if self.trace is not None:
            await self.trace.terminal(task, event)

    async def _finish_terminal(self, task, event: dict[str, Any]) -> None:
        try:
            await self.graph.finish(task, event)
            await self.repository.mark_checkpoint(task.task_id, CheckpointStatus.READY)
        except Exception:
            await self.repository.mark_checkpoint(task.task_id, CheckpointStatus.FAILED)
        if self.trace is not None:
            await self.trace.terminal(task, event)

    async def _retry_or_dead_letter(self, entry_id: str, event_id: str, exc: Exception) -> None:
        attempts = 0
        if event_id:
            try:
                rows = await self.repository._query(
                    "SELECT attempt_count FROM inventory_event_consumption WHERE event_id=%s", (event_id,)
                )
                attempts = int(rows[0]["attempt_count"]) if rows else 0
            except Exception:
                pass
        if attempts >= self.max_attempts:
            await self._dead_letter(entry_id, event_id, "inventory event processing failed")
        else:
            self.log.warning("inventory event processing failed; entry remains pending")

    async def _dead_letter(self, entry_id: str, event_id: str, message: str) -> None:
        await self.redis.xadd(
            self.dlq,
            {"source_stream": self.stream, "stream_entry_id": entry_id, "event_id": event_id, "error": message},
        )
        if event_id:
            await self.repository.mark_event_dead_letter(event_id, message, datetime.now(UTC))
        await self.redis.xack(self.stream, self.group, entry_id)
        EVENT_DEAD.inc()
        self.log.error("inventory event moved to DLQ")

    async def _expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(self.expiry_scan_seconds)
            try:
                await self.expire_once()
                await self.cleanup_terminal_checkpoints()
                counts = await self.repository._query(
                    "SELECT status,COUNT(*) AS count FROM agent_task WHERE status IN ('ACTIVE','TRIGGERED') GROUP BY status", ()
                )
                by_status = {row["status"]: row["count"] for row in counts}
                for status in ("ACTIVE", "TRIGGERED"):
                    TASK_BACKLOG.labels(status).set(by_status.get(status, 0))
            except Exception:
                self.log.error("inventory task expiry scan failed")

    async def _trim_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.trim_once()
            except Exception:
                self.log.error("inventory stream trim failed")


def _parse_event(fields: dict[Any, Any]) -> tuple[dict[str, Any], bytes]:
    normalized = {_text(key): _text(value) for key, value in fields.items()}
    raw = normalized.get("payload_json")
    if raw is None:
        raise TaskError("INVENTORY_EVENT_CONFLICT", "payload_json is required")
    try:
        event = json.loads(raw)
        if not isinstance(event, dict) or set(event) != PAYLOAD_KEYS:
            raise ValueError
        if event["event_type"] != INVENTORY_EVENT_TYPE or normalized.get("event_type") != INVENTORY_EVENT_TYPE:
            raise ValueError
        if event["event_id"] != normalized.get("event_id"):
            raise ValueError
        uuid.UUID(event["event_id"])
        occurred = datetime.fromisoformat(str(event["occurred_at"]).replace("Z", "+00:00"))
        if occurred.utcoffset() is None or occurred.utcoffset().total_seconds() != 0:
            raise ValueError
        event["book_id"] = int(event["book_id"])
        event["available_stock"] = int(event["available_stock"])
        event["inventory_version"] = int(event["inventory_version"])
        if event["book_id"] <= 0 or event["available_stock"] < 0 or event["inventory_version"] <= 0:
            raise ValueError
    except Exception as exc:
        raise TaskError("INVENTORY_EVENT_CONFLICT", "invalid inventory event") from exc
    canonical = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return event, hashlib.sha256(canonical).digest()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)


def _stream_id_tuple(value: str) -> tuple[int, int]:
    milliseconds, sequence = value.split("-", 1)
    return int(milliseconds), int(sequence)
