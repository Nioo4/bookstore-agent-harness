from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.api.app import AppDependencies, create_app
from app.api.auth import AuthUser
from app.models.fake import FakeModelProvider
from app.tools.registry import ToolRegistry
from app.trace.manifests import VersionRegistry
from app.trace.repository import InMemoryTraceRepository
from app.trace.service import TraceService
from app.trace.types import CostStatus, ReplayMode, RunKind, RunStatus, TraceCost, TraceRun, TraceStatus, WorkflowType


NOW = datetime(2026, 9, 25, tzinfo=UTC)


class TokenVerifier:
    def verify_authorization(self, authorization):
        if authorization == "Bearer user-7":
            return "jwt", AuthUser(user_id=7, email="user@example.test", email_verified=True)
        if authorization == "Bearer user-8":
            return "jwt", AuthUser(user_id=8, email="other@example.test", email_verified=True)
        if authorization:
            raise ValueError("invalid")
        return None, None


def versions() -> VersionRegistry:
    from pathlib import Path

    root = Path(__file__).parents[1]
    return VersionRegistry.load(
        root=root,
        prompt_manifest_path=root / "prompts/manifest.yaml",
        model_manifest_path=root / "config/model-profiles.yaml",
        pricing_manifest_path=root / "pricing/pricing-v1.json",
    )


def run(run_id: str, *, user_id: int = 7, kind: RunKind = RunKind.LIVE) -> TraceRun:
    registry = versions()
    return TraceRun(
        run_id=run_id,
        user_id=user_id,
        conversation_id="conversation-7",
        run_kind=kind,
        workflow_type=WorkflowType.CHAT,
        status=RunStatus.COMPLETED,
        trace_status=TraceStatus.COMPLETE,
        replay_mode=ReplayMode.NONE,
        replayable=False,
        input={"content": "请推荐一本书"},
        dag=None,
        final_answer="这是一个测试回答",
        model_profile="default",
        model_manifest_hash=registry.model_profile_hash("default"),
        prompt_set_version="v1",
        prompt_manifest_hash=registry.prompt_set_hash("v1"),
        tool_registry_version=registry.tool_registry_versions["live-v2"],
        harness_version="0.1.0+test",
        cost=TraceCost(status=CostStatus.UNAVAILABLE),
        retention_expires_at=NOW + timedelta(days=30),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
    )


async def client_and_repository():
    repository = InMemoryTraceRepository()
    await repository.create_run(run(str(uuid.uuid4())))
    # A historical replay row must not become a public v2 trace or replay API.
    await repository.create_run(run(str(uuid.uuid4()), kind=RunKind.REPLAY))
    service = TraceService(
        repository,
        versions(),
        None,
        tool_registry_version=versions().tool_registry_versions["live-v2"],
        harness_version="0.1.0+test",
    )
    app = create_app(
        AppDependencies(
            model_provider=FakeModelProvider([]),
            tools=ToolRegistry(),
            token_verifier=TokenVerifier(),  # type: ignore[arg-type]
            trace_service=service,
        )
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), repository


@pytest.mark.asyncio
async def test_v2_trace_lists_only_live_and_has_no_replay_route() -> None:
    client, _ = await client_and_repository()
    async with client:
        listing = await client.get("/api/traces", headers={"Authorization": "Bearer user-7"})
        replay_route = await client.post(
            "/api/traces/synthetic/replays",
            headers={"Authorization": "Bearer user-7", "Idempotency-Key": "not-used"},
            json={},
        )
    assert listing.status_code == 200
    assert len(listing.json()["runs"]) == 1
    assert listing.json()["runs"][0]["run_kind"] == "LIVE"
    assert replay_route.status_code == 404


@pytest.mark.asyncio
async def test_v2_trace_detail_delete_and_user_isolation() -> None:
    client, repository = await client_and_repository()
    live_id = next(item.run_id for item in repository.runs.values() if item.run_kind is RunKind.LIVE)
    replay_id = next(item.run_id for item in repository.runs.values() if item.run_kind is RunKind.REPLAY)
    async with client:
        cross_user = await client.get(f"/api/traces/{live_id}", headers={"Authorization": "Bearer user-8"})
        detail = await client.get(f"/api/traces/{live_id}", headers={"Authorization": "Bearer user-7"})
        replay_detail = await client.get(f"/api/traces/{replay_id}", headers={"Authorization": "Bearer user-7"})
        deleted = await client.delete(f"/api/traces/{live_id}", headers={"Authorization": "Bearer user-7"})
        hidden = await client.get(f"/api/traces/{live_id}", headers={"Authorization": "Bearer user-7"})
    assert cross_user.status_code == 404
    assert detail.status_code == 200
    assert detail.json()["run"]["dag"] is None
    assert detail.json()["run"]["replayable"] is False
    assert replay_detail.status_code == 404
    assert deleted.status_code == 200
    assert hidden.status_code == 404
