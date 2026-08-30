from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.tools.types import ToolResult


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)
    source_tool: str = Field(min_length=1)
    payload: dict[str, Any]
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def evidence_payload(source_tool: str, result: ToolResult, evidence_id: str) -> dict[str, Any]:
    if source_tool == "search_policy":
        # The ID names one immutable chunk, not a query's ranked result list.
        chunks = [chunk for chunk in result.value.get("chunks", [])
            if isinstance(chunk, dict) and evidence_id == f"policy:{chunk.get('content_hash')}"]
        if len(chunks) != 1:
            raise ValueError("policy evidence ID must identify exactly one source chunk")
        return {"chunks": [deepcopy({key: value for key, value in chunks[0].items() if key != "score"})]}
    return deepcopy(result.value)


class EvidenceLedger:
    """Run-scoped, application-owned snapshots. Models receive IDs but cannot insert records."""

    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}

    def record_tool_result(self, source_tool: str, result: ToolResult) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        for evidence_id in result.evidence_ids:
            existing = self._records.get(evidence_id)
            candidate = EvidenceRecord(
                evidence_id=evidence_id,
                source_tool=source_tool,
                payload=evidence_payload(source_tool, result, evidence_id),
            )
            if existing is not None and (
                existing.source_tool != candidate.source_tool or existing.payload != candidate.payload
            ):
                raise ValueError(f"conflicting evidence snapshot: {evidence_id}")
            self._records[evidence_id] = existing or candidate
            records.append(self._records[evidence_id])
        return records

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self._records.get(evidence_id)

    def snapshot(self) -> dict[str, EvidenceRecord]:
        return dict(self._records)
