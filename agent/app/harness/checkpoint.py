from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis


class RunStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    WAITING_EVENT = "WAITING_EVENT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NodeStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class NodeCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    node_id: str
    status: NodeStatus
    attempt: int = Field(ge=1, le=2)
    output: dict[str, Any] | None = None
    error: str | None = None
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    error: str | None = None
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CheckpointStore(Protocol):
    async def save_node(self, checkpoint: NodeCheckpoint) -> None: ...

    async def save_run(self, checkpoint: RunCheckpoint) -> None: ...

    async def load_run(self, run_id: str) -> RunCheckpoint | None: ...


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self.nodes: list[NodeCheckpoint] = []
        self.runs: list[RunCheckpoint] = []

    async def save_node(self, checkpoint: NodeCheckpoint) -> None:
        self.nodes.append(checkpoint.model_copy(deep=True))

    async def save_run(self, checkpoint: RunCheckpoint) -> None:
        self.runs.append(checkpoint.model_copy(deep=True))

    async def load_run(self, run_id: str) -> RunCheckpoint | None:
        return next((item.model_copy(deep=True) for item in reversed(self.runs) if item.run_id == run_id), None)


class RedisCheckpointStore:
    def __init__(self, redis: Redis, *, ttl_seconds: int = 7 * 24 * 60 * 60) -> None:
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    async def save_node(self, checkpoint: NodeCheckpoint) -> None:
        key = f"agent:run:{checkpoint.run_id}:nodes"
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.hset(key, checkpoint.node_id, checkpoint.model_dump_json())
            pipeline.expire(key, self.ttl_seconds)
            await pipeline.execute()

    async def save_run(self, checkpoint: RunCheckpoint) -> None:
        key = f"agent:run:{checkpoint.run_id}:status"
        await self.redis.set(key, checkpoint.model_dump_json(), ex=self.ttl_seconds)

    async def load_run(self, run_id: str) -> RunCheckpoint | None:
        value = await self.redis.get(f"agent:run:{run_id}:status")
        if value is None:
            return None
        return RunCheckpoint.model_validate_json(value)

    async def delete_run(self, run_id: str) -> None:
        await self.redis.delete(
            f"agent:run:{run_id}:status",
            f"agent:run:{run_id}:nodes",
        )


@asynccontextmanager
async def redis_checkpoint_resources(redis_url: str):
    """Create the LangGraph saver and per-node checkpoint store over one Redis deployment."""
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        async with AsyncRedisSaver.from_conn_string(redis_url) as saver:
            await saver.asetup()
            yield saver, RedisCheckpointStore(redis)
    finally:
        await redis.aclose()
