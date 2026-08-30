from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import asyncmy
from asyncmy.cursors import DictCursor
from pydantic import BaseModel, ConfigDict

from app.memory.repository import _parse_mysql_url
from app.trace.errors import TraceError
from app.trace.types import CostStatus, canonical_json, utc


class EvaluationRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_run_id: str
    idempotency_key: str
    request_hash: bytes
    suite_version: str
    dataset_hash: bytes
    runner_version: str
    mode: str
    status: str
    model_profile: str
    prompt_set_version: str
    judge_model_profile: str | None = None
    judge_prompt_version: str | None = None
    judge_status: str
    case_count: int
    passed_count: int = 0
    failed_count: int = 0
    error_count: int = 0
    metrics: dict[str, Any] | None = None
    deterministic_gate_passed: bool | None = None
    known_cost_microusd: int | None = None
    cost_status: CostStatus = CostStatus.UNAVAILABLE
    retention_expires_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EvaluationCaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    eval_run_id: str
    case_id: str
    agent_run_id: str | None = None
    category: str
    status: str
    case_snapshot: dict[str, Any]
    fixture_manifest_hash: bytes
    deterministic_metrics: dict[str, Any]
    failure_codes: list[str]
    judge_scores: dict[str, Any] | None = None
    judge_reason: str | None = None
    duration_ms: int
    known_cost_microusd: int | None = None
    created_at: datetime


class EvaluationRepository(Protocol):
    async def create(self, run: EvaluationRunRecord) -> tuple[EvaluationRunRecord, bool]: ...
    async def put_case(self, case: EvaluationCaseRecord) -> EvaluationCaseRecord: ...
    async def finish(self, eval_run_id: str, **updates: Any) -> EvaluationRunRecord: ...
    async def get(self, eval_run_id: str) -> EvaluationRunRecord: ...
    async def list_cases(self, eval_run_id: str) -> list[EvaluationCaseRecord]: ...
    async def purge_expired(self, now: datetime) -> list[str]: ...


class InMemoryEvaluationRepository:
    def __init__(self) -> None:
        self.runs: dict[str, EvaluationRunRecord] = {}
        self.cases: dict[str, dict[str, EvaluationCaseRecord]] = {}
        self._lock = asyncio.Lock()

    async def create(self, run: EvaluationRunRecord) -> tuple[EvaluationRunRecord, bool]:
        async with self._lock:
            for current in self.runs.values():
                if current.idempotency_key == run.idempotency_key:
                    if current.request_hash != run.request_hash:
                        raise TraceError("IDEMPOTENCY_CONFLICT", "evaluation idempotency conflict")
                    return current, True
            self.runs[run.eval_run_id] = run
            return run, False

    async def put_case(self, case: EvaluationCaseRecord) -> EvaluationCaseRecord:
        async with self._lock:
            values = self.cases.setdefault(case.eval_run_id, {})
            return values.setdefault(case.case_id, case)

    async def finish(self, eval_run_id: str, **updates: Any) -> EvaluationRunRecord:
        async with self._lock:
            run = self.runs.get(eval_run_id)
            if run is None:
                raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "evaluation run not found")
            if run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                return run
            completed = run.model_copy(update=updates)
            self.runs[eval_run_id] = completed
            return completed

    async def get(self, eval_run_id: str) -> EvaluationRunRecord:
        try:
            return self.runs[eval_run_id]
        except KeyError as exc:
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "evaluation run not found") from exc

    async def list_cases(self, eval_run_id: str) -> list[EvaluationCaseRecord]:
        return sorted(self.cases.get(eval_run_id, {}).values(), key=lambda item: item.case_id)

    async def purge_expired(self, now: datetime) -> list[str]:
        async with self._lock:
            expired = sorted(
                run_id
                for run_id, run in self.runs.items()
                if run.retention_expires_at <= now
            )
            for run_id in expired:
                del self.runs[run_id]
                self.cases.pop(run_id, None)
            return expired


