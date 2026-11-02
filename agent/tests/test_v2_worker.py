from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.memory.legacy_delete import LegacyMemoryDeletionStore
from app.tasks.v2_worker import (
    SUPPORTED_OUTBOX_EVENT_TYPES,
    MySQLAgentWorker,
    retry_delay_seconds,
)


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _InventoryCursor:
    def __init__(self, *, existing=None, lease_lost=False):
        self.existing = existing
        self.lease_lost = lease_lost
        self.rowcount = 0
        self.last_query = ""

    async def execute(self, query, args=()):
        self.last_query = query
        if "UPDATE outbox_event SET status='PROCESSED'" in query:
            self.rowcount = 0 if self.lease_lost else 1
        else:
            self.rowcount = 1

    async def fetchone(self):
        if "inventory_event_consumption" in self.last_query:
            return self.existing
        return None

    async def fetchall(self):
        if "FROM agent_task" in self.last_query:
            return []
        return []


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    async def begin(self):
        return None

    def cursor(self, *_args):
        return _CursorContext(self._cursor)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _ConnectionContext(self.connection)


class _MemoryCursor:
    """Small SQL-shape fake for the memory-delete worker path."""

    def __init__(self, row: dict[str, object], state: dict[str, object]) -> None:
        self.row = row
        self.state = state
        self.last_query = ""
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 0

    async def execute(self, query, args=()):
        self.last_query = query
        self.calls.append((query, tuple(args)))
        self.rowcount = 1
        if "SET available_at" in query and "attempt_count=attempt_count+1" in query:
            self.state["attempt_count"] = int(self.state["attempt_count"]) + 1
        elif "SET status='PUBLISHED'" in query:
            self.state["outbox_status"] = "PUBLISHED"
        elif "SET status=%s,last_error=%s" in query:
            self.state["outbox_status"] = args[0]
            self.state["last_error"] = args[1]
        elif "SET vector_status='DELETED'" in query:
            self.state["vector_status"] = "DELETED"
        elif "SET vector_status='FAILED'" in query:
            self.state["vector_status"] = "FAILED"

    async def fetchall(self):
        if "FROM memory_outbox" in self.last_query:
            return [self.row]
        return []


class _MemoryConnection:
    def __init__(self, cursor: _MemoryCursor) -> None:
        self.cursor_value = cursor
        self.commits = 0
        self.rollbacks = 0

    async def begin(self):
        return None

    def cursor(self, *_args):
        return _CursorContext(self.cursor_value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _memory_row(*, attempt_count: int = 0) -> dict[str, object]:
    return {
        "event_id": "delete-event-1",
        "event_type": "memory.delete_requested.v1",
        "memory_id": "episode-1",
        "attempt_count": attempt_count,
        "status": "PENDING",
    }


class _DeleteStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def delete(self, memory_id: str) -> None:
        self.calls.append(memory_id)
        if self.error is not None:
            raise self.error


class _ClaimCursor:
    def __init__(self, rows: list[dict], *, lease_wins: bool):
        self.rows = rows
        self.lease_wins = lease_wins
        self.calls: list[tuple[str, tuple]] = []
        self.rowcount = 0

    async def execute(self, query, args=()):
        self.calls.append((query, tuple(args)))
        self.rowcount = 1 if "UPDATE outbox_event" in query and self.lease_wins else 0

    async def fetchall(self):
        return self.rows


@pytest.mark.asyncio
async def test_retention_cleanup_is_throttled_between_poll_cycles() -> None:
    worker = MySQLAgentWorker(
        "mysql://worker:secret@localhost/bookstore",
        retention_scan_interval=60,
    )
    # run_once only needs a non-None pool after the database-facing methods are
    # replaced; this keeps the test independent of MySQL.
    worker.pool = object()  # type: ignore[assignment]
    worker._claim_outbox = AsyncMock(return_value=[])
    worker._process_memory_deletes = AsyncMock(return_value=0)
    worker.cleanup_trace_retention = AsyncMock(return_value=(0, 0))

    await worker.run_once()
    await worker.run_once()

    worker.cleanup_trace_retention.assert_awaited_once()
    worker._next_retention_scan = 0
    await worker.run_once()
    assert worker.cleanup_trace_retention.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404])
