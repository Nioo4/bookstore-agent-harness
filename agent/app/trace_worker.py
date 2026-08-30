from __future__ import annotations

import asyncio
from pathlib import Path

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from redis.asyncio import Redis

from app.agents.registry import build_agent_registry
from app.config import AgentSettings
from app.models.factory import ModelRuntime
from app.evaluation.repository import AsyncMySQLEvaluationRepository, EvaluationCleanupLoop
from app.harness.checkpoint import RedisCheckpointStore
from app.harness.plan import PlanLimits
from app.models.openai_compatible import OpenAICompatibleProvider
from app.tools.business import build_business_replay_definitions
from app.trace.execution import ReplayChatExecutor
from app.trace.manifests import VersionRegistry
from app.trace.recorder import TracingModelProvider
from app.trace.repository import TraceRepositoryRuntime
from app.trace.safety import TraceSafetyProjector
from app.trace.worker import ReplayWorker, TraceCleanupLoop
from app.tasks.mysql_repository import TaskRepositoryRuntime


async def run() -> None:
    from app.telemetry import configure_telemetry
    from prometheus_client import start_http_server
    configure_telemetry("bookstore-trace-worker")
    start_http_server(9107)
    settings = AgentSettings()  # type: ignore[call-arg]
    runtime = ModelRuntime(settings)
    versions = runtime.versions
    redis = Redis.from_url(settings.redis_addr)
    saver = AsyncRedisSaver(redis_client=redis)
    repository = TraceRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    task_repository = TaskRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    provider = runtime.chat()
    evaluation_repository = None
    limits = PlanLimits(
        max_nodes=settings.agent_max_nodes,
        max_depth=settings.agent_max_depth,
        max_parallel=settings.agent_max_parallel,
        max_tool_calls=settings.agent_max_tool_calls,
    )
    try:
        await repository.startup()
        await task_repository.startup()
        evaluation_repository = await AsyncMySQLEvaluationRepository.create_pool(
            settings.agent_mysql_dsn.get_secret_value()
        )
        versions.assert_database_references(await repository.manifest_references())
        await saver.asetup()
        checkpoint_store = RedisCheckpointStore(redis)
        executor = ReplayChatExecutor(
            repository,  # type: ignore[arg-type]
            TracingModelProvider(provider),
            build_agent_registry(),
            build_business_replay_definitions(),
            checkpoint_store,
            saver,
            limits,
            versions,
            TraceSafetyProjector(
                max_json_bytes=settings.trace_max_json_bytes,
                max_final_answer_bytes=settings.trace_max_final_answer_bytes,
                max_error_bytes=settings.trace_max_error_bytes,
                max_evidence_bytes=settings.trace_max_evidence_bytes,
            ),
        )
        worker = ReplayWorker(
            repository,  # type: ignore[arg-type]
            executor,
            lease_seconds=settings.replay_lease_ttl_seconds,
        )
        async def delete_run_state(run_id: str) -> None:
            task_id = await task_repository.checkpoint_thread_id_for_run(run_id)
            for thread_id in {run_id, task_id} - {None}:
                await saver.adelete_thread(thread_id)
                await checkpoint_store.delete_run(thread_id)
            await redis.delete(f"agent:trace:{run_id}:events")

        cleanup = TraceCleanupLoop(
            repository,  # type: ignore[arg-type]
            delete_run_state,
            interval_seconds=settings.trace_purge_interval_seconds,
            grace_seconds=settings.trace_purge_grace_seconds,
        )
        evaluation_cleanup = EvaluationCleanupLoop(
            evaluation_repository,
            interval_seconds=settings.trace_purge_interval_seconds,
        )
        await asyncio.gather(worker.run(), cleanup.run(), evaluation_cleanup.run())
    finally:
        await repository.aclose()
        await task_repository.aclose()
        if evaluation_repository is not None:
            await evaluation_repository.aclose()
        await provider.aclose()
        await redis.aclose()


def main() -> None:
    try:
        asyncio.run(run())
    except Exception:
        raise SystemExit("TRACE_WORKER_FAILED: check configuration and dependency health") from None


if __name__ == "__main__":
    main()