class AsyncMySQLEvaluationRepository:
    def __init__(self, pool: asyncmy.Pool) -> None:
        self.pool = pool

    @classmethod
    async def create_pool(cls, dsn: str) -> "AsyncMySQLEvaluationRepository":
        try:
            pool = await asyncmy.create_pool(
                minsize=1, maxsize=5, autocommit=False, **_parse_mysql_url(dsn)
            )
            return cls(pool)
        except Exception as exc:
            raise TraceError(
                "EVAL_DEPENDENCY_UNAVAILABLE", "evaluation database unavailable", retryable=True
            ) from exc

    async def aclose(self) -> None:
        self.pool.close()
        await self.pool.wait_closed()

    async def create(self, run: EvaluationRunRecord) -> tuple[EvaluationRunRecord, bool]:
        try:
            async with self.pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM evaluation_run WHERE idempotency_key=%s LIMIT 1 FOR UPDATE",
                            (run.idempotency_key,),
                        )
                        row = await cursor.fetchone()
                        if row is not None:
                            current = _run(row)
                            if current.request_hash != run.request_hash:
                                raise TraceError("IDEMPOTENCY_CONFLICT", "evaluation idempotency conflict")
                            await connection.commit()
                            return current, True
                        await cursor.execute(
                            "INSERT INTO evaluation_run "
                            "(eval_run_id,idempotency_key,request_hash,suite_version,dataset_hash,runner_version,mode,status,"
                            "model_profile,prompt_set_version,judge_model_profile,judge_prompt_version,judge_status,case_count,"
                            "passed_count,failed_count,error_count,metrics_json,deterministic_gate_passed,known_cost_microusd,"
                            "cost_status,lease_owner,lease_expires_at,retention_expires_at,started_at,completed_at,created_at,updated_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,0,NULL,NULL,NULL,%s,NULL,NULL,%s,%s,NULL,%s,%s)",
                            (
                                run.eval_run_id,
                                run.idempotency_key,
                                run.request_hash,
                                run.suite_version,
                                run.dataset_hash,
                                run.runner_version,
                                run.mode,
                                run.status,
                                run.model_profile,
                                run.prompt_set_version,
                                run.judge_model_profile,
                                run.judge_prompt_version,
                                run.judge_status,
                                run.case_count,
                                run.cost_status.value,
                                run.retention_expires_at,
                                run.started_at,
                                run.created_at,
                                run.updated_at,
                            ),
                        )
                    await connection.commit()
                    return run, False
                except Exception:
                    await connection.rollback()
                    raise
        except TraceError:
            raise
        except Exception as exc:
            raise TraceError(
                "EVAL_DEPENDENCY_UNAVAILABLE", "evaluation run create failed", retryable=True
            ) from exc

    async def put_case(self, case: EvaluationCaseRecord) -> EvaluationCaseRecord:
        try:
            async with self.pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "INSERT IGNORE INTO evaluation_case_result "
                        "(eval_run_id,case_id,agent_run_id,category,status,case_snapshot_json,fixture_manifest_hash,"
                        "deterministic_metrics_json,failure_codes_json,judge_scores_json,judge_reason,duration_ms,"
                        "known_cost_microusd,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            case.eval_run_id,
                            case.case_id,
                            case.agent_run_id,
                            case.category,
                            case.status,
                            canonical_json(case.case_snapshot),
                            case.fixture_manifest_hash,
                            canonical_json(case.deterministic_metrics),
                            canonical_json(case.failure_codes),
                            canonical_json(case.judge_scores) if case.judge_scores is not None else None,
                            case.judge_reason,
                            case.duration_ms,
                            case.known_cost_microusd,
                            case.created_at,
                        ),
                    )
                    await cursor.execute(
                        "SELECT * FROM evaluation_case_result WHERE eval_run_id=%s AND case_id=%s",
                        (case.eval_run_id, case.case_id),
                    )
                    row = await cursor.fetchone()
                await connection.commit()
                return _case(row)
        except Exception as exc:
            raise TraceError(
                "EVAL_DEPENDENCY_UNAVAILABLE", "evaluation case write failed", retryable=True
            ) from exc

    async def finish(self, eval_run_id: str, **updates: Any) -> EvaluationRunRecord:
        try:
            async with self.pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(
                        "UPDATE evaluation_run SET status=%s,passed_count=%s,failed_count=%s,error_count=%s,"
                        "metrics_json=%s,deterministic_gate_passed=%s,judge_status=%s,known_cost_microusd=%s,"
                        "cost_status=%s,lease_owner=NULL,lease_expires_at=NULL,completed_at=%s,updated_at=%s "
                        "WHERE eval_run_id=%s AND status NOT IN ('COMPLETED','FAILED','CANCELLED')",
                        (
                            updates["status"],
                            updates["passed_count"],
                            updates["failed_count"],
                            updates["error_count"],
                            canonical_json(updates["metrics"]),
                            updates["deterministic_gate_passed"],
                            updates["judge_status"],
                            updates["known_cost_microusd"],
                            updates["cost_status"].value,
                            updates["completed_at"],
                            updates["updated_at"],
                            eval_run_id,
                        ),
                    )
                    await cursor.execute(
                        "SELECT * FROM evaluation_run WHERE eval_run_id=%s", (eval_run_id,)
                    )
                    row = await cursor.fetchone()
                await connection.commit()
                return _run(row)
        except Exception as exc:
            raise TraceError(
                "EVAL_DEPENDENCY_UNAVAILABLE", "evaluation run completion failed", retryable=True
            ) from exc

    async def get(self, eval_run_id: str) -> EvaluationRunRecord:
        rows = await self._query(
            "SELECT * FROM evaluation_run WHERE eval_run_id=%s", (eval_run_id,)
        )
        if not rows:
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "evaluation run not found")
        return _run(rows[0])

    async def list_cases(self, eval_run_id: str) -> list[EvaluationCaseRecord]:
        return [
            _case(row)
            for row in await self._query(
                "SELECT * FROM evaluation_case_result WHERE eval_run_id=%s ORDER BY case_id",
                (eval_run_id,),
            )
        ]

    async def purge_expired(self, now: datetime) -> list[str]:
        try:
            async with self.pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT eval_run_id FROM evaluation_run WHERE retention_expires_at<=%s "
                            "ORDER BY retention_expires_at LIMIT 200 FOR UPDATE SKIP LOCKED",
                            (now,),
                        )
                        expired = [str(row["eval_run_id"]) for row in await cursor.fetchall()]
                        if expired:
                            placeholders = ",".join(["%s"] * len(expired))
                            await cursor.execute(
                                f"DELETE FROM evaluation_run WHERE eval_run_id IN ({placeholders})",
                                tuple(expired),
                            )
                    await connection.commit()
                    return expired
                except Exception:
                    await connection.rollback()
                    raise
        except Exception as exc:
            raise TraceError(
                "EVAL_DEPENDENCY_UNAVAILABLE", "evaluation retention cleanup failed", retryable=True
            ) from exc

    async def _query(self, query: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            async with self.pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(query, args)
                    return list(await cursor.fetchall())
        except Exception as exc:
            raise TraceError(
                "EVAL_DEPENDENCY_UNAVAILABLE", "evaluation database query failed", retryable=True
            ) from exc


class EvaluationCleanupLoop:
    def __init__(
        self,
        repository: EvaluationRepository,
        *,
        interval_seconds: int = 3600,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper
        self._stopping = False

    async def run(self) -> None:
        while not self._stopping:
            await self.run_once()
            await self.sleeper(self.interval_seconds)

    async def run_once(self) -> list[str]:
        return await self.repository.purge_expired(self.clock())

    def stop(self) -> None:
        self._stopping = True


def _json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value)


def _run(row: dict[str, Any]) -> EvaluationRunRecord:
    return EvaluationRunRecord(
        eval_run_id=str(row["eval_run_id"]),
        idempotency_key=row["idempotency_key"],
        request_hash=bytes(row["request_hash"]),
        suite_version=row["suite_version"],
        dataset_hash=bytes(row["dataset_hash"]),
        runner_version=row["runner_version"],
        mode=row["mode"],
        status=row["status"],
        model_profile=row["model_profile"],
        prompt_set_version=row["prompt_set_version"],
        judge_model_profile=row.get("judge_model_profile"),
        judge_prompt_version=row.get("judge_prompt_version"),
        judge_status=row["judge_status"],
        case_count=int(row["case_count"]),
        passed_count=int(row["passed_count"]),
        failed_count=int(row["failed_count"]),
        error_count=int(row["error_count"]),
        metrics=_json(row.get("metrics_json")),
        deterministic_gate_passed=(
            bool(row["deterministic_gate_passed"])
            if row.get("deterministic_gate_passed") is not None
            else None
        ),
        known_cost_microusd=(
            int(row["known_cost_microusd"])
            if row.get("known_cost_microusd") is not None
            else None
        ),
        cost_status=CostStatus(row["cost_status"]),
        retention_expires_at=utc(row["retention_expires_at"]),
        started_at=utc(row.get("started_at")),
        completed_at=utc(row.get("completed_at")),
        created_at=utc(row["created_at"]),
        updated_at=utc(row["updated_at"]),
    )


def _case(row: dict[str, Any]) -> EvaluationCaseRecord:
    return EvaluationCaseRecord(
        eval_run_id=str(row["eval_run_id"]),
        case_id=row["case_id"],
        agent_run_id=row.get("agent_run_id"),
        category=row["category"],
        status=row["status"],
        case_snapshot=_json(row["case_snapshot_json"]),
        fixture_manifest_hash=bytes(row["fixture_manifest_hash"]),
        deterministic_metrics=_json(row["deterministic_metrics_json"]),
        failure_codes=_json(row["failure_codes_json"]),
        judge_scores=_json(row.get("judge_scores_json")),
        judge_reason=row.get("judge_reason"),
        duration_ms=int(row["duration_ms"]),
        known_cost_microusd=(
            int(row["known_cost_microusd"])
            if row.get("known_cost_microusd") is not None
            else None
        ),
        created_at=utc(row["created_at"]),
    )
