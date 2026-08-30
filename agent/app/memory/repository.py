from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

import asyncmy
from asyncmy.cursors import DictCursor
from pydantic import BaseModel, ConfigDict

from app.memory.errors import MemoryDependencyUnavailable, MemoryNotFound
from app.memory.types import (
    ApplyResult,
    MemoryCandidate,
    MemoryOperation,
    MemoryRecord,
    MemorySource,
    MemoryType,
    canonical_json,
    content_hash,
)


class MemoryOutboxEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    memory_id: str
    user_id: int
    payload: dict[str, Any]
    attempt_count: int


class OwnedEpisode(BaseModel):
    """Operator-only index input; never part of a public memory response."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: int
    memory: MemoryRecord


class MemoryRepository(Protocol):
    async def apply(
        self,
        user_id: int,
        candidate: MemoryCandidate,
        *,
        write_key: str,
        conversation_id: str,
        message_id: str,
    ) -> ApplyResult: ...

    async def list_active(
        self, user_id: int, memory_type: MemoryType | None = None, limit: int = 50
    ) -> list[MemoryRecord]: ...

    async def list_stable(self, user_id: int, limit: int = 20) -> list[MemoryRecord]: ...

    async def delete_one(self, user_id: int, memory_id: str) -> tuple[int, int]: ...

    async def delete_all(self, user_id: int) -> tuple[int, int]: ...

    async def active_by_ids(self, user_id: int, memory_ids: Sequence[str]) -> list[MemoryRecord]: ...

    async def pending_outbox(self, limit: int = 50) -> list[MemoryOutboxEvent]: ...

    async def episode_for_index(self, user_id: int, memory_id: str) -> MemoryRecord | None: ...

    async def active_episode_page(self, after_id: str = "", limit: int = 100) -> list[OwnedEpisode]: ...

    async def mark_indexed(self, event_id: str, memory_id: str) -> None: ...

    async def mark_deleted(self, event_id: str, memory_id: str) -> None: ...

    async def mark_skipped(self, event_id: str) -> None: ...

    async def mark_failed(self, event_id: str, memory_id: str, message: str) -> None: ...


class AsyncMySQLMemoryRepository:
    def __init__(self, pool: asyncmy.Pool) -> None:
        self._pool = pool

    @classmethod
    async def create(cls, dsn: str, *, min_size: int = 1, max_size: int = 10) -> "AsyncMySQLMemoryRepository":
        options = _parse_mysql_url(dsn)
        try:
            pool = await asyncmy.create_pool(minsize=min_size, maxsize=max_size, autocommit=False, **options)
        except Exception as exc:
            raise MemoryDependencyUnavailable("memory database connection failed") from exc
        return cls(pool)

    async def aclose(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()

    async def apply(
        self,
        user_id: int,
        candidate: MemoryCandidate,
        *,
        write_key: str,
        conversation_id: str,
        message_id: str,
    ) -> ApplyResult:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM user_memory WHERE user_id=%s AND write_idempotency_key=%s LIMIT 1",
                            (user_id, write_key),
                        )
                        replay = await cursor.fetchone()
                        if replay is not None:
                            await connection.commit()
                            return ApplyResult(memory=_record(replay), applied=False, replayed=True)

                        await cursor.execute(
                            "SELECT * FROM user_memory WHERE user_id=%s AND memory_key=%s "
                            "AND status='ACTIVE' AND active_slot=1 LIMIT 1 FOR UPDATE",
                            (user_id, candidate.memory_key),
                        )
                        active = await cursor.fetchone()
                        if active is not None and active["write_idempotency_key"] == write_key:
                            await connection.commit()
                            return ApplyResult(memory=_record(active), applied=False, replayed=True)
                        if (
                            active is not None
                            and candidate.source is MemorySource.INFERRED
                            and active["source"] == MemorySource.EXPLICIT.value
                        ):
                            await connection.commit()
                            return ApplyResult(
                                memory=_record(active),
                                applied=False,
                                ignored_reason="EXPLICIT_PRECEDENCE",
                            )

                        next_value = _merged_value(active, candidate)
                        if next_value is None:
                            if active is None:
                                await connection.commit()
                                return ApplyResult(applied=False, ignored_reason="NO_ACTIVE_MEMORY")
                            await self._delete_row(cursor, active)
                            await connection.commit()
                            return ApplyResult(applied=True)

                        if active is not None and _json_value(active["value_json"]) == next_value and active["source"] == candidate.source.value:
                            await connection.commit()
                            return ApplyResult(memory=_record(active), applied=False, ignored_reason="NO_CHANGE")

                        memory_id = str(uuid.uuid4())
                        if active is not None:
                            await cursor.execute(
                                "UPDATE user_memory SET status='SUPERSEDED', active_slot=NULL, updated_at=UTC_TIMESTAMP(3) "
                                "WHERE memory_id=%s AND user_id=%s AND status='ACTIVE'",
                                (active["memory_id"], user_id),
                            )
                        vector_status = "PENDING" if candidate.memory_type is MemoryType.EPISODE else "NONE"
                        await cursor.execute(
                            "INSERT INTO user_memory "
                            "(memory_id,user_id,memory_type,memory_key,value_json,source,confidence,evidence_excerpt,"
                            "source_conversation_id,source_message_id,write_idempotency_key,status,active_slot,vector_status) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',1,%s)",
                            (
                                memory_id,
                                user_id,
                                candidate.memory_type.value,
                                candidate.memory_key,
                                canonical_json(next_value),
                                candidate.source.value,
                                candidate.confidence,
                                candidate.evidence_excerpt,
                                conversation_id,
                                message_id,
                                write_key,
                                vector_status,
                            ),
                        )
                        if active is not None:
                            await cursor.execute(
                                "UPDATE user_memory SET superseded_by_memory_id=%s WHERE memory_id=%s AND user_id=%s",
                                (memory_id, active["memory_id"], user_id),
                            )
                        if candidate.memory_type is MemoryType.EPISODE:
                            await _insert_outbox(
                                cursor,
                                event_type="memory.index_requested.v1",
                                memory_id=memory_id,
                                user_id=user_id,
                                memory_type=candidate.memory_type.value,
                                memory_key=candidate.memory_key,
                                value=next_value,
                            )
                        await cursor.execute(
                            "SELECT * FROM user_memory WHERE memory_id=%s AND user_id=%s",
                            (memory_id, user_id),
                        )
                        inserted = await cursor.fetchone()
                    await connection.commit()
                    return ApplyResult(memory=_record(inserted), applied=True)
                except Exception:
                    await connection.rollback()
                    raise
        except (MemoryNotFound, MemoryDependencyUnavailable):
            raise
        except asyncmy.errors.IntegrityError as exc:
            replay = await self._query_all(
                "SELECT * FROM user_memory WHERE user_id=%s AND write_idempotency_key=%s LIMIT 1",
                [user_id, write_key],
            )
            if replay:
                return ApplyResult(memory=_record(replay[0]), applied=False, replayed=True)
            raise MemoryDependencyUnavailable("memory write conflict") from exc
        except Exception as exc:
            raise MemoryDependencyUnavailable("memory write failed") from exc

    async def list_active(
        self, user_id: int, memory_type: MemoryType | None = None, limit: int = 50
    ) -> list[MemoryRecord]:
        query = "SELECT * FROM user_memory WHERE user_id=%s AND status='ACTIVE' AND active_slot=1"
        args: list[Any] = [user_id]
        if memory_type is not None:
            query += " AND memory_type=%s"
            args.append(memory_type.value)
        query += " ORDER BY updated_at DESC, memory_id DESC LIMIT %s"
        args.append(limit)
        rows = await self._query_all(query, args)
        return [_record(row) for row in rows]

    async def list_stable(self, user_id: int, limit: int = 20) -> list[MemoryRecord]:
        rows = await self._query_all(
            "SELECT * FROM user_memory WHERE user_id=%s AND status='ACTIVE' AND active_slot=1 "
            "AND memory_type IN ('PREFERENCE','NEGATIVE_PREFERENCE') "
            "ORDER BY updated_at DESC,memory_id DESC LIMIT %s",
            [user_id, limit],
        )
        return [_record(row) for row in rows]

    async def delete_one(self, user_id: int, memory_id: str) -> tuple[int, int]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM user_memory WHERE user_id=%s AND memory_id=%s "
                            "AND status='ACTIVE' AND active_slot=1 LIMIT 1 FOR UPDATE",
                            (user_id, memory_id),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            raise MemoryNotFound("memory not found")
                        scheduled = await self._delete_row(cursor, row)
                    await connection.commit()
                    return 1, scheduled
                except Exception:
                    await connection.rollback()
                    raise
        except MemoryNotFound:
            raise
        except Exception as exc:
            raise MemoryDependencyUnavailable("memory delete failed") from exc

    async def delete_all(self, user_id: int) -> tuple[int, int]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT * FROM user_memory WHERE user_id=%s AND status='ACTIVE' "
                            "AND active_slot=1 FOR UPDATE",
                            (user_id,),
                        )
                        rows = await cursor.fetchall()
                        scheduled = 0
                        for row in rows:
                            scheduled += await self._delete_row(cursor, row)
                    await connection.commit()
                    return len(rows), scheduled
                except Exception:
                    await connection.rollback()
                    raise
        except Exception as exc:
            raise MemoryDependencyUnavailable("memory delete failed") from exc

    async def active_by_ids(self, user_id: int, memory_ids: Sequence[str]) -> list[MemoryRecord]:
        unique_ids = list(dict.fromkeys(memory_ids))[:10]
        if not unique_ids:
            return []
        placeholders = ",".join(["%s"] * len(unique_ids))
        rows = await self._query_all(
            f"SELECT * FROM user_memory WHERE user_id=%s AND status='ACTIVE' AND active_slot=1 "
            f"AND memory_id IN ({placeholders})",
            [user_id, *unique_ids],
        )
        by_id = {row["memory_id"]: _record(row) for row in rows}
        return [by_id[memory_id] for memory_id in unique_ids if memory_id in by_id]

    async def pending_outbox(self, limit: int = 50) -> list[MemoryOutboxEvent]:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        await cursor.execute(
                            "SELECT event_id,event_type,memory_id,user_id,payload_json,attempt_count "
                            "FROM memory_outbox WHERE status IN ('PENDING','FAILED') "
                            "AND available_at<=UTC_TIMESTAMP(3) ORDER BY created_at,event_id "
                            "LIMIT %s FOR UPDATE SKIP LOCKED",
                            (limit,),
                        )
                        rows = list(await cursor.fetchall())
                        if rows:
                            placeholders = ",".join(["%s"] * len(rows))
                            await cursor.execute(
                                f"UPDATE memory_outbox SET available_at=UTC_TIMESTAMP(3)+INTERVAL 1 MINUTE "
                                f"WHERE event_id IN ({placeholders})",
                                tuple(row["event_id"] for row in rows),
                            )
                    await connection.commit()
                except Exception:
                    await connection.rollback()
                    raise
        except Exception as exc:
            raise MemoryDependencyUnavailable("memory outbox claim failed") from exc
        return [
            MemoryOutboxEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                memory_id=row["memory_id"],
                user_id=int(row["user_id"]),
                payload=_json_value(row["payload_json"]),
                attempt_count=int(row["attempt_count"]),
            )
            for row in rows
        ]

    async def episode_for_index(self, user_id: int, memory_id: str) -> MemoryRecord | None:
        rows = await self._query_all(
            "SELECT * FROM user_memory WHERE user_id=%s AND memory_id=%s AND memory_type='EPISODE' "
            "AND status='ACTIVE' AND active_slot=1 LIMIT 1",
            [user_id, memory_id],
        )
        return _record(rows[0]) if rows else None

    async def active_episode_page(self, after_id: str = "", limit: int = 100) -> list[OwnedEpisode]:
        if not 1 <= limit <= 1000:
            raise ValueError("episode index page size must be between 1 and 1000")
        # Deliberately independent of vector_status and outbox publication status:
        # a new embedding space must also include previously indexed episodes.
        rows = await self._query_all(
            "SELECT * FROM user_memory WHERE memory_type='EPISODE' AND status='ACTIVE' "
            "AND active_slot=1 AND memory_id>%s ORDER BY memory_id LIMIT %s",
            [after_id, limit],
        )
        return [OwnedEpisode(user_id=int(row["user_id"]), memory=_record(row)) for row in rows]

    async def mark_indexed(self, event_id: str, memory_id: str) -> None:
        await self._finish_event(event_id, memory_id, "INDEXED")

    async def mark_deleted(self, event_id: str, memory_id: str) -> None:
        await self._finish_event(event_id, memory_id, "DELETED")

    async def mark_skipped(self, event_id: str) -> None:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "UPDATE memory_outbox SET status='PUBLISHED',published_at=UTC_TIMESTAMP(3),last_error=NULL "
                        "WHERE event_id=%s AND status<>'PUBLISHED'",
                        (event_id,),
                    )
                await connection.commit()
        except Exception as exc:
            raise MemoryDependencyUnavailable("memory outbox skip failed") from exc

    async def mark_failed(self, event_id: str, memory_id: str, message: str) -> None:
        safe_message = message[:1024]
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor() as cursor:
                        await cursor.execute(
                            "UPDATE memory_outbox SET status='FAILED',attempt_count=attempt_count+1,last_error=%s,"
                            "available_at=UTC_TIMESTAMP(3)+INTERVAL 1 MINUTE "
                            "WHERE event_id=%s AND status<>'PUBLISHED'",
                            (safe_message, event_id),
                        )
                        if cursor.rowcount:
                            await cursor.execute(
                                "UPDATE user_memory SET vector_status='FAILED',updated_at=UTC_TIMESTAMP(3) "
                                "WHERE memory_id=%s",
                                (memory_id,),
                            )
                    await connection.commit()
                except Exception:
                    await connection.rollback()
                    raise
        except Exception as exc:
            raise MemoryDependencyUnavailable("memory outbox failure state update failed") from exc

    async def _finish_event(self, event_id: str, memory_id: str, vector_status: str) -> None:
        try:
            async with self._pool.acquire() as connection:
                await connection.begin()
                try:
                    async with connection.cursor() as cursor:
                        await cursor.execute(
                            "UPDATE memory_outbox SET status='PUBLISHED',published_at=UTC_TIMESTAMP(3),last_error=NULL "
                            "WHERE event_id=%s AND status<>'PUBLISHED'",
                            (event_id,),
                        )
                        if cursor.rowcount:
                            await cursor.execute(
                                "UPDATE user_memory SET vector_status=%s,updated_at=UTC_TIMESTAMP(3) WHERE memory_id=%s",
                                (vector_status, memory_id),
                            )
                    await connection.commit()
                except Exception:
                    await connection.rollback()
                    raise
        except Exception as exc:
            raise MemoryDependencyUnavailable("memory outbox completion failed") from exc

    async def _delete_row(self, cursor: Any, row: dict[str, Any]) -> int:
        schedule_vector = row["memory_type"] == MemoryType.EPISODE.value and row["vector_status"] in {
            "PENDING",
            "INDEXED",
            "FAILED",
        }
        vector_status = "DELETE_PENDING" if schedule_vector else row["vector_status"]
        await cursor.execute(
            "UPDATE user_memory SET status='DELETED',active_slot=NULL,deleted_at=UTC_TIMESTAMP(3),"
            "vector_status=%s,updated_at=UTC_TIMESTAMP(3) WHERE memory_id=%s AND user_id=%s AND status='ACTIVE'",
            (vector_status, row["memory_id"], row["user_id"]),
        )
        if schedule_vector:
            await _insert_outbox(
                cursor,
                event_type="memory.delete_requested.v1",
                memory_id=row["memory_id"],
                user_id=int(row["user_id"]),
                memory_type=row["memory_type"],
                memory_key=row["memory_key"],
                value=_json_value(row["value_json"]),
            )
        return int(schedule_vector)

    async def _query_all(self, query: str, args: Sequence[Any]) -> list[dict[str, Any]]:
        try:
            async with self._pool.acquire() as connection:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(query, args)
                    return list(await cursor.fetchall())
        except Exception as exc:
            raise MemoryDependencyUnavailable("memory read failed") from exc


async def _insert_outbox(
    cursor: Any,
    *,
    event_type: str,
    memory_id: str,
    user_id: int,
    memory_type: str,
    memory_key: str,
    value: dict[str, Any],
) -> None:
    payload = {
        "memory_id": memory_id,
        "user_id": user_id,
        "memory_type": memory_type,
        "memory_key": memory_key,
        "content_hash": content_hash(value),
    }
    await cursor.execute(
        "INSERT IGNORE INTO memory_outbox "
        "(event_id,event_type,memory_id,user_id,payload_json,status) VALUES (%s,%s,%s,%s,%s,'PENDING')",
        (str(uuid.uuid4()), event_type, memory_id, user_id, canonical_json(payload)),
    )


def _merged_value(active: dict[str, Any] | None, candidate: MemoryCandidate) -> dict[str, Any] | None:
    if candidate.operation is MemoryOperation.SET or candidate.memory_type is MemoryType.EPISODE:
        return candidate.value
    current = [] if active is None else list(_json_value(active["value_json"])["items"])
    incoming = list(candidate.value["items"])
    if candidate.operation is MemoryOperation.ADD:
        seen = {item.casefold() for item in current}
        for item in incoming:
            folded = item.casefold()
            if folded not in seen:
                seen.add(folded)
                current.append(item)
    else:
        removed = {item.casefold() for item in incoming}
        current = [item for item in current if item.casefold() not in removed]
    return {"items": current} if current else None


def _record(row: dict[str, Any] | None) -> MemoryRecord:
    if row is None:
        raise MemoryDependencyUnavailable("memory row was not returned")
    created_at = row["created_at"]
    updated_at = row["updated_at"]
    if isinstance(created_at, datetime) and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    confidence = row["confidence"]
    if isinstance(confidence, Decimal):
        confidence = float(confidence)
    return MemoryRecord(
        memory_id=row["memory_id"],
        memory_type=row["memory_type"],
        memory_key=row["memory_key"],
        value=_json_value(row["value_json"]),
        source=row["source"],
        confidence=confidence,
        created_at=created_at,
        updated_at=updated_at,
    )


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise MemoryDependencyUnavailable("memory JSON has an invalid shape")


def _parse_mysql_url(dsn: str) -> dict[str, Any]:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"mysql", "mysql+asyncmy"} or not parsed.username or not parsed.hostname:
        raise MemoryDependencyUnavailable("AGENT_MYSQL_DSN must be a MySQL URL")
    database = parsed.path.lstrip("/")
    if not database:
        raise MemoryDependencyUnavailable("AGENT_MYSQL_DSN must select a database")
    query = parse_qs(parsed.query)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password or ""),
        "db": unquote(database),
        "charset": query.get("charset", ["utf8mb4"])[0],
    }


class MemoryRepositoryRuntime:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._repository: AsyncMySQLMemoryRepository | None = None

    async def startup(self) -> None:
        if self._repository is None:
            self._repository = await AsyncMySQLMemoryRepository.create(self._dsn)

    async def aclose(self) -> None:
        if self._repository is not None:
            await self._repository.aclose()
            self._repository = None

    def __getattr__(self, name: str):
        if self._repository is None:
            raise MemoryDependencyUnavailable("memory repository is not started")
        return getattr(self._repository, name)
