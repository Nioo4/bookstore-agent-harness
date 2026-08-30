from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.trace.errors import TraceDependencyUnavailable, TraceError
from app.trace.manifests import VersionRegistry
from app.trace.repository import TraceRepository
from app.trace.types import (
    CostStatus,
    ReplayMode,
    RunKind,
    RunStatus,
    TraceCost,
    TraceRun,
    TraceStatus,
    WorkflowType,
    sha256_json,
)


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_profile: str | None = Field(default=None, min_length=1, max_length=64)
    prompt_set_version: str | None = Field(default=None, min_length=1, max_length=64)


class ReplayRateLimiter(Protocol):
    async def allow(self, user_id: int, idempotency_hash: str, now: datetime) -> None: ...


class InMemoryReplayRateLimiter:
    def __init__(self, maximum: int = 5) -> None:
        self.maximum = maximum
        self._values: dict[int, dict[str, datetime]] = {}

    async def allow(self, user_id: int, idempotency_hash: str, now: datetime) -> None:
        threshold = now - timedelta(hours=1)
        values = self._values.setdefault(user_id, {})
        for key, created_at in list(values.items()):
            if created_at <= threshold:
                del values[key]
        if idempotency_hash in values:
            return
        if len(values) >= self.maximum:
            raise TraceError("RATE_LIMITED", "replay creation rate limit exceeded")
        values[idempotency_hash] = now


class RedisReplayRateLimiter:
    _SCRIPT = """
local key = KEYS[1]
local member = ARGV[1]
local now_ms = tonumber(ARGV[2])
local cutoff_ms = tonumber(ARGV[3])
local maximum = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff_ms)
if redis.call('ZSCORE', key, member) then
  redis.call('EXPIRE', key, 7200)
  return 1
end
if redis.call('ZCARD', key) >= maximum then
  return 0
end
redis.call('ZADD', key, now_ms, member)
redis.call('EXPIRE', key, 7200)
return 1
"""

    def __init__(self, redis, maximum: int = 5, prefix: str = "trace:replay-rate") -> None:
        self.redis = redis
        self.maximum = maximum
        self.prefix = prefix

    async def allow(self, user_id: int, idempotency_hash: str, now: datetime) -> None:
        now_ms = int(now.timestamp() * 1000)
        try:
            allowed = await self.redis.eval(
                self._SCRIPT,
                1,
                f"{self.prefix}:{user_id}",
                idempotency_hash,
                now_ms,
                now_ms - 3_600_000,
                self.maximum,
            )
        except Exception as exc:
            raise TraceDependencyUnavailable("replay rate limiter is unavailable") from exc
        if int(allowed) != 1:
            raise TraceError("RATE_LIMITED", "replay creation rate limit exceeded")


