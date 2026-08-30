from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncmy
from asyncmy.cursors import DictCursor

from app.memory.repository import _parse_mysql_url
from app.trace.errors import TraceDependencyUnavailable, TraceError
from app.trace.schema import require_usage_schema
from app.trace.usage import aggregate_usage
from app.trace.types import (
    CostStatus,
    EvidenceDraft,
    FixtureDraft,
    FixtureOutcome,
    ReplayMode,
    RunKind,
    RunStatus,
    StepDraft,
    StepStatus,
    StepType,
    TokenUsage,
    UsageStatus,
    validate_usage,
    ToolFixture,
    TraceCost,
    TraceEvidence,
    TraceRun,
    TraceStatus,
    TraceStep,
    WorkflowType,
    ManifestReference,
    canonical_json,
    utc,
)


class TraceRepository(Protocol):
    async def create_run(self, run: TraceRun) -> TraceRun: ...

    async def create_replay(self, run: TraceRun) -> tuple[TraceRun, bool]: ...

    async def get_owned(self, user_id: int, run_id: str) -> TraceRun: ...

    async def get_worker_run(self, run_id: str) -> TraceRun: ...

    async def list_owned(
        self,
        user_id: int,
        *,
        conversation_id: str | None,
        kind: RunKind | None,
        status: RunStatus | None,
        before: tuple[datetime, str] | None,
        limit: int,
    ) -> list[TraceRun]: ...

    async def list_replays(
        self, user_id: int, source_run_id: str, before: tuple[datetime, str] | None, limit: int
    ) -> list[TraceRun]: ...

    async def list_steps(self, run_id: str, after_sequence: int | None, limit: int) -> list[TraceStep]: ...

    async def list_evidence(self, run_id: str) -> list[TraceEvidence]: ...

    async def replay_count(self, run_id: str) -> int: ...

    async def append_step(self, draft: StepDraft) -> TraceStep: ...

    async def finish_step(
        self,
        run_id: str,
        step_id: str,
        *,
        status: StepStatus,
        output: dict[str, Any] | None,
        evidence_ids: Sequence[str],
        token_usage: TokenUsage,
        usage_status: UsageStatus = UsageStatus.NO_CALLS,
        cost: TraceCost,
        error_code: str | None,
        error_message: str | None,
        completed_at: datetime,
    ) -> TraceStep: ...

    async def insert_evidence(self, draft: EvidenceDraft) -> TraceEvidence: ...

    async def insert_fixture(self, draft: FixtureDraft) -> ToolFixture: ...

    async def list_fixtures(self, source_run_id: str) -> list[ToolFixture]: ...

    async def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        trace_status: TraceStatus,
        replayable: bool,
        replay_block_reason: str | None,
        final_answer: str | None,
        dag: dict[str, Any] | None,
        token_usage: TokenUsage,
        usage_status: UsageStatus = UsageStatus.NO_CALLS,
        cost: TraceCost,
        unused_fixture_count: int | None,
        error_code: str | None,
        error_message: str | None,
        completed_at: datetime,
    ) -> TraceRun: ...

    async def pause_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        trace_status: TraceStatus,
        replay_block_reason: str,
        final_answer: str | None,
        dag: dict[str, Any] | None,
        token_usage: TokenUsage,
        usage_status: UsageStatus = UsageStatus.NO_CALLS,
        cost: TraceCost,
        updated_at: datetime,
    ) -> TraceRun: ...

    async def mark_incomplete(self, run_id: str, reason: str) -> None: ...

    async def mark_unreplayable(self, run_id: str, reason: str) -> None: ...

    async def soft_delete(self, user_id: int, run_id: str, now: datetime) -> tuple[int, int]: ...

    async def claim_replay(self, owner: str, now: datetime, lease_expires_at: datetime) -> TraceRun | None: ...

    async def renew_replay_lease(
        self, run_id: str, owner: str, now: datetime, lease_expires_at: datetime
    ) -> bool: ...

    async def is_deleted(self, run_id: str) -> bool: ...

    async def trace_bytes(self, run_id: str) -> int: ...

    async def purge_expired(self, now: datetime, hard_before: datetime) -> tuple[list[str], list[str]]: ...

    async def hard_delete(self, run_id: str, hard_before: datetime) -> bool: ...

    async def manifest_references(self) -> list[ManifestReference]: ...


