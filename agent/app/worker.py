from __future__ import annotations

"""v2 worker entry point: MySQL Outbox leases plus legacy vector deletion."""

import asyncio

from app.config import WorkerSettings
from app.memory.legacy_delete import LegacyMemoryDeletionStore
from app.tasks.v2_worker import MySQLAgentWorker


async def run() -> None:
    from app.telemetry import configure_telemetry

    configure_telemetry("bookstore-agent-worker")
    settings = WorkerSettings()  # type: ignore[call-arg]
    memory_store = LegacyMemoryDeletionStore(settings.qdrant_url, settings.legacy_memory_collection)
    worker = MySQLAgentWorker(
        settings.worker_mysql_dsn_value,
        qdrant_memory=memory_store,
        poll_interval=settings.outbox_poll_interval_seconds,
        batch_size=settings.outbox_batch_size,
        lease_seconds=int(settings.outbox_lease_ttl_seconds),
        max_attempts=settings.outbox_max_attempts,
        retention_scan_interval=settings.retention_scan_interval_seconds,
    )
    try:
        await worker.run_forever()
    finally:
        await worker.aclose()
        await memory_store.aclose()


def _seconds(value: str) -> float:
    value = value.strip().casefold()
    if value.endswith("ms"):
        return float(value[:-2]) / 1000
    if value.endswith("s"):
        return float(value[:-1])
    if value.endswith("m"):
        return float(value[:-1]) * 60
    if value.endswith("h"):
        return float(value[:-1]) * 3600
    raise ValueError("worker duration must use ms, s, m, or h")


def main() -> None:
    try:
        asyncio.run(run())
    except Exception:
        raise SystemExit("AGENT_WORKER_FAILED: check WORKER_MYSQL_DSN and database health") from None


if __name__ == "__main__":
    main()