async def test_legacy_memory_delete_store_only_deletes_qdrant_point_and_treats_404_as_idempotent(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, request=request)

    store = LegacyMemoryDeletionStore("http://qdrant.test", "legacy_episode")
    await store.client.aclose()
    store.client = httpx.AsyncClient(
        base_url="http://qdrant.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        await store.delete("episode-1")
    finally:
        await store.aclose()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/collections/legacy_episode/points/delete"
    assert request.url.params["wait"] == "true"
    assert json.loads(request.content) == {"points": ["episode-1"]}


@pytest.mark.asyncio
async def test_memory_delete_success_publishes_outbox_and_marks_vector_deleted() -> None:
    state: dict[str, object] = {
        "attempt_count": 0,
        "outbox_status": "PENDING",
        "vector_status": "DELETE_PENDING",
    }
    cursor = _MemoryCursor(_memory_row(), state)
    connection = _MemoryConnection(cursor)
    worker = MySQLAgentWorker(
        "mysql://worker:secret@localhost/bookstore",
        qdrant_memory=_DeleteStore(),  # type: ignore[arg-type]
        max_attempts=3,
    )
    worker.pool = _Pool(connection)  # type: ignore[assignment]

    processed = await worker._process_memory_deletes()

    assert processed == 1
    assert worker.qdrant_memory.calls == ["episode-1"]  # type: ignore[union-attr]
    assert state == {
        "attempt_count": 1,
        "outbox_status": "PUBLISHED",
        "vector_status": "DELETED",
    }
    assert connection.commits == 2
    assert connection.rollbacks == 0


@pytest.mark.asyncio
async def test_memory_delete_temporary_failure_returns_to_pending_without_vector_failure() -> None:
    state: dict[str, object] = {
        "attempt_count": 0,
        "outbox_status": "PENDING",
        "vector_status": "DELETE_PENDING",
    }
    cursor = _MemoryCursor(_memory_row(), state)
    connection = _MemoryConnection(cursor)
    worker = MySQLAgentWorker(
        "mysql://worker:secret@localhost/bookstore",
        qdrant_memory=_DeleteStore(RuntimeError("qdrant unavailable")),  # type: ignore[arg-type]
        max_attempts=3,
    )
    worker.pool = _Pool(connection)  # type: ignore[assignment]

    assert await worker._process_memory_deletes() == 0
    assert state["attempt_count"] == 1
    assert state["outbox_status"] == "PENDING"
    assert state["vector_status"] == "DELETE_PENDING"
    assert connection.commits == 2


@pytest.mark.asyncio
async def test_memory_delete_at_attempt_limit_marks_outbox_and_vector_failed() -> None:
    state: dict[str, object] = {
        "attempt_count": 2,
        "outbox_status": "FAILED",
        "vector_status": "DELETE_PENDING",
    }
    cursor = _MemoryCursor(_memory_row(attempt_count=2), state)
    connection = _MemoryConnection(cursor)
    worker = MySQLAgentWorker(
        "mysql://worker:secret@localhost/bookstore",
        qdrant_memory=_DeleteStore(RuntimeError("qdrant unavailable")),  # type: ignore[arg-type]
        max_attempts=3,
    )
    worker.pool = _Pool(connection)  # type: ignore[assignment]

    assert await worker._process_memory_deletes() == 0
    assert state["attempt_count"] == 3
    assert state["outbox_status"] == "FAILED"
    assert state["vector_status"] == "FAILED"
    assert connection.commits == 2


def test_worker_owns_only_known_outbox_event_types() -> None:
    assert SUPPORTED_OUTBOX_EVENT_TYPES == (
        "inventory.stock_changed.v1",
        "order.placed.v1",
        "order.confirmation_rejected.v1",
    )
    assert "future.unknown.v1" not in SUPPORTED_OUTBOX_EVENT_TYPES


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_wins, expected_claims", [(True, 1), (False, 0)])
async def test_claim_filters_known_events_and_requires_lease_update(lease_wins: bool, expected_claims: int) -> None:
    rows = [{"event_id": "event-1", "event_type": "inventory.stock_changed.v1", "attempt_count": 0}]
    cursor = _ClaimCursor(rows, lease_wins=lease_wins)
    connection = _Connection(cursor)
    worker = MySQLAgentWorker("mysql://worker:secret@localhost/bookstore")
    worker.pool = _Pool(connection)  # type: ignore[assignment]

    claimed = await worker._claim_outbox()

    assert len(claimed) == expected_claims
    query, args = cursor.calls[0]
    assert "event_type IN (%s,%s,%s)" in query
    assert args[:3] == SUPPORTED_OUTBOX_EVENT_TYPES
    assert connection.commits == 1


@pytest.mark.asyncio
async def test_claim_query_excludes_events_at_retry_limit() -> None:
    class ExhaustedCursor(_ClaimCursor):
        async def fetchall(self):
            query, args = self.calls[-1]
            assert "attempt_count < %s" in query
            assert args[-2] == 10
            return []

    cursor = ExhaustedCursor(
        [{"event_id": "event-exhausted", "event_type": "inventory.stock_changed.v1", "attempt_count": 10}],
        lease_wins=True,
    )
    connection = _Connection(cursor)
    worker = MySQLAgentWorker("mysql://worker:secret@localhost/bookstore", max_attempts=10)
    worker.pool = _Pool(connection)  # type: ignore[assignment]

    assert await worker._claim_outbox() == []
    assert len(cursor.calls) == 1
    assert connection.commits == 1


def test_retry_backoff_uses_attempt_minus_one_and_stops_at_five_minutes() -> None:
    assert [retry_delay_seconds(attempt) for attempt in (1, 2, 3)] == [1, 2, 4]
    assert retry_delay_seconds(9) == 256
    assert retry_delay_seconds(10) == 300
    assert retry_delay_seconds(100) == 300


@pytest.mark.asyncio
async def test_mark_processed_rejects_a_lost_lease() -> None:
    class Cursor:
        rowcount = 0

        async def execute(self, query, args):
            return None

    worker = MySQLAgentWorker("mysql://worker:secret@localhost/bookstore")
    with pytest.raises(RuntimeError, match="lease lost"):
        await worker._mark_processed(Cursor(), "event-1")


@pytest.mark.asyncio
async def test_mark_processed_accepts_exactly_one_current_lease() -> None:
    class Cursor:
        rowcount = 1

        async def execute(self, query, args):
            return None

    worker = MySQLAgentWorker("mysql://worker:secret@localhost/bookstore")
    await worker._mark_processed(Cursor(), "event-1")


def _inventory_event(event_id: str = "00000000-0000-0000-0000-000000000001") -> dict:
    return {
        "event_id": event_id,
        "event_type": "inventory.stock_changed.v1",
        "aggregate_type": "INVENTORY",
        "aggregate_id": "7",
        "payload_json": json.dumps(
            {
                "event_id": event_id,
                "event_type": "inventory.stock_changed.v1",
                "occurred_at": "2026-09-25T12:00:00Z",
                "book_id": 7,
                "available_stock": 4,
                "inventory_version": 3,
            },
            separators=(",", ":"),
        ),
    }


@pytest.mark.asyncio
async def test_inventory_consumption_identity_conflict_rolls_back_and_is_re_raised() -> None:
    connection = _Connection(
        _InventoryCursor(
            existing={
                "event_type": "inventory.stock_changed.v1",
                "payload_hash": b"wrong-payload-hash",
                "book_id": 7,
                "inventory_version": 3,
                "status": "PROCESSING",
            }
        )
    )
    worker = MySQLAgentWorker("mysql://worker:secret@localhost/bookstore")
    worker.pool = _Pool(connection)  # type: ignore[assignment]

    with pytest.raises(ValueError, match="identity or payload conflict"):
        await worker._process_inventory_event(_inventory_event())

    assert connection.rollbacks == 1
    assert connection.commits == 0


@pytest.mark.asyncio
async def test_inventory_lease_loss_rolls_back_consumption_and_notifications() -> None:
    connection = _Connection(_InventoryCursor(lease_lost=True))
    worker = MySQLAgentWorker("mysql://worker:secret@localhost/bookstore")
    worker.pool = _Pool(connection)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="lease lost"):
        await worker._process_inventory_event(_inventory_event())

    assert connection.rollbacks == 1
    assert connection.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda event: event["payload_json"].replace('"event_id":"00000000-0000-0000-0000-000000000001"', '"event_id":"other"'), "event_id"),
        (lambda event: event["payload_json"].replace('"event_type":"inventory.stock_changed.v1"', '"event_type":"other.v1"'), "event_type"),
        (lambda event: event["payload_json"].replace('"occurred_at":"2026-09-25T12:00:00Z"', '"occurred_at":"2026-09-25T12:00:00"'), "RFC3339"),
        (lambda event: event | {"aggregate_id": "8"}, "aggregate identity"),
    ],
)
async def test_inventory_event_identity_is_validated_before_consumption(mutation, expected: str) -> None:
    event = _inventory_event()
    changed = mutation(event)
    if changed is not None:
        event = changed if isinstance(changed, dict) else {**event, "payload_json": changed}
    worker = MySQLAgentWorker("mysql://worker:secret@localhost/bookstore")

    with pytest.raises(ValueError, match=expected):
        await worker._process_inventory_event(event)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {
            "event_id": "event-1",
            "event_type": "order.placed.v1",
            "aggregate_type": "order",
            "aggregate_id": "12",
            "payload_json": json.dumps(
                {
                    "event_id": "wrong-event",
                    "order_id": 12,
                    "user_id": 7,
                    "status": "PLACED",
                    "occurred_at": "2026-09-25T12:00:00Z",
                }
            ),
        },
        {
            "event_id": "event-2",
            "event_type": "order.confirmation_rejected.v1",
            "aggregate_type": "pending_action",
            "aggregate_id": "pending-1",
            "payload_json": json.dumps(
                {
                    "event_id": "event-2",
                    "confirmation_id": "pending-1",
                    "conversation_id": "conversation-1",
                    "decision": "APPROVE",
                    "occurred_at": "2026-09-25T12:00:00Z",
                }
            ),
        },
    ],
)
async def test_order_audit_schema_errors_are_not_acknowledged(event: dict) -> None:
    worker = MySQLAgentWorker("mysql://worker:secret@localhost/bookstore")
    with pytest.raises(ValueError):
        await worker._process_order_event(event)


@pytest.mark.asyncio
async def test_valid_order_audit_event_is_acknowledged_without_side_effect() -> None:
    event = {
        "event_id": "event-3",
        "event_type": "order.placed.v1",
        "aggregate_type": "order",
        "aggregate_id": "12",
        "payload_json": json.dumps(
            {
                "event_id": "event-3",
                "order_id": 12,
                "user_id": 7,
                "status": "PLACED",
                "occurred_at": "2026-09-25T12:00:00Z",
            }
        ),
    }
    connection = _Connection(_InventoryCursor())
    worker = MySQLAgentWorker("mysql://worker:secret@localhost/bookstore")
    worker.pool = _Pool(connection)  # type: ignore[assignment]

    await worker._process_order_event(event)

    assert connection.commits == 1
    assert connection.rollbacks == 0