class InMemoryTraceRepository:
    def __init__(self) -> None:
        self.runs: dict[str, TraceRun] = {}
        self.steps: dict[str, list[TraceStep]] = {}
        self.evidence: dict[str, list[TraceEvidence]] = {}
        self.fixtures: dict[str, list[ToolFixture]] = {}
        self._lock = asyncio.Lock()

    async def create_run(self, run: TraceRun) -> TraceRun:
        _validate_new_run(run)
        async with self._lock:
            if run.run_id in self.runs:
                raise TraceDependencyUnavailable("trace run already exists")
            self.runs[run.run_id] = run
            return run

    async def manifest_references(self) -> list[ManifestReference]:
        unique: dict[tuple[object, ...], ManifestReference] = {}
        for run in self.runs.values():
            reference = ManifestReference(
                model_profile=run.model_profile,
                model_manifest_hash=run.model_manifest_hash,
                prompt_set_version=run.prompt_set_version,
                prompt_manifest_hash=run.prompt_manifest_hash,
                tool_registry_version=run.tool_registry_version,
                pricing_version=run.cost.pricing_version,
            )
            key = (
                reference.model_profile,
                reference.model_manifest_hash,
                reference.prompt_set_version,
                reference.prompt_manifest_hash,
                reference.tool_registry_version,
                reference.pricing_version,
            )
            unique[key] = reference
        return list(unique.values())

    async def create_replay(self, run: TraceRun) -> tuple[TraceRun, bool]:
        async with self._lock:
            for current in self.runs.values():
                if current.user_id == run.user_id and current.request_idempotency_key == run.request_idempotency_key:
                    if current.request_hash != run.request_hash:
                        raise TraceError("IDEMPOTENCY_CONFLICT", "idempotency key conflicts with another request")
                    return current, True
            source = self.runs.get(run.source_run_id or "")
            _validate_replay_source(source, run.user_id)
            if any(
                item.user_id == run.user_id
                and item.run_kind is RunKind.REPLAY
                and item.status in {RunStatus.QUEUED, RunStatus.RUNNING}
                and item.deleted_at is None
                for item in self.runs.values()
            ):
                raise TraceError("REPLAY_ALREADY_RUNNING", "another replay is already running")
            self.runs[run.run_id] = run
            return run, False

    async def get_owned(self, user_id: int, run_id: str) -> TraceRun:
        run = self.runs.get(run_id)
        if run is None or run.user_id != user_id or run.deleted_at is not None or run.run_kind is RunKind.EVALUATION:
            raise TraceError("TRACE_NOT_FOUND", "trace not found")
        return run

    async def get_worker_run(self, run_id: str) -> TraceRun:
        run = self.runs.get(run_id)
        if run is None:
            raise TraceError("TRACE_NOT_FOUND", "trace not found")
        return run

    async def list_owned(
        self,
        user_id: int,
        *,
        conversation_id: str | None,
        kind: RunKind | None,
        status: RunStatus | None,
        before: tuple[datetime, str] | None,
        limit: int,
    ) -> list[TraceRun]:
        values = [
            run
            for run in self.runs.values()
            if run.user_id == user_id
            and run.deleted_at is None
            and run.run_kind is not RunKind.EVALUATION
            and (conversation_id is None or run.conversation_id == conversation_id)
            and (kind is None or run.run_kind is kind)
            and (status is None or run.status is status)
            and (before is None or (run.created_at, run.run_id) < before)
        ]
        return sorted(values, key=lambda item: (item.created_at, item.run_id), reverse=True)[:limit]

    async def list_replays(
        self, user_id: int, source_run_id: str, before: tuple[datetime, str] | None, limit: int
    ) -> list[TraceRun]:
        await self.get_owned(user_id, source_run_id)
        values = [
            run
            for run in self.runs.values()
            if run.user_id == user_id
            and run.source_run_id == source_run_id
            and run.deleted_at is None
            and (before is None or (run.created_at, run.run_id) < before)
        ]
        return sorted(values, key=lambda item: (item.created_at, item.run_id), reverse=True)[:limit]

    async def list_steps(self, run_id: str, after_sequence: int | None, limit: int) -> list[TraceStep]:
        return [
            step
            for step in self.steps.get(run_id, [])
            if after_sequence is None or step.sequence_no > after_sequence
        ][:limit]

    async def list_evidence(self, run_id: str) -> list[TraceEvidence]:
        return list(self.evidence.get(run_id, []))

    async def replay_count(self, run_id: str) -> int:
        return sum(1 for run in self.runs.values() if run.source_run_id == run_id and run.deleted_at is None)

    async def append_step(self, draft: StepDraft) -> TraceStep:
        if draft.usage_status is UsageStatus.LEGACY_UNKNOWN:
            raise ValueError("new steps cannot use legacy usage status")
        async with self._lock:
            run = self.runs.get(draft.run_id)
            if run is None or run.status.terminal:
                raise TraceDependencyUnavailable("trace run is not writable")
            current = self.steps.setdefault(draft.run_id, [])
            if draft.parent_step_id is not None and not any(
                item.step_id == draft.parent_step_id for item in current
            ):
                raise TraceDependencyUnavailable("trace parent step does not exist in the run")
            step = TraceStep(
                **draft.model_dump(),
                sequence_no=len(current) + 1,
                status=StepStatus.RUNNING,
                evidence_ids=[],
                cost=TraceCost(status=CostStatus.UNAVAILABLE),
                created_at=draft.started_at,
            )
            current.append(step)
            return step

    async def finish_step(
        self,
        run_id: str,
        step_id: str,
        *,
        status: StepStatus,
        output: dict[str, Any] | None,
        evidence_ids: Sequence[str],
        token_usage: TokenUsage,
        usage_status: UsageStatus = UsageStatus.NO_CALLS,
        cost: TraceCost,
        error_code: str | None,
        error_message: str | None,
        completed_at: datetime,
    ) -> TraceStep:
        async with self._lock:
            values = self.steps.get(run_id, [])
            for index, step in enumerate(values):
                if step.step_id == step_id:
                    if step.status is not StepStatus.RUNNING:
                        return step
                    duration = max(0, int((completed_at - step.started_at).total_seconds() * 1000))
                    completed = TraceStep.model_validate({
                        **step.model_dump(), **{
                            "status": status,
                            "output": output,
                            "evidence_ids": list(evidence_ids),
                            **_usage_values(step, token_usage, usage_status, cost),
                            "error_code": error_code,
                            "error_message": error_message,
                            "completed_at": completed_at,
                            "duration_ms": duration,
                        }
                    })
                    values[index] = completed
                    return completed
            raise TraceDependencyUnavailable("trace step does not exist")

    async def insert_evidence(self, draft: EvidenceDraft) -> TraceEvidence:
        item = TraceEvidence(**draft.model_dump())
        async with self._lock:
            values = self.evidence.setdefault(draft.run_id, [])
            if not any(existing.evidence_id == item.evidence_id for existing in values):
                values.append(item)
            return next(existing for existing in values if existing.evidence_id == item.evidence_id)

    async def insert_fixture(self, draft: FixtureDraft) -> ToolFixture:
        item = ToolFixture(**draft.model_dump())
        async with self._lock:
            values = self.fixtures.setdefault(draft.source_run_id, [])
            for existing in values:
                if (
                    existing.tool_name,
                    existing.canonical_args_hash,
                    existing.occurrence_index,
                ) == (item.tool_name, item.canonical_args_hash, item.occurrence_index):
                    return existing
            values.append(item)
            return item

    async def list_fixtures(self, source_run_id: str) -> list[ToolFixture]:
        return list(self.fixtures.get(source_run_id, []))

    async def finish_run(self, run_id: str, **values: Any) -> TraceRun:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                raise TraceDependencyUnavailable("trace run does not exist")
            if run.status.terminal:
                return run
            steps = self.steps.get(run_id, [])
            _validate_snapshot(run, steps, values)
            started = run.started_at or run.created_at
            completed_at = values["completed_at"]
            values["wall_duration_ms"] = max(0, int((completed_at - started).total_seconds() * 1000))
            values["active_duration_ms"] = active_duration_ms(steps)
            values["retry_count"] = sum(max(step.attempt_no - 1, 0) for step in steps)
            values["lease_owner"] = None
            values["lease_expires_at"] = None
            values["updated_at"] = completed_at
            values.update(_usage_values(run, values["token_usage"], values.get("usage_status", UsageStatus.NO_CALLS), values["cost"]))
            completed = TraceRun.model_validate({**run.model_dump(), **values})
            self.runs[run_id] = completed
            return completed

    async def pause_run(self, run_id: str, **values: Any) -> TraceRun:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None or run.status.terminal:
                raise TraceDependencyUnavailable("trace run is not pausable")
            _validate_snapshot(run, self.steps.get(run_id, []), values)
            values.update(_usage_values(run, values["token_usage"], values.get("usage_status", UsageStatus.NO_CALLS), values["cost"]))
            paused = TraceRun.model_validate({
                **run.model_dump(), **{
                    **values,
                    "replayable": False,
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
            })
            self.runs[run_id] = paused
            return paused

    async def mark_incomplete(self, run_id: str, reason: str) -> None:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is not None and not run.status.terminal:
                usage, usage_status = aggregate_usage(self.steps.get(run_id, []), coverage_complete=False,
                    observed_known=run.usage_status in {UsageStatus.COMPLETE, UsageStatus.PARTIAL})
                self.runs[run_id] = run.model_copy(
                    update={
                        **_usage_values(run, usage, usage_status, TraceCost(status=CostStatus.UNAVAILABLE)),
                        "trace_status": TraceStatus.INCOMPLETE,
                        "replayable": False,
                        "replay_block_reason": reason[:64],
                    }
                )

    async def mark_unreplayable(self, run_id: str, reason: str) -> None:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is not None and not run.status.terminal:
                self.runs[run_id] = run.model_copy(
                    update={
                        "replayable": False,
                        "replay_block_reason": reason[:64],
                    }
                )

    async def soft_delete(self, user_id: int, run_id: str, now: datetime) -> tuple[int, int]:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None or run.user_id != user_id or run.deleted_at is not None:
                raise TraceError("TRACE_NOT_FOUND", "trace not found")
            if run.run_kind is RunKind.LIVE and not run.status.terminal:
                raise TraceError("TRACE_NOT_DELETABLE", "a non-terminal live trace cannot be deleted")
            targets = [run]
            if run.run_kind is RunKind.LIVE:
                targets.extend(item for item in self.runs.values() if item.source_run_id == run_id and item.deleted_at is None)
            for item in targets:
                updates: dict[str, Any] = {"deleted_at": now, "updated_at": now}
                if item.run_kind is RunKind.REPLAY and item.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                    updates.update(
                        status=RunStatus.CANCELLED,
                        completed_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                    )
                self.runs[item.run_id] = item.model_copy(update=updates)
            return len(targets), len(targets)

    async def claim_replay(self, owner: str, now: datetime, lease_expires_at: datetime) -> TraceRun | None:
        async with self._lock:
            candidates = sorted(
                (
                    run
                    for run in self.runs.values()
                    if run.run_kind is RunKind.REPLAY
                    and run.deleted_at is None
                    and (
                        run.status is RunStatus.QUEUED
                        or (
                            run.status is RunStatus.RUNNING
                            and run.lease_expires_at is not None
                            and run.lease_expires_at <= now
                        )
                    )
                ),
                key=lambda item: (item.created_at, item.run_id),
            )
            if not candidates:
                return None
            run = candidates[0]
            claimed = run.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "lease_owner": owner,
                    "lease_expires_at": lease_expires_at,
                    "started_at": run.started_at or now,
                    "updated_at": now,
                    **({"trace_status": TraceStatus.INCOMPLETE, "replayable": False,
                        "replay_block_reason": "RECOVERY_MODEL_COVERAGE_UNKNOWN"}
                       if run.status is RunStatus.RUNNING else {}),
                }
            )
            self.runs[run.run_id] = claimed
            return claimed

    async def renew_replay_lease(
        self, run_id: str, owner: str, now: datetime, lease_expires_at: datetime
    ) -> bool:
        async with self._lock:
            run = self.runs.get(run_id)
            if (
                run is None
                or run.status is not RunStatus.RUNNING
                or run.lease_owner != owner
                or run.deleted_at is not None
            ):
                return False
            self.runs[run_id] = run.model_copy(
                update={"lease_expires_at": lease_expires_at, "updated_at": now}
            )
            return True

    async def is_deleted(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        return run is None or run.deleted_at is not None or run.status is RunStatus.CANCELLED

    async def trace_bytes(self, run_id: str) -> int:
        run = self.runs.get(run_id)
        if run is None:
            return 0
        values: list[Any] = [run.input, run.dag, run.final_answer]
        values.extend((step.input, step.output) for step in self.steps.get(run_id, []))
        values.extend(item.payload for item in self.evidence.get(run_id, []))
        values.extend((item.request, item.response) for item in self.fixtures.get(run_id, []))
        return len(canonical_json(values).encode("utf-8"))

    async def purge_expired(self, now: datetime, hard_before: datetime) -> tuple[list[str], list[str]]:
        async with self._lock:
            soft: list[str] = []
            hard: list[str] = []
            for run_id, run in self.runs.items():
                if run.deleted_at is None and run.retention_expires_at <= now:
                    self.runs[run_id] = run.model_copy(update={"deleted_at": now, "updated_at": now})
                    soft.append(run_id)
                elif run.deleted_at is not None and run.deleted_at <= hard_before:
                    blocking_child = any(
                        item.source_run_id == run_id
                        and (item.deleted_at is None or item.deleted_at > hard_before)
                        for item in self.runs.values()
                        if item.run_id != run_id
                    )
                    if blocking_child:
                        continue
                    hard.append(run_id)
            hard.sort(key=lambda item: self.runs[item].source_run_id is None)
            return soft, hard

    async def hard_delete(self, run_id: str, hard_before: datetime) -> bool:
        async with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                return True
            if run.deleted_at is None or run.deleted_at > hard_before:
                return False
            if any(item.source_run_id == run_id for item in self.runs.values() if item.run_id != run_id):
                return False
            del self.runs[run_id]
            self.steps.pop(run_id, None)
            self.evidence.pop(run_id, None)
            self.fixtures.pop(run_id, None)
            return True


class AsyncMySQLTraceRepository:
    def __init__(self, pool: asyncmy.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create_pool(cls, dsn: str) -> "AsyncMySQLTraceRepository":
        try:
            pool = await asyncmy.create_pool(
                minsize=1, maxsize=10, autocommit=False, **_parse_mysql_url(dsn)
            )
            try:
                await require_usage_schema(pool)
            except BaseException:
                pool.close()
                await pool.wait_closed()
                raise
            return cls(pool)
        except Exception as exc:
            raise TraceDependencyUnavailable("trace database connection failed") from exc

    async def aclose(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()

    async def create_run(self, run: TraceRun) -> TraceRun:
        _validate_new_run(run)
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(_INSERT_RUN, _run_insert_args(run))
                await connection.commit()
            return run
        except Exception as exc:
            raise TraceDependencyUnavailable("trace run create failed") from exc

    async def manifest_references(self) -> list[ManifestReference]:
        rows = await self._query(
            "SELECT DISTINCT model_profile,model_manifest_hash,prompt_set_version,prompt_manifest_hash,"
            "tool_registry_version,pricing_version "
            "FROM agent_run",
            (),
        )
        return [
            ManifestReference(
                model_profile=str(row["model_profile"]),
                model_manifest_hash=bytes(row["model_manifest_hash"]),
                prompt_set_version=str(row["prompt_set_version"]),
                prompt_manifest_hash=bytes(row["prompt_manifest_hash"]),
                tool_registry_version=str(row["tool_registry_version"]),
                pricing_version=(str(row["pricing_version"]) if row["pricing_version"] is not None else None),
            )
            for row in rows
        ]

    async def create_replay(self, run: TraceRun) -> tuple[TraceRun, bool]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM agent_run WHERE user_id=%s AND request_idempotency_key=%s "
                            "LIMIT 1 FOR UPDATE",
                            (run.user_id, run.request_idempotency_key),
                        )
                        replay = await cursor.fetchone()
                        if replay is not None:
                            existing = _run(replay)
                            if existing.request_hash != run.request_hash:
                                raise TraceError("IDEMPOTENCY_CONFLICT", "idempotency key conflicts with another request")
                            await connection.commit()
                            return existing, True
                        await cursor.execute(
                            "SELECT * FROM agent_run WHERE run_id=%s AND user_id=%s AND deleted_at IS NULL "
                            "LIMIT 1 FOR UPDATE",
                            (run.source_run_id, run.user_id),
                        )
                        source_row = await cursor.fetchone()
                        source = _run(source_row) if source_row is not None else None
                        _validate_replay_source(source, run.user_id)
                        await cursor.execute(
                            "SELECT run_id FROM agent_run WHERE user_id=%s AND run_kind='REPLAY' "
                            "AND status IN ('QUEUED','RUNNING') AND deleted_at IS NULL LIMIT 1 FOR UPDATE",
                            (run.user_id,),
                        )
                        if await cursor.fetchone() is not None:
                            raise TraceError("REPLAY_ALREADY_RUNNING", "another replay is already running")
                        await cursor.execute(_INSERT_RUN, _run_insert_args(run))
                    await connection.commit()
                    return run, False
                except Exception:
                    await connection.rollback()
                    raise
        except TraceError:
            raise
        except asyncmy.errors.IntegrityError as exc:
            rows = await self._query(
                "SELECT * FROM agent_run WHERE user_id=%s AND request_idempotency_key=%s LIMIT 1",
                (run.user_id, run.request_idempotency_key),
            )
            if rows:
                existing = _run(rows[0])
                if existing.request_hash != run.request_hash:
                    raise TraceError("IDEMPOTENCY_CONFLICT", "idempotency key conflicts with another request") from exc
                return existing, True
            raise TraceDependencyUnavailable("trace replay create conflict") from exc
        except Exception as exc:
            raise TraceDependencyUnavailable("trace replay create failed") from exc

    async def get_owned(self, user_id: int, run_id: str) -> TraceRun:
        rows = await self._query(
            "SELECT * FROM agent_run WHERE run_id=%s AND user_id=%s AND deleted_at IS NULL "
            "AND run_kind IN ('LIVE','REPLAY') LIMIT 1",
            (run_id, user_id),
        )
        if not rows:
            raise TraceError("TRACE_NOT_FOUND", "trace not found")
        return _run(rows[0])

    async def get_worker_run(self, run_id: str) -> TraceRun:
        rows = await self._query("SELECT * FROM agent_run WHERE run_id=%s LIMIT 1", (run_id,))
        if not rows:
            raise TraceError("TRACE_NOT_FOUND", "trace not found")
        return _run(rows[0])

    async def list_owned(
        self,
        user_id: int,
        *,
        conversation_id: str | None,
        kind: RunKind | None,
        status: RunStatus | None,
        before: tuple[datetime, str] | None,
        limit: int,
    ) -> list[TraceRun]:
        query = "SELECT * FROM agent_run WHERE user_id=%s AND deleted_at IS NULL AND run_kind IN ('LIVE','REPLAY')"
        args: list[Any] = [user_id]
        if conversation_id is not None:
            query += " AND conversation_id=%s"
            args.append(conversation_id)
        if kind is not None:
            query += " AND run_kind=%s"
            args.append(kind.value)
        if status is not None:
            query += " AND status=%s"
            args.append(status.value)
        if before is not None:
            query += " AND (created_at<%s OR (created_at=%s AND run_id<%s))"
            args.extend([before[0], before[0], before[1]])
        query += " ORDER BY created_at DESC,run_id DESC LIMIT %s"
        args.append(limit)
        return [_run(row) for row in await self._query(query, tuple(args))]

    async def list_replays(
        self, user_id: int, source_run_id: str, before: tuple[datetime, str] | None, limit: int
    ) -> list[TraceRun]:
        await self.get_owned(user_id, source_run_id)
        query = "SELECT * FROM agent_run WHERE source_run_id=%s AND user_id=%s AND deleted_at IS NULL"
        args: list[Any] = [source_run_id, user_id]
        if before is not None:
            query += " AND (created_at<%s OR (created_at=%s AND run_id<%s))"
            args.extend([before[0], before[0], before[1]])
        query += " ORDER BY created_at DESC,run_id DESC LIMIT %s"
        args.append(limit)
        return [_run(row) for row in await self._query(query, tuple(args))]

    async def list_steps(self, run_id: str, after_sequence: int | None, limit: int) -> list[TraceStep]:
        query = "SELECT * FROM agent_step WHERE run_id=%s"
        args: list[Any] = [run_id]
        if after_sequence is not None:
            query += " AND sequence_no>%s"
            args.append(after_sequence)
        query += " ORDER BY sequence_no ASC LIMIT %s"
        args.append(limit)
        return [_step(row) for row in await self._query(query, tuple(args))]

    async def list_evidence(self, run_id: str) -> list[TraceEvidence]:
        return [
            _evidence(row)
            for row in await self._query(
                "SELECT * FROM agent_evidence WHERE run_id=%s ORDER BY created_at,evidence_id", (run_id,)
            )
        ]

    async def replay_count(self, run_id: str) -> int:
        rows = await self._query(
            "SELECT COUNT(*) AS total FROM agent_run WHERE source_run_id=%s AND deleted_at IS NULL", (run_id,)
        )
        return int(rows[0]["total"])

    async def append_step(self, draft: StepDraft) -> TraceStep:
        if draft.usage_status is UsageStatus.LEGACY_UNKNOWN:
            raise ValueError("new steps cannot use legacy usage status")
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT status FROM agent_run WHERE run_id=%s LIMIT 1 FOR UPDATE", (draft.run_id,)
                        )
                        run = await cursor.fetchone()
                        if run is None or str(run["status"]) in {"COMPLETED", "FAILED", "CANCELLED"}:
                            raise TraceDependencyUnavailable("trace run is not writable")
                        await cursor.execute(
                            "SELECT COALESCE(MAX(sequence_no),0)+1 AS next_sequence FROM agent_step WHERE run_id=%s",
                            (draft.run_id,),
                        )
                        sequence = int((await cursor.fetchone())["next_sequence"])
                        await cursor.execute(
                            "INSERT INTO agent_step "
                            "(step_id,run_id,parent_step_id,sequence_no,node_id,step_type,agent_name,tool_name,status,"
                            "attempt_no,input_json,evidence_ids_json,model_profile,prompt_version,tool_version,cost_status,"
                            "started_at,created_at,prompt_tokens,completion_tokens,cached_tokens,reasoning_tokens,usage_status) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING',%s,%s,JSON_ARRAY(),%s,%s,%s,"
                            "'UNAVAILABLE',%s,%s,%s,%s,%s,%s,%s)",
                            (
                                draft.step_id,
                                draft.run_id,
                                draft.parent_step_id,
                                sequence,
                                draft.node_id,
                                draft.step_type.value,
                                draft.agent_name,
                                draft.tool_name,
                                draft.attempt_no,
                                _json(draft.input) if draft.input is not None else None,
                                draft.model_profile,
                                draft.prompt_version,
                                draft.tool_version,
                                draft.started_at,
                                draft.started_at,
                                draft.token_usage.prompt, draft.token_usage.completion,
                                draft.token_usage.cached, draft.token_usage.reasoning, draft.usage_status.value,
                            ),
                        )
                        await cursor.execute("SELECT * FROM agent_step WHERE step_id=%s", (draft.step_id,))
                        inserted = await cursor.fetchone()
                    await connection.commit()
                    return _step(inserted)
                except Exception:
                    await connection.rollback()
                    raise
        except TraceError:
            raise
        except Exception as exc:
            raise TraceDependencyUnavailable("trace step create failed") from exc

    async def finish_step(
        self,
        run_id: str,
        step_id: str,
        *,
        status: StepStatus,
        output: dict[str, Any] | None,
        evidence_ids: Sequence[str],
        token_usage: TokenUsage,
        usage_status: UsageStatus = UsageStatus.NO_CALLS,
        cost: TraceCost,
        error_code: str | None,
        error_message: str | None,
        completed_at: datetime,
    ) -> TraceStep:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute("SELECT * FROM agent_step WHERE run_id=%s AND step_id=%s", (run_id, step_id))
                    prior_row = await cursor.fetchone()
                    if prior_row is None:
                        raise TraceDependencyUnavailable("trace step does not exist")
                    prior = _step(prior_row)
                    if prior.status is not StepStatus.RUNNING:
                        return prior
                    usage_values = _usage_values(prior, token_usage, usage_status, cost)
                    checked = TraceStep.model_validate({**prior.model_dump(), **usage_values})
                    token_usage, usage_status, cost = checked.token_usage, checked.usage_status, checked.cost
                    await cursor.execute(
                        "UPDATE agent_step SET status=%s,output_json=%s,evidence_ids_json=%s,prompt_tokens=%s,"
                        "completion_tokens=%s,cached_tokens=%s,reasoning_tokens=%s,usage_status=%s,known_cost_microusd=%s,cost_status=%s,error_code=%s,error_message=%s,"
                        "completed_at=%s,duration_ms=TIMESTAMPDIFF(MICROSECOND,started_at,%s) DIV 1000 "
                        "WHERE run_id=%s AND step_id=%s AND status='RUNNING'",
                        (
                            status.value,
                            _json(output) if output is not None else None,
                            _json(list(evidence_ids)),
                            token_usage.prompt,
                            token_usage.completion,
                            token_usage.cached, token_usage.reasoning, usage_status.value,
                            cost.known_microusd,
                            cost.status.value,
                            error_code,
                            error_message,
                            completed_at,
                            completed_at,
                            run_id,
                            step_id,
                        ),
                    )
                    await cursor.execute(
                        "SELECT * FROM agent_step WHERE run_id=%s AND step_id=%s", (run_id, step_id)
                    )
                    row = await cursor.fetchone()
                await connection.commit()
            if row is None:
                raise TraceDependencyUnavailable("trace step does not exist")
            return _step(row)
        except TraceError:
            raise
        except Exception as exc:
            raise TraceDependencyUnavailable("trace step completion failed") from exc

    async def insert_evidence(self, draft: EvidenceDraft) -> TraceEvidence:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "INSERT INTO agent_evidence "
                        "(run_id,evidence_id,source_step_id,evidence_type,source_ref,payload_json,content_hash,created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE evidence_id=evidence_id",
                        (
                            draft.run_id,
                            draft.evidence_id,
                            draft.source_step_id,
                            draft.evidence_type,
                            draft.source_ref,
                            _json(draft.payload),
                            draft.content_hash,
                            draft.created_at,
                        ),
                    )
                    await cursor.execute(
                        "SELECT * FROM agent_evidence WHERE run_id=%s AND evidence_id=%s",
                        (draft.run_id, draft.evidence_id),
                    )
                    row = await cursor.fetchone()
                await connection.commit()
            return _evidence(row)
        except Exception as exc:
            raise TraceDependencyUnavailable("trace evidence write failed") from exc

    async def insert_fixture(self, draft: FixtureDraft) -> ToolFixture:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "INSERT INTO agent_tool_fixture "
                        "(fixture_id,source_run_id,source_step_id,tool_name,risk_level,canonical_args_hash,"
                        "occurrence_index,request_json,response_json,outcome,error_code,schema_version,replayable,"
                        "blocked_reason,content_hash,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE fixture_id=fixture_id",
                        (
                            draft.fixture_id,
                            draft.source_run_id,
                            draft.source_step_id,
                            draft.tool_name,
                            draft.risk_level,
                            draft.canonical_args_hash,
                            draft.occurrence_index,
                            _json(draft.request),
                            _json(draft.response) if draft.response is not None else None,
                            draft.outcome.value,
                            draft.error_code,
                            draft.schema_version,
                            draft.replayable,
                            draft.blocked_reason,
                            draft.content_hash,
                            draft.created_at,
                        ),
                    )
                    await cursor.execute(
                        "SELECT * FROM agent_tool_fixture WHERE source_run_id=%s AND tool_name=%s "
                        "AND canonical_args_hash=%s AND occurrence_index=%s",
                        (
                            draft.source_run_id,
                            draft.tool_name,
                            draft.canonical_args_hash,
                            draft.occurrence_index,
                        ),
                    )
                    row = await cursor.fetchone()
                await connection.commit()
            return _fixture(row)
        except Exception as exc:
            raise TraceDependencyUnavailable("trace fixture write failed") from exc

    async def list_fixtures(self, source_run_id: str) -> list[ToolFixture]:
        return [
            _fixture(row)
            for row in await self._query(
                "SELECT * FROM agent_tool_fixture WHERE source_run_id=%s "
                "ORDER BY tool_name,canonical_args_hash,occurrence_index",
                (source_run_id,),
            )
        ]

    async def finish_run(self, run_id: str, **values: Any) -> TraceRun:
        completed_at: datetime = values["completed_at"]
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute("SELECT * FROM agent_run WHERE run_id=%s FOR UPDATE", (run_id,))
                        current = await cursor.fetchone()
                        if current is None:
                            raise TraceDependencyUnavailable("trace run does not exist")
                        existing = _run(current)
                        if existing.status.terminal:
                            await connection.commit()
                            return existing
                        await cursor.execute(
                            "SELECT * FROM agent_step WHERE run_id=%s ORDER BY started_at",
                            (run_id,),
                        )
                        step_rows = list(await cursor.fetchall())
                        _validate_snapshot(existing, [_step(row) for row in step_rows], values)
                        active_ms = _active_duration_rows(step_rows)
                        retries = sum(max(int(row["attempt_no"]) - 1, 0) for row in step_rows)
                        started = existing.started_at or existing.created_at
                        wall_ms = max(0, int((completed_at - started).total_seconds() * 1000))
                        values.update(_usage_values(existing, values["token_usage"], values.get("usage_status", UsageStatus.NO_CALLS), values["cost"]))
                        token_usage: TokenUsage = values["token_usage"]
                        cost: TraceCost = values["cost"]
                        await cursor.execute(
                            "UPDATE agent_run SET status=%s,trace_status=%s,replayable=%s,replay_block_reason=%s,"
                            "final_answer=%s,dag_json=%s,prompt_tokens=%s,completion_tokens=%s,cached_tokens=%s,"
                            "reasoning_tokens=%s,usage_status=%s,known_cost_microusd=%s,cost_status=%s,pricing_version=%s,"
                            "wall_duration_ms=%s,active_duration_ms=%s,retry_count=%s,unused_fixture_count=%s,"
                            "error_code=%s,error_message=%s,lease_owner=NULL,lease_expires_at=NULL,completed_at=%s,updated_at=%s "
                            "WHERE run_id=%s AND status NOT IN ('COMPLETED','FAILED','CANCELLED')",
                            (
                                values["status"].value,
                                values["trace_status"].value,
                                values["replayable"],
                                values["replay_block_reason"],
                                values["final_answer"],
                                _json(values["dag"]) if values["dag"] is not None else None,
                                token_usage.prompt,
                                token_usage.completion,
                                token_usage.cached,
                                token_usage.reasoning,
                                values["usage_status"].value,
                                cost.known_microusd,
                                cost.status.value,
                                cost.pricing_version,
                                wall_ms,
                                active_ms,
                                retries,
                                values["unused_fixture_count"],
                                values["error_code"],
                                values["error_message"],
                                completed_at,
                                completed_at,
                                run_id,
                            ),
                        )
                        await cursor.execute("SELECT * FROM agent_run WHERE run_id=%s", (run_id,))
                        row = await cursor.fetchone()
                    await connection.commit()
                    return _run(row)
                except Exception:
                    await connection.rollback()
                    raise
        except TraceError:
            raise
        except Exception as exc:
            raise TraceDependencyUnavailable("trace run completion failed") from exc

    async def pause_run(self, run_id: str, **values: Any) -> TraceRun:
        current = await self.get_worker_run(run_id)
        from app.trace.usage import persisted_steps
        _validate_snapshot(current, await persisted_steps(self, run_id), values)
        values.update(_usage_values(current, values["token_usage"], values.get("usage_status", UsageStatus.NO_CALLS), values["cost"]))
        token_usage: TokenUsage = values["token_usage"]
        cost: TraceCost = values["cost"]
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "UPDATE agent_run SET status=%s,trace_status=%s,replayable=FALSE,replay_block_reason=%s,"
                        "final_answer=%s,dag_json=%s,prompt_tokens=%s,completion_tokens=%s,cached_tokens=%s,"
                        "reasoning_tokens=%s,usage_status=%s,known_cost_microusd=%s,cost_status=%s,pricing_version=%s,"
                        "lease_owner=NULL,lease_expires_at=NULL,updated_at=%s "
                        "WHERE run_id=%s AND status NOT IN ('COMPLETED','FAILED','CANCELLED')",
                        (
                            values["status"].value,
                            values["trace_status"].value,
                            values["replay_block_reason"],
                            values["final_answer"],
                            _json(values["dag"]) if values["dag"] is not None else None,
                            token_usage.prompt,
                            token_usage.completion,
                            token_usage.cached,
                            token_usage.reasoning,
                            values["usage_status"].value,
                            cost.known_microusd,
                            cost.status.value,
                            cost.pricing_version,
                            values["updated_at"],
                            run_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise TraceDependencyUnavailable("trace run is not pausable")
                    await cursor.execute("SELECT * FROM agent_run WHERE run_id=%s", (run_id,))
                    row = await cursor.fetchone()
                await connection.commit()
                return _run(row)
        except TraceError:
            raise
        except Exception as exc:
            raise TraceDependencyUnavailable("trace run pause failed") from exc

    async def mark_incomplete(self, run_id: str, reason: str) -> None:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE agent_run SET trace_status='INCOMPLETE',replayable=FALSE,replay_block_reason=%s,"
                        "usage_status=CASE WHEN usage_status='LEGACY_UNKNOWN' THEN usage_status "
                        "WHEN usage_status IN ('COMPLETE','PARTIAL') OR EXISTS(SELECT 1 FROM agent_step s "
                        "WHERE s.run_id=agent_run.run_id AND s.step_type='SYSTEM' AND LEFT(s.node_id,6)='model:' "
                        "AND (s.prompt_tokens IS NOT NULL OR s.completion_tokens IS NOT NULL)) THEN 'PARTIAL' ELSE 'UNAVAILABLE' END,"
                        "prompt_tokens=IF(usage_status='LEGACY_UNKNOWN',prompt_tokens,NULL),"
                        "completion_tokens=IF(usage_status='LEGACY_UNKNOWN',completion_tokens,NULL),"
                        "cached_tokens=IF(usage_status='LEGACY_UNKNOWN',cached_tokens,NULL),"
                        "reasoning_tokens=IF(usage_status='LEGACY_UNKNOWN',reasoning_tokens,NULL),"
                        "known_cost_microusd=IF(usage_status='LEGACY_UNKNOWN',known_cost_microusd,NULL),"
                        "cost_status=IF(usage_status='LEGACY_UNKNOWN',cost_status,'UNAVAILABLE'),"
                        "pricing_version=IF(usage_status='LEGACY_UNKNOWN',pricing_version,NULL) "
                        "WHERE run_id=%s AND status NOT IN ('COMPLETED','FAILED','CANCELLED')",
                        (reason[:64], run_id),
                    )
                await connection.commit()
        except Exception as exc:
            raise TraceDependencyUnavailable("trace degradation update failed") from exc

    async def mark_unreplayable(self, run_id: str, reason: str) -> None:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE agent_run SET replayable=FALSE,replay_block_reason=%s "
                        "WHERE run_id=%s AND status NOT IN ('COMPLETED','FAILED','CANCELLED')",
                        (reason[:64], run_id),
                    )
                await connection.commit()
        except Exception as exc:
            raise TraceDependencyUnavailable("trace replayability update failed") from exc

    async def soft_delete(self, user_id: int, run_id: str, now: datetime) -> tuple[int, int]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM agent_run WHERE run_id=%s AND user_id=%s AND deleted_at IS NULL "
                            "AND run_kind IN ('LIVE','REPLAY') LIMIT 1 FOR UPDATE",
                            (run_id, user_id),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            raise TraceError("TRACE_NOT_FOUND", "trace not found")
                        run = _run(row)
                        if run.run_kind is RunKind.LIVE and not run.status.terminal:
                            raise TraceError("TRACE_NOT_DELETABLE", "a non-terminal live trace cannot be deleted")
                        targets = [run_id]
                        if run.run_kind is RunKind.LIVE:
                            await cursor.execute(
                                "SELECT run_id FROM agent_run WHERE source_run_id=%s AND user_id=%s AND deleted_at IS NULL FOR UPDATE",
                                (run_id, user_id),
                            )
                            targets.extend(str(item["run_id"]) for item in await cursor.fetchall())
                        placeholders = ",".join(["%s"] * len(targets))
                        await cursor.execute(
                            f"UPDATE agent_run SET completed_at=CASE WHEN run_kind='REPLAY' "
                            f"AND status IN ('QUEUED','RUNNING') THEN %s ELSE completed_at END,"
                            f"status=CASE WHEN run_kind='REPLAY' AND status IN ('QUEUED','RUNNING') "
                            f"THEN 'CANCELLED' ELSE status END,lease_owner=NULL,"
                            f"lease_expires_at=NULL,deleted_at=%s,updated_at=%s WHERE run_id IN ({placeholders})",
                            (now, now, now, *targets),
                        )
                    await connection.commit()
                    return len(targets), len(targets)
                except Exception:
                    await connection.rollback()
                    raise
        except TraceError:
            raise
        except Exception as exc:
            raise TraceDependencyUnavailable("trace delete failed") from exc

    async def claim_replay(self, owner: str, now: datetime, lease_expires_at: datetime) -> TraceRun | None:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT run_id,status FROM agent_run WHERE run_kind='REPLAY' AND deleted_at IS NULL "
                            "AND (status='QUEUED' OR (status='RUNNING' AND lease_expires_at<=%s)) "
                            "ORDER BY created_at,run_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                            (now,),
                        )
                        candidate = await cursor.fetchone()
                        if candidate is None:
                            await connection.commit()
                            return None
                        if candidate["status"] == 'RUNNING':
                            await cursor.execute(
                                "UPDATE agent_run SET trace_status='INCOMPLETE',replayable=FALSE,"
                                "replay_block_reason='RECOVERY_MODEL_COVERAGE_UNKNOWN' WHERE run_id=%s",
                                (candidate["run_id"],))
                        await cursor.execute(
                            "UPDATE agent_run SET status='RUNNING',lease_owner=%s,lease_expires_at=%s,"
                            "started_at=COALESCE(started_at,%s),updated_at=%s WHERE run_id=%s",
                            (owner, lease_expires_at, now, now, candidate["run_id"]),
                        )
                        await cursor.execute("SELECT * FROM agent_run WHERE run_id=%s", (candidate["run_id"],))
                        row = await cursor.fetchone()
                    await connection.commit()
                    return _run(row)
                except Exception:
                    await connection.rollback()
                    raise
        except Exception as exc:
            raise TraceDependencyUnavailable("trace replay claim failed") from exc

    async def renew_replay_lease(
        self, run_id: str, owner: str, now: datetime, lease_expires_at: datetime
    ) -> bool:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE agent_run SET lease_expires_at=%s,updated_at=%s "
                        "WHERE run_id=%s AND run_kind='REPLAY' AND status='RUNNING' "
                        "AND lease_owner=%s AND deleted_at IS NULL",
                        (lease_expires_at, now, run_id, owner),
                    )
                    renewed = cursor.rowcount == 1
                await connection.commit()
                return renewed
        except Exception as exc:
            raise TraceDependencyUnavailable("trace replay lease renewal failed") from exc

    async def is_deleted(self, run_id: str) -> bool:
        rows = await self._query(
            "SELECT deleted_at,status FROM agent_run WHERE run_id=%s LIMIT 1", (run_id,)
        )
        return not rows or rows[0]["deleted_at"] is not None or rows[0]["status"] == "CANCELLED"

    async def trace_bytes(self, run_id: str) -> int:
        rows = await self._query(
            "SELECT COALESCE(OCTET_LENGTH(input_json),0)+COALESCE(OCTET_LENGTH(dag_json),0)+"
            "COALESCE(OCTET_LENGTH(final_answer),0) AS total FROM agent_run WHERE run_id=%s",
            (run_id,),
        )
        if not rows:
            return 0
        total = int(rows[0]["total"])
        for query in (
            "SELECT COALESCE(SUM(COALESCE(OCTET_LENGTH(input_json),0)+COALESCE(OCTET_LENGTH(output_json),0)),0) AS total FROM agent_step WHERE run_id=%s",
            "SELECT COALESCE(SUM(OCTET_LENGTH(payload_json)),0) AS total FROM agent_evidence WHERE run_id=%s",
            "SELECT COALESCE(SUM(OCTET_LENGTH(request_json)+COALESCE(OCTET_LENGTH(response_json),0)),0) AS total FROM agent_tool_fixture WHERE source_run_id=%s",
        ):
            part = await self._query(query, (run_id,))
            total += int(part[0]["total"])
        return total

    async def purge_expired(self, now: datetime, hard_before: datetime) -> tuple[list[str], list[str]]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT run_id FROM agent_run WHERE deleted_at IS NULL AND retention_expires_at<=%s "
                            "ORDER BY retention_expires_at LIMIT 200 FOR UPDATE SKIP LOCKED",
                            (now,),
                        )
                        soft = [str(row["run_id"]) for row in await cursor.fetchall()]
                        if soft:
                            placeholders = ",".join(["%s"] * len(soft))
                            await cursor.execute(
                                f"UPDATE agent_run SET deleted_at=%s,updated_at=%s WHERE run_id IN ({placeholders})",
                                (now, now, *soft),
                            )
                        await cursor.execute(
                            "SELECT run_id FROM agent_run WHERE deleted_at IS NOT NULL AND deleted_at<=%s "
                            "AND source_run_id IS NOT NULL ORDER BY deleted_at LIMIT 200",
                            (hard_before,),
                        )
                        replay_hard = [str(row["run_id"]) for row in await cursor.fetchall()]
                        await cursor.execute(
                            "SELECT r.run_id FROM agent_run r WHERE r.deleted_at IS NOT NULL AND r.deleted_at<=%s "
                            "AND r.source_run_id IS NULL AND NOT EXISTS (SELECT 1 FROM agent_run child "
                            "WHERE child.source_run_id=r.run_id AND (child.deleted_at IS NULL OR child.deleted_at>%s)) "
                            "ORDER BY r.deleted_at LIMIT 200",
                            (hard_before, hard_before),
                        )
                        live_hard = [str(row["run_id"]) for row in await cursor.fetchall()]
                    await connection.commit()
                    return soft, [*replay_hard, *live_hard]
                except Exception:
                    await connection.rollback()
                    raise
        except Exception as exc:
            raise TraceDependencyUnavailable("trace retention cleanup failed") from exc

    async def hard_delete(self, run_id: str, hard_before: datetime) -> bool:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "DELETE r FROM agent_run r LEFT JOIN agent_run child ON child.source_run_id=r.run_id "
                        "WHERE r.run_id=%s AND r.deleted_at IS NOT NULL AND r.deleted_at<=%s "
                        "AND child.run_id IS NULL",
                        (run_id, hard_before),
                    )
                    deleted = cursor.rowcount > 0
                await connection.commit()
                return deleted
        except Exception as exc:
            raise TraceDependencyUnavailable("trace hard deletion failed") from exc

    async def _query(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(query, args)
                    return list(await cursor.fetchall())
        except Exception as exc:
            raise TraceDependencyUnavailable("trace database query failed") from exc


class TraceRepositoryRuntime:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._repository: AsyncMySQLTraceRepository | None = None

    async def startup(self) -> None:
        if self._repository is None:
            self._repository = await AsyncMySQLTraceRepository.create_pool(self._dsn)

    async def aclose(self) -> None:
        if self._repository is not None:
            await self._repository.aclose()
            self._repository = None

    def __getattr__(self, name: str):
        if self._repository is None:
            raise TraceDependencyUnavailable("trace repository has not started")
        return getattr(self._repository, name)


_INSERT_RUN = (
    "INSERT INTO agent_run (run_id,source_run_id,user_id,conversation_id,run_kind,workflow_type,status,trace_status,"
    "replay_mode,replayable,replay_block_reason,input_json,dag_json,final_answer,model_profile,model_manifest_hash,"
    "prompt_set_version,prompt_manifest_hash,tool_registry_version,harness_version,prompt_tokens,completion_tokens,"
    "cached_tokens,reasoning_tokens,known_cost_microusd,cost_currency,cost_status,pricing_version,wall_duration_ms,"
    "active_duration_ms,retry_count,unused_fixture_count,error_code,error_message,request_idempotency_key,request_hash,"
    "lease_owner,lease_expires_at,retention_expires_at,deleted_at,started_at,completed_at,created_at,updated_at,usage_status) "
    "VALUES (" + ",".join(["%s"] * 45) + ")"
)


def _run_insert_args(run: TraceRun) -> tuple[Any, ...]:
    return (
        run.run_id,
        run.source_run_id,
        run.user_id,
        run.conversation_id,
        run.run_kind.value,
        run.workflow_type.value,
        run.status.value,
        run.trace_status.value,
        run.replay_mode.value,
        run.replayable,
        run.replay_block_reason,
        _json(run.input),
        _json(run.dag) if run.dag is not None else None,
        run.final_answer,
        run.model_profile,
        run.model_manifest_hash,
        run.prompt_set_version,
        run.prompt_manifest_hash,
        run.tool_registry_version,
        run.harness_version,
        run.token_usage.prompt,
        run.token_usage.completion,
        run.token_usage.cached,
        run.token_usage.reasoning,
        run.cost.known_microusd,
        run.cost.currency,
        run.cost.status.value,
        run.cost.pricing_version,
        run.wall_duration_ms,
        run.active_duration_ms,
        run.retry_count,
        run.unused_fixture_count,
        run.error_code,
        run.error_message,
        run.request_idempotency_key,
        run.request_hash,
        run.lease_owner,
        run.lease_expires_at,
        run.retention_expires_at,
        run.deleted_at,
        run.started_at,
        run.completed_at,
        run.created_at,
        run.updated_at,
        run.usage_status.value,
    )


def _run(row: dict[str, Any]) -> TraceRun:
    return TraceRun(
        run_id=str(row["run_id"]),
        source_run_id=row.get("source_run_id"),
        user_id=int(row["user_id"]) if row.get("user_id") is not None else None,
        conversation_id=row.get("conversation_id"),
        run_kind=RunKind(row["run_kind"]),
        workflow_type=WorkflowType(row["workflow_type"]),
        status=RunStatus(row["status"]),
        trace_status=TraceStatus(row["trace_status"]),
        replay_mode=ReplayMode(row["replay_mode"]),
        replayable=bool(row["replayable"]),
        replay_block_reason=row.get("replay_block_reason"),
        input=_json_value(row["input_json"]),
        dag=_json_value(row.get("dag_json")),
        final_answer=row.get("final_answer"),
        model_profile=row["model_profile"],
        model_manifest_hash=bytes(row["model_manifest_hash"]),
        prompt_set_version=row["prompt_set_version"],
        prompt_manifest_hash=bytes(row["prompt_manifest_hash"]),
        tool_registry_version=row["tool_registry_version"],
        harness_version=row["harness_version"],
        token_usage=TokenUsage(
            prompt=row["prompt_tokens"],
            completion=row["completion_tokens"],
            cached=int(row["cached_tokens"]) if row.get("cached_tokens") is not None else None,
            reasoning=int(row["reasoning_tokens"]) if row.get("reasoning_tokens") is not None else None,
        ),
        usage_status=UsageStatus(row.get("usage_status", "LEGACY_UNKNOWN")),
        cost=TraceCost(
            status=CostStatus(row["cost_status"]),
            known_microusd=(
                int(row["known_cost_microusd"]) if row.get("known_cost_microusd") is not None else None
            ),
            currency=row["cost_currency"],
            pricing_version=row.get("pricing_version"),
        ),
        wall_duration_ms=int(row["wall_duration_ms"]) if row.get("wall_duration_ms") is not None else None,
        active_duration_ms=int(row["active_duration_ms"]) if row.get("active_duration_ms") is not None else None,
        retry_count=int(row["retry_count"]),
        unused_fixture_count=(
            int(row["unused_fixture_count"]) if row.get("unused_fixture_count") is not None else None
        ),
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        request_idempotency_key=row.get("request_idempotency_key"),
        request_hash=bytes(row["request_hash"]) if row.get("request_hash") is not None else None,
        lease_owner=row.get("lease_owner"),
        lease_expires_at=utc(row.get("lease_expires_at")),
        retention_expires_at=utc(row["retention_expires_at"]),
        deleted_at=utc(row.get("deleted_at")),
        started_at=utc(row.get("started_at")),
        completed_at=utc(row.get("completed_at")),
        created_at=utc(row["created_at"]),
        updated_at=utc(row["updated_at"]),
    )


def _step(row: dict[str, Any]) -> TraceStep:
    return TraceStep(
        step_id=str(row["step_id"]),
        run_id=str(row["run_id"]),
        parent_step_id=row.get("parent_step_id"),
        sequence_no=int(row["sequence_no"]),
        node_id=row["node_id"],
        step_type=StepType(row["step_type"]),
        agent_name=row.get("agent_name"),
        tool_name=row.get("tool_name"),
        status=StepStatus(row["status"]),
        attempt_no=int(row["attempt_no"]),
        input=_json_value(row.get("input_json")),
        output=_json_value(row.get("output_json")),
        evidence_ids=_json_value(row["evidence_ids_json"]),
        model_profile=row.get("model_profile"),
        prompt_version=row.get("prompt_version"),
        tool_version=row.get("tool_version"),
        token_usage=TokenUsage(
            prompt=row["prompt_tokens"], completion=row["completion_tokens"],
            cached=row.get("cached_tokens"), reasoning=row.get("reasoning_tokens"),
        ),
        usage_status=UsageStatus(row.get("usage_status", "LEGACY_UNKNOWN")),
        cost=TraceCost(
            status=CostStatus(row["cost_status"]),
            known_microusd=(
                int(row["known_cost_microusd"]) if row.get("known_cost_microusd") is not None else None
            ),
        ),
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        started_at=utc(row["started_at"]),
        completed_at=utc(row.get("completed_at")),
        duration_ms=int(row["duration_ms"]) if row.get("duration_ms") is not None else None,
        created_at=utc(row["created_at"]),
    )


def _evidence(row: dict[str, Any]) -> TraceEvidence:
    return TraceEvidence(
        run_id=str(row["run_id"]),
        evidence_id=row["evidence_id"],
        source_step_id=str(row["source_step_id"]),
        evidence_type=row["evidence_type"],
        source_ref=row["source_ref"],
        payload=_json_value(row["payload_json"]),
        content_hash=bytes(row["content_hash"]),
        created_at=utc(row["created_at"]),
    )


def _fixture(row: dict[str, Any]) -> ToolFixture:
    return ToolFixture(
        fixture_id=str(row["fixture_id"]),
        source_run_id=str(row["source_run_id"]),
        source_step_id=str(row["source_step_id"]),
        tool_name=row["tool_name"],
        risk_level=row["risk_level"],
        canonical_args_hash=bytes(row["canonical_args_hash"]),
        occurrence_index=int(row["occurrence_index"]),
        request=_json_value(row["request_json"]),
        response=_json_value(row.get("response_json")),
        outcome=FixtureOutcome(row["outcome"]),
        error_code=row.get("error_code"),
        schema_version=row["schema_version"],
        replayable=bool(row["replayable"]),
        blocked_reason=row.get("blocked_reason"),
        content_hash=bytes(row["content_hash"]),
        created_at=utc(row["created_at"]),
    )


def _validate_replay_source(source: TraceRun | None, user_id: int | None) -> None:
    if (
        source is None
        or source.user_id != user_id
        or source.deleted_at is not None
        or source.run_kind is not RunKind.LIVE
        or source.workflow_type is not WorkflowType.CHAT
        or not source.status.terminal
        or source.trace_status is not TraceStatus.COMPLETE
        or not source.replayable
    ):
        raise TraceError("TRACE_NOT_REPLAYABLE", "trace is not replayable")


def active_duration_ms(steps: Sequence[TraceStep]) -> int:
    intervals = sorted(
        (step.started_at, step.completed_at)
        for step in steps
        if step.completed_at is not None
    )
    if not intervals:
        return 0
    total = 0
    start, end = intervals[0]
    assert end is not None
    for next_start, next_end in intervals[1:]:
        assert next_end is not None
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += max(0, int((end - start).total_seconds() * 1000))
            start, end = next_start, next_end
    return total + max(0, int((end - start).total_seconds() * 1000))


def _active_duration_rows(rows: Sequence[dict[str, Any]]) -> int:
    intervals = [
        (utc(row["started_at"]), utc(row["completed_at"]))
        for row in rows
        if row.get("completed_at") is not None
    ]
    if not intervals:
        return 0
    intervals.sort(key=lambda item: item[0])
    start, end = intervals[0]
    assert start is not None and end is not None
    total = 0
    for next_start, next_end in intervals[1:]:
        assert next_start is not None and next_end is not None
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += max(0, int((end - start).total_seconds() * 1000))
            start, end = next_start, next_end
    return total + max(0, int((end - start).total_seconds() * 1000))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value)

