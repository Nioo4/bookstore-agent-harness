from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from app.trace.errors import TraceError
from app.trace.manifests import VersionRegistry
from app.trace.repository import TraceRepository
from app.trace.types import (
    RunKind,
    RunStatus,
    TraceRun,
)


class TraceService:
    def __init__(
        self,
        repository: TraceRepository,
        versions: VersionRegistry,
        _legacy_rate_limiter: object | None = None,
        *,
        tool_registry_version: str,
        harness_version: str,
        retention_days: int = 30,
    ) -> None:
        self.repository = repository
        self.versions = versions
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
            parsed_kind = RunKind(kind) if kind is not None else RunKind.LIVE
            parsed_status = RunStatus(status) if status is not None else None
        except ValueError as exc:
            raise TraceError("TRACE_REQUEST_INVALID", "invalid trace filter") from exc
        if parsed_kind is not RunKind.LIVE:
            raise TraceError("TRACE_REQUEST_INVALID", "only live traces are public")
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
        return [run.public_summary() for run in runs if run.run_kind is RunKind.LIVE and run.user_id == user_id]

    async def detail(
        self, user_id: int, run_id: str, *, step_cursor: str | None, step_limit: int
    ) -> dict[str, object]:
        if not 1 <= step_limit <= 200:
            raise TraceError("TRACE_REQUEST_INVALID", "step limit must be between 1 and 200")
        after = decode_step_cursor(step_cursor) if step_cursor else None
        run = await self.repository.get_owned(user_id, run_id)
        if run.run_kind is not RunKind.LIVE:
            raise TraceError("TRACE_NOT_FOUND", "trace not found")
        steps = await self.repository.list_steps(run_id, after, step_limit + 1)
        page = steps[:step_limit]
        next_cursor = encode_step_cursor(page[-1].sequence_no) if len(steps) > step_limit else None
        evidence = await self.repository.list_evidence(run_id)
        public_run = run.public_detail()
        public_run.update((await self.public_summaries(user_id, [run]))[0])
        return {
            "run": public_run,
            "steps": [step.public() for step in page],
            "evidence": [item.public() for item in evidence],
            "next_step_cursor": next_cursor,
        }

    async def delete(self, user_id: int, run_id: str, now: datetime | None = None) -> tuple[int, int]:
        run = await self.repository.get_owned(user_id, run_id)
        if run.run_kind is not RunKind.LIVE:
            raise TraceError("TRACE_NOT_FOUND", "trace not found")
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
