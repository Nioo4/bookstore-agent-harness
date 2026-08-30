from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(StrEnum):
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class CheckpointStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class InventoryWatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=128)
    book_id: int = Field(gt=0)
    desired_quantity: int = Field(default=1, ge=1, le=99)
    expires_at: datetime | None = None


class AgentTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    user_id: int
    conversation_id: str
    run_id: str
    task_type: str = "INVENTORY_WATCH"
    status: TaskStatus
    book_id: int
    book_title: str
    desired_quantity: int
    last_seen_inventory_version: int
    last_seen_available_stock: int
    trigger_event_id: str | None = None
    create_idempotency_key: str
    request_hash: bytes
    checkpoint_status: CheckpointStatus
    expires_at: datetime
    triggered_at: datetime | None = None
    completed_at: datetime | None = None
    expired_at: datetime | None = None
    cancelled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status.value,
            "conversation_id": self.conversation_id,
            "book_id": self.book_id,
            "book_title": self.book_title,
            "desired_quantity": self.desired_quantity,
            "last_seen_available_stock": self.last_seen_available_stock,
            "last_seen_inventory_version": self.last_seen_inventory_version,
            "expires_at": _utc_text(self.expires_at),
            "triggered_at": _utc_text(self.triggered_at),
            "completed_at": _utc_text(self.completed_at),
            "created_at": _utc_text(self.created_at),
            "updated_at": _utc_text(self.updated_at),
        }


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    notification_id: str
    user_id: int
    task_id: str
    notification_type: str = "INVENTORY_AVAILABLE"
    status: str
    title: str
    body: str
    payload: dict[str, Any]
    read_at: datetime | None = None
    created_at: datetime

    def public(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "type": self.notification_type,
            "status": self.status,
            "title": self.title,
            "body": self.body,
            "task_id": self.task_id,
            "payload": self.payload,
            "created_at": _utc_text(self.created_at),
            "read_at": _utc_text(self.read_at),
        }


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