class TraceService:
    def __init__(
        self,
        repository: TraceRepository,
        versions: VersionRegistry,
        rate_limiter: ReplayRateLimiter,
        *,
        tool_registry_version: str,
        harness_version: str,
        retention_days: int = 30,
    ) -> None:
        self.repository = repository
        self.versions = versions
        self.rate_limiter = rate_limiter
        self.tool_registry_version = tool_registry_version
        self.harness_version = harness_version
        self.retention_days = retention_days

    async def page_runs(
        self,
        user_id: int,
        *,
        conversation_id: str | None,
        kind: str | None,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[TraceRun], str | None]:
        if not 1 <= limit <= 50:
            raise TraceError("TRACE_REQUEST_INVALID", "limit must be between 1 and 50")
        if conversation_id is not None and (not conversation_id or len(conversation_id) > 128):
            raise TraceError("TRACE_REQUEST_INVALID", "invalid conversation id")
        try:
            parsed_kind = RunKind(kind) if kind is not None else None
            parsed_status = RunStatus(status) if status is not None else None
        except ValueError as exc:
            raise TraceError("TRACE_REQUEST_INVALID", "invalid trace filter") from exc
        if parsed_kind is RunKind.EVALUATION:
            raise TraceError("TRACE_REQUEST_INVALID", "evaluation traces are not public")
        before = decode_run_cursor(cursor) if cursor else None
        values = await self.repository.list_owned(
            user_id,
            conversation_id=conversation_id,
            kind=parsed_kind,
            status=parsed_status,
            before=before,
            limit=limit + 1,
        )
        return _run_page(values, limit)

    async def public_summaries(self, user_id: int, runs: list[TraceRun]) -> list[dict]:
        # Replay input is configuration, not the user's question. Resolve only
        # owned LIVE sources, once per source in this response; do not persist copies.
        sources: dict[str, TraceRun | None] = {
            run.run_id: run for run in runs if run.run_kind is RunKind.LIVE and run.user_id == user_id
        }
        summaries = []
        for run in runs:
            summary = run.public_summary()
            if run.run_kind is RunKind.REPLAY:
                summary["user_question"] = None
                source_id = run.source_run_id
                if source_id and source_id not in sources:
                    try:
                        sources[source_id] = await self.repository.get_owned(user_id, source_id)
                    except TraceError as exc:
                        if exc.code != "TRACE_NOT_FOUND":
                            raise
                        sources[source_id] = None
                source = sources.get(source_id) if source_id else None
                if source is not None and source.run_kind is RunKind.LIVE:
                    summary["user_question"] = source.public_summary()["user_question"]
            summaries.append(summary)
        return summaries

    async def detail(
        self, user_id: int, run_id: str, *, step_cursor: str | None, step_limit: int
    ) -> dict[str, object]:
        if not 1 <= step_limit <= 200:
            raise TraceError("TRACE_REQUEST_INVALID", "step limit must be between 1 and 200")
        after = decode_step_cursor(step_cursor) if step_cursor else None
        run = await self.repository.get_owned(user_id, run_id)
        steps = await self.repository.list_steps(run_id, after, step_limit + 1)
        page = steps[:step_limit]
        next_cursor = encode_step_cursor(page[-1].sequence_no) if len(steps) > step_limit else None
        evidence = await self.repository.list_evidence(run_id)
        fixtures = await self.repository.list_fixtures(run_id)
        public_run = run.public_detail()
        public_run.update((await self.public_summaries(user_id, [run]))[0])
        return {
            "run": public_run,
            "steps": [step.public() for step in page],
            "evidence": [item.public() for item in evidence],
            "fixture_summary": {
                "count": len(fixtures),
                "replayable_count": sum(1 for item in fixtures if item.replayable),
            },
            "next_step_cursor": next_cursor,
            "replays": {"count": await self.repository.replay_count(run_id)},
        }

    async def page_replays(
        self, user_id: int, source_run_id: str, *, cursor: str | None, limit: int
    ) -> tuple[list[TraceRun], str | None]:
        if not 1 <= limit <= 50:
            raise TraceError("TRACE_REQUEST_INVALID", "limit must be between 1 and 50")
        source = await self.repository.get_owned(user_id, source_run_id)
        if source.run_kind is not RunKind.LIVE:
            raise TraceError("TRACE_NOT_FOUND", "trace not found")
        before = decode_run_cursor(cursor) if cursor else None
        values = await self.repository.list_replays(user_id, source_run_id, before, limit + 1)
        return _run_page(values, limit)

    async def create_replay(
        self,
        user_id: int,
        source_run_id: str,
        request: ReplayRequest,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> tuple[TraceRun, bool]:
        if not 16 <= len(idempotency_key) <= 128:
            raise TraceError("TRACE_REQUEST_INVALID", "Idempotency-Key must be 16 to 128 characters")
        timestamp = now or datetime.now(UTC)
        source = await self.repository.get_owned(user_id, source_run_id)
        if (
            source.run_kind is not RunKind.LIVE
            or source.workflow_type is not WorkflowType.CHAT
            or not source.status.terminal
            or source.trace_status is not TraceStatus.COMPLETE
            or not source.replayable
        ):
            raise TraceError("TRACE_NOT_REPLAYABLE", "trace is not replayable")
        model_profile = request.model_profile or source.model_profile
        prompt_version = request.prompt_set_version or source.prompt_set_version
        self.versions.model_profile(model_profile)
        self.versions.prompt_set(prompt_version)
        normalized = {
            "source_run_id": source_run_id,
            "model_profile": model_profile,
            "prompt_set_version": prompt_version,
            "replay_mode": ReplayMode.FIXED_TOOL_RESULTS.value,
        }
        request_hash = sha256_json(normalized)
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        await self.rate_limiter.allow(user_id, key_hash, timestamp)
        replay = TraceRun(
            run_id=str(uuid.uuid4()),
            source_run_id=source_run_id,
            user_id=user_id,
            conversation_id=source.conversation_id,
            run_kind=RunKind.REPLAY,
            workflow_type=WorkflowType.CHAT,
            status=RunStatus.QUEUED,
            trace_status=TraceStatus.RECORDING,
            replay_mode=ReplayMode.FIXED_TOOL_RESULTS,
            replayable=False,
            replay_block_reason="RUN_NOT_COMPLETED",
            input=normalized,
            model_profile=model_profile,
            model_manifest_hash=self.versions.model_profile_hash(model_profile),
            prompt_set_version=prompt_version,
            prompt_manifest_hash=self.versions.prompt_set_hash(prompt_version),
            tool_registry_version=self.tool_registry_version,
            harness_version=self.harness_version,
            cost=TraceCost(status=CostStatus.UNAVAILABLE),
            request_idempotency_key=key_hash,
            request_hash=request_hash,
            retention_expires_at=timestamp + timedelta(days=self.retention_days),
            created_at=timestamp,
            updated_at=timestamp,
        )
        return await self.repository.create_replay(replay)

    async def delete(self, user_id: int, run_id: str, now: datetime | None = None) -> tuple[int, int]:
        return await self.repository.soft_delete(user_id, run_id, now or datetime.now(UTC))


def encode_run_cursor(created_at: datetime, run_id: str) -> str:
    return _encode_cursor([created_at.astimezone(UTC).isoformat(), run_id])


def decode_run_cursor(value: str) -> tuple[datetime, str]:
    decoded = _decode_cursor(value)
    try:
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        timestamp = datetime.fromisoformat(str(decoded[0]))
        if timestamp.tzinfo is None:
            raise ValueError
        run_id = str(uuid.UUID(str(decoded[1])))
        return timestamp.astimezone(UTC), run_id
    except Exception as exc:
        raise TraceError("TRACE_REQUEST_INVALID", "invalid trace cursor") from exc


def encode_step_cursor(sequence_no: int) -> str:
    return _encode_cursor([sequence_no])


def decode_step_cursor(value: str) -> int:
    decoded = _decode_cursor(value)
    if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], int) or decoded[0] < 1:
        raise TraceError("TRACE_REQUEST_INVALID", "invalid step cursor")
    return decoded[0]


def _run_page(values: list[TraceRun], limit: int) -> tuple[list[TraceRun], str | None]:
    page = values[:limit]
    next_cursor = None
    if len(values) > limit and page:
        next_cursor = encode_run_cursor(page[-1].created_at, page[-1].run_id)
    return page, next_cursor


def _encode_cursor(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str):
    try:
        if not value or len(value) > 512:
            raise ValueError
        padded = value + "=" * (-len(value) % 4)
        return json.loads(base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8"))
    except Exception as exc:
        raise TraceError("TRACE_REQUEST_INVALID", "invalid trace cursor") from exc
