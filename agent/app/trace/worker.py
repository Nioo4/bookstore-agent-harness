from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.tools.errors import ToolError
from app.trace.errors import TraceError
from app.trace.repository import TraceRepository
from app.trace.usage import persisted_steps, aggregate_usage
from app.trace.types import (
    CostStatus,
    RunStatus,
    TokenUsage,
    UsageStatus,
    TraceCost,
    TraceRun,
    TraceStatus,
)


class ReplayExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    final_answer: str
    dag: dict[str, object]
    token_usage: TokenUsage = Field(default_factory=TokenUsage.no_calls)
    usage_status: UsageStatus = UsageStatus.NO_CALLS
    trace_incomplete: bool = False
    cost: TraceCost = Field(default_factory=lambda: TraceCost(status=CostStatus.UNAVAILABLE))
    unused_fixture_count: int = Field(ge=0)


class ReplayExecutor(Protocol):
    async def execute(
        self, run: TraceRun, ensure_active: Callable[[], Awaitable[None]]
    ) -> ReplayExecutionResult: ...


class ReplayWorker:
    def __init__(
        self,
        repository: TraceRepository,
        executor: ReplayExecutor,
        *,
        owner: str | None = None,
        lease_seconds: int = 60,
        poll_seconds: float = 1,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.owner = owner or f"trace-worker-{uuid.uuid4()}"
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper
        self.log = logger or logging.getLogger(__name__)
        self._stopping = False

    async def run(self) -> None:
        while not self._stopping:
            processed = await self.run_once()
            if not processed:
                await self.sleeper(self.poll_seconds)

    def stop(self) -> None:
        self._stopping = True

    async def run_once(self) -> bool:
        now = self.clock()
        run = await self.repository.claim_replay(
            self.owner, now, now + timedelta(seconds=self.lease_seconds)
        )
        if run is None:
            return False
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(self._renew(run.run_id, lost_lease))

        async def ensure_active() -> None:
            if lost_lease.is_set() or await self.repository.is_deleted(run.run_id):
                raise TraceError("TRACE_NOT_FOUND", "replay was deleted or its lease was lost")

        try:
            await ensure_active()
            result = await self.executor.execute(run, ensure_active)
            await ensure_active()
            current = await self.repository.get_worker_run(run.run_id)
            incomplete = result.trace_incomplete or current.trace_status is TraceStatus.INCOMPLETE
            steps = await persisted_steps(self.repository, run.run_id)
            usage, usage_status = aggregate_usage(steps, coverage_complete=not incomplete,
                observed_known=result.usage_status is UsageStatus.PARTIAL)
            await self.repository.finish_run(
                run.run_id,
                status=RunStatus.COMPLETED,
                trace_status=TraceStatus.INCOMPLETE if incomplete else TraceStatus.COMPLETE,
                replayable=False,
                replay_block_reason="REPLAY_RUN",
                final_answer=result.final_answer,
                dag=result.dag,
                token_usage=usage,
                usage_status=usage_status,
                cost=TraceCost(status=CostStatus.UNAVAILABLE) if incomplete else result.cost,
                unused_fixture_count=result.unused_fixture_count,
                error_code=None,
                error_message=None,
                completed_at=self.clock(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, message = _safe_failure(exc)
            try:
                current = await self.repository.get_worker_run(run.run_id)
                if not current.status.terminal and current.deleted_at is None:
                    steps = await persisted_steps(self.repository, run.run_id)
                    usage, usage_status = aggregate_usage(steps, coverage_complete=False)
                    await self.repository.finish_run(
                        run.run_id,
                        status=RunStatus.FAILED,
                        trace_status=TraceStatus.INCOMPLETE,
                        replayable=False,
                        replay_block_reason="REPLAY_RUN",
                        final_answer=None,
                        dag=None,
                        token_usage=usage,
                        usage_status=usage_status,
                        cost=TraceCost(status=CostStatus.UNAVAILABLE),
                        unused_fixture_count=None,
                        error_code=code,
                        error_message=message,
                        completed_at=self.clock(),
                    )
            except Exception:
                self.log.exception("failed to persist replay terminal failure", extra={"run_id": run.run_id})
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True

    async def _renew(self, run_id: str, lost_lease: asyncio.Event) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await self.sleeper(interval)
            now = self.clock()
            try:
                renewed = await self.repository.renew_replay_lease(
                    run_id,
                    self.owner,
                    now,
                    now + timedelta(seconds=self.lease_seconds),
                )
            except Exception:
                renewed = False
            if not renewed:
                lost_lease.set()
                return


class TraceCleanupLoop:
    def __init__(
        self,
        repository: TraceRepository,
        delete_checkpoint: Callable[[str], Awaitable[None]],
        *,
        interval_seconds: int = 3600,
        grace_seconds: int = 86400,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.repository = repository
        self.delete_checkpoint = delete_checkpoint
        self.interval_seconds = interval_seconds
        self.grace_seconds = grace_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper
        self.log = logger or logging.getLogger(__name__)
        self._stopping = False

    async def run(self) -> None:
        while not self._stopping:
            await self.run_once()
            await self.sleeper(self.interval_seconds)

    def stop(self) -> None:
        self._stopping = True

    async def run_once(self) -> tuple[list[str], list[str]]:
        now = self.clock()
        soft, hard = await self.repository.purge_expired(
            now, now - timedelta(seconds=self.grace_seconds)
        )
        for run_id in hard:
            try:
                await self.delete_checkpoint(run_id)
                await self.repository.hard_delete(run_id, now - timedelta(seconds=self.grace_seconds))
            except Exception:
                self.log.exception(
                    "failed to purge expired trace state", extra={"run_id": run_id}
                )
        return soft, hard


def _safe_failure(exc: Exception) -> tuple[str, str]:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ToolError):
            return current.code[:64], "fixed replay execution failed"
        if isinstance(current, TraceError):
            return current.code[:64], "replay trace dependency failed"
        current = current.__cause__
    return "REPLAY_EXECUTION_FAILED", "fixed replay execution failed"
