from __future__ import annotations

import asyncio
from pathlib import Path

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from redis.asyncio import Redis

from app.config import AgentSettings
from app.models.factory import ModelRuntime
from app.tasks.graph import InventoryWatchGraph
from app.tasks.mysql_repository import TaskRepositoryRuntime
from app.tasks.worker import InventoryEventWorker
from app.tasks.trace import InventoryWatchTrace
from app.trace.manifests import VersionRegistry
from app.trace.pricing import PricingCalculator
from app.trace.recorder import TraceRecorderFactory
from app.trace.repository import TraceRepositoryRuntime
from app.trace.safety import TraceSafetyProjector


async def run() -> None:
    from app.telemetry import configure_telemetry
    from prometheus_client import start_http_server
    configure_telemetry("bookstore-agent-worker")
    start_http_server(9106)
    settings = AgentSettings()  # type: ignore[call-arg]
    redis = Redis.from_url(settings.redis_addr)
    saver = AsyncRedisSaver(redis_client=redis)
    repository = TaskRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    trace_repository = TraceRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    versions = ModelRuntime(settings).versions
    trace_factory = TraceRecorderFactory(
        trace_repository,  # type: ignore[arg-type]
        TraceSafetyProjector(
            max_json_bytes=settings.trace_max_json_bytes,
            max_final_answer_bytes=settings.trace_max_final_answer_bytes,
            max_error_bytes=settings.trace_max_error_bytes,
            max_evidence_bytes=settings.trace_max_evidence_bytes,
        ),
        versions,
        PricingCalculator(versions.pricing_manifest),
        model_profile=settings.model_profile,
        prompt_set_version=settings.prompt_set_version,
        tool_registry_version=versions.tool_registry_versions["live-v1"],
        harness_version=settings.harness_version,
        retention_days=settings.trace_retention_days,
        max_run_bytes=settings.trace_max_run_bytes,
    )
    try:
        await repository.startup()
        await trace_repository.startup()
        versions.assert_database_references(await trace_repository.manifest_references())
        await saver.asetup()
        worker = InventoryEventWorker(
            repository,
            InventoryWatchGraph(saver),
            redis,
            stream=settings.inventory_stream,
            group=settings.inventory_stream_group,
            dlq=settings.inventory_stream_dlq,
            block_ms=settings.inventory_stream_block_ms,
            batch_size=settings.inventory_stream_batch_size,
            claim_idle_ms=settings.inventory_stream_claim_idle_ms,
            max_attempts=settings.inventory_event_max_attempts,
            retention_seconds=settings.inventory_event_retention_seconds,
            expiry_scan_seconds=settings.inventory_watch_expiry_scan_seconds,
            trace=InventoryWatchTrace(trace_factory),
        )
        await worker.run()
    finally:
        await repository.aclose()
        await trace_repository.aclose()
        await redis.aclose()


def main() -> None:
    try:
        asyncio.run(run())
    except Exception:
        raise SystemExit("AGENT_WORKER_FAILED: check configuration and dependency health") from None


if __name__ == "__main__":
    main()