def _usage_values(current, usage, status, cost):
    if current.usage_status is UsageStatus.LEGACY_UNKNOWN:
        return dict(token_usage=current.token_usage, usage_status=current.usage_status, cost=current.cost)
    status = UsageStatus(status)
    validate_usage(usage, status, step=isinstance(current, TraceStep))
    if cost.status is CostStatus.PARTIAL:
        cost = TraceCost(status=CostStatus.UNAVAILABLE)
    return dict(token_usage=usage, usage_status=status, cost=cost)


def _validate_new_run(run):
    if run.usage_status is UsageStatus.LEGACY_UNKNOWN:
        raise ValueError("new runs cannot use legacy usage status")
    # Also validates callers that used model_copy (which itself skips validation).
    TraceRun.model_validate(run.model_dump())


def _validate_snapshot(run, steps, values):
    if run.usage_status is UsageStatus.LEGACY_UNKNOWN:
        return
    incomplete = run.trace_status is TraceStatus.INCOMPLETE or values["trace_status"] is TraceStatus.INCOMPLETE
    if incomplete:
        values["trace_status"] = TraceStatus.INCOMPLETE
        values["replayable"] = False
    status = UsageStatus(values.get("usage_status", UsageStatus.NO_CALLS))
    expected_usage, expected_status = aggregate_usage(steps, coverage_complete=not incomplete,
        observed_known=incomplete and status is UsageStatus.PARTIAL)
    if values["token_usage"] != expected_usage or status is not expected_status:
        raise ValueError("trace usage snapshot does not match persisted model attempts")
