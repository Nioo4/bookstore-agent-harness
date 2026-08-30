"""Isolated component storage using production merge rules, not prewritten outcomes.

Only U/D scenarios use this implementation. It cannot prove SQL locks, durability,
outbox delivery or Qdrant cleanup; those remain the real-dependency matrix tests.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app.memory.errors import MemoryNotFound
from app.memory.repository import _merged_value
from app.memory.types import ApplyResult, MemoryRecord, MemorySource, MemoryType


class ComponentMemoryRepository:
    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.rows = {}
        self.write_ids = {}
        self._lock = asyncio.Lock()
        self._sequence = 0

    def _time(self):
        self._sequence += 1
        return datetime(2026,8,28,tzinfo=UTC) + timedelta(milliseconds=self._sequence)

    async def apply(self, user_id, candidate, *, write_key, conversation_id, message_id):
        async with self._lock:
            replay = self.write_ids.get((user_id, write_key))
            if replay:
                return ApplyResult(memory=self.rows[replay]["memory"].model_copy(deep=True), applied=False, replayed=True)
            active = next((row for row in self.rows.values() if row["user_id"] == user_id and row["status"] == "ACTIVE" and row["memory"].memory_key == candidate.memory_key), None)
            if active and candidate.source == MemorySource.INFERRED and active["memory"].source == MemorySource.EXPLICIT:
                return ApplyResult(memory=active["memory"].model_copy(deep=True), applied=False, ignored_reason="EXPLICIT_PRECEDENCE")
            value = _merged_value({"value_json":active["memory"].value} if active else None, candidate)
            if value is None:
                if active:
                    active["status"] = "DELETED"
                return ApplyResult(applied=active is not None, ignored_reason=None if active else "NO_ACTIVE_MEMORY")
            if active and active["memory"].value == value and active["memory"].source == candidate.source:
                return ApplyResult(memory=active["memory"].model_copy(deep=True), applied=False, ignored_reason="NO_CHANGE")
            now = self._time()
            memory_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.namespace}/{user_id}/{self._sequence}"))
            record = MemoryRecord(memory_id=memory_id, memory_type=candidate.memory_type, memory_key=candidate.memory_key,
                value=value, source=candidate.source, confidence=candidate.confidence, created_at=now, updated_at=now)
            if active: active["status"] = "SUPERSEDED"
            self.rows[memory_id] = {"user_id":user_id, "memory":record, "status":"ACTIVE"}
            self.write_ids[user_id, write_key] = memory_id
            return ApplyResult(memory=record.model_copy(deep=True), applied=True)

    async def list_active(self, user_id, memory_type=None, limit=50):
        rows = [row["memory"] for row in self.rows.values() if row["user_id"] == user_id and row["status"] == "ACTIVE" and (memory_type is None or row["memory"].memory_type == memory_type)]
        return [record.model_copy(deep=True) for record in sorted(rows, key=lambda record:(record.updated_at,record.memory_id), reverse=True)[:limit]]

    async def list_stable(self, user_id, limit=20):
        return [record for record in await self.list_active(user_id, limit=1000) if record.memory_type != MemoryType.EPISODE][:limit]

    async def active_by_ids(self, user_id, memory_ids):
        return [self.rows[key]["memory"].model_copy(deep=True) for key in list(dict.fromkeys(memory_ids))[:10]
            if key in self.rows and self.rows[key]["status"] == "ACTIVE" and self.rows[key]["user_id"] == user_id]

    async def delete_one(self, user_id, memory_id):
        row = self.rows.get(memory_id)
        if row is None or row["user_id"] != user_id or row["status"] != "ACTIVE": raise MemoryNotFound()
        row["status"] = "DELETED"
        return 1, int(row["memory"].memory_type == MemoryType.EPISODE)

    async def delete_all(self, user_id):
        active = await self.list_active(user_id, limit=1000)
        scheduled = 0
        for record in active:
            _, count = await self.delete_one(user_id, record.memory_id)
            scheduled += count
        return len(active), scheduled
