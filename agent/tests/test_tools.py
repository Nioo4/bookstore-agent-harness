from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.harness.budget import ToolCallBudget
from app.harness.errors import ToolBudgetExceeded
from app.models.configuration import ManifestSettings
from app.models.factory import load_versions
from app.tools.business import build_business_tool_registry
from app.tools.errors import ToolError
from app.tools.registry import ToolRegistry
from app.tools.schemas import (
    CreateOrderInput,
    GetBookInput,
    GetOrderInput,
    ListMyOrdersInput,
    PrepareOrderInput,
    PrepareOrderRequest,
    SearchBooksInput,
)
from app.tools.types import RiskLevel, ToolContext, ToolResult, ToolSpec


class FakeCatalogClient:
    async def search_books(self, request: SearchBooksInput, timeout: float) -> dict[str, Any]:
        return {
            "books": [{"book_id": 7, "version": 3, "title": "Agent Systems"}],
            "next_cursor": "",
        }

    async def get_book(self, request: GetBookInput, timeout: float) -> dict[str, Any]:
        return {"book_id": request.book_id or 7, "version": 3, "title": "Agent Systems"}


class FakeOrderClient:
    def __init__(self) -> None:
        self.last_prepare: PrepareOrderRequest | None = None

    async def prepare_order(self, request: PrepareOrderRequest, access_token: str, conversation_id: str, timeout: float) -> dict[str, Any]:
        self.last_prepare = request
        return {
            "confirmation_token": "x" * 43,
            "expires_at": "2026-08-25T12:10:00Z",
            "items": [],
            "total_amount": 1000,
            "currency": "CNY",
        }

    async def create_order(self, request: CreateOrderInput, access_token: str, conversation_id: str, timeout: float) -> dict[str, Any]:
        order = {"order_id": 99, "status": "PLACED"} if request.decision == "APPROVE" else None
        return {"confirmation_id": "action-1", "decision": request.decision,
                "status": "COMPLETED" if order else "CANCELLED", "order": order, "replayed": False}

    async def get_order(self, request: GetOrderInput, access_token: str, timeout: float) -> dict[str, Any]:
        return {"order_id": request.order_id, "status": "PLACED"}

    async def list_my_orders(self, request: ListMyOrdersInput, access_token: str, timeout: float) -> dict[str, Any]:
        return {"orders": [{"order_id": 99}], "next_cursor": ""}


class FakeKnowledgeService:
    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return [
            {
                "content_hash": "abc123",
                "source_path": "knowledge/approved.md",
                "document_title": "Approved Guide",
                "section": "Returns",
                "version": "v1",
                "effective_date": "2026-08-26",
                "content": "Approved text",
                "score": 0.9,
            }
        ][:limit]


def test_live_v2_tool_registry_matches_its_frozen_version_artifact() -> None:
    registry = build_business_tool_registry(
        FakeCatalogClient(),
        FakeOrderClient(),
        FakeKnowledgeService(),  # type: ignore[arg-type]
        FakeKnowledgeService(),  # type: ignore[arg-type]
    )
    versions = load_versions(ManifestSettings(_env_file=None))
    assert registry.version == versions.tool_registry_versions["live-v2"]


def context(*, user_confirmed: bool = False, email_verified: bool = True) -> ToolContext:
    return ToolContext(
        trace_id="trace-1",
        conversation_id="conversation-1",
        access_token="signed-jwt",
        authenticated_user_id=42,
        email_verified=email_verified,
        user_confirmed=user_confirmed,
    )


def test_every_registered_business_tool_has_complete_protocol_metadata() -> None:
    registry = build_business_tool_registry(FakeCatalogClient(), FakeOrderClient())

    manifests = registry.manifests()

    assert {manifest["name"] for manifest in manifests} == {
        "search_books",
        "get_book",
        "prepare_order",
        "create_order",
        "get_order",
        "list_my_orders",
    }
    for manifest in manifests:
        assert manifest["input_schema"]["type"] == "object"
        assert manifest["risk"] in {risk.value for risk in RiskLevel}
        assert manifest["timeout_seconds"] > 0
        assert manifest["max_retries"] >= 0
        assert isinstance(manifest["idempotent"], bool)
        assert manifest["permissions"]
        assert manifest["error_codes"]


@pytest.mark.asyncio
async def test_knowledge_agent_can_only_retrieve_cited_policy_chunks() -> None:
    registry = build_business_tool_registry(FakeCatalogClient(), FakeOrderClient(), FakeKnowledgeService())  # type: ignore[arg-type]

    result = await registry.invoke(
        "search_policy",
        {"query": "returns", "limit": 5},
        agent_name="single_agent",
        agent_tools=frozenset({"search_policy"}),
        permissions=frozenset({"policy:read"}),
        context=context(),
    )

    assert result.evidence_ids == ["policy:abc123"]
    assert result.value["chunks"][0]["source_path"] == "knowledge/approved.md"


@pytest.mark.asyncio
async def test_registry_enforces_agent_allowlist_and_permissions() -> None:
    registry = build_business_tool_registry(FakeCatalogClient(), FakeOrderClient())

    with pytest.raises(ToolError) as error:
        await registry.invoke(
            "search_books",
            {"q": "agents"},
            agent_name="single_agent",
            agent_tools=frozenset(),
            permissions=frozenset(),
            context=context(),
        )

    assert error.value.code == "TOOL_PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_prepare_rejects_forged_identity_and_uses_authenticated_context() -> None:
    orders = FakeOrderClient()
    registry = build_business_tool_registry(FakeCatalogClient(), orders)

    class EventSink:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        async def emit(self, event_type: str, data: dict[str, Any]) -> None:
            self.events.append((event_type, data))

    sink = EventSink()
    with pytest.raises(ToolError) as forged:
        await registry.invoke(
            "prepare_order",
            {
                "user_id": 999999,
                "items": [{"book_id": 7, "quantity": 1}],
                "idempotency_key": "idem-key-123",
            },
            agent_name="single_agent",
            agent_tools=frozenset({"prepare_order"}),
            permissions=frozenset({"order:prepare"}),
            context=context().model_copy(update={"event_sink": sink}),
        )
    assert forged.value.code == "INVALID_TOOL_ARGUMENTS"

    result = await registry.invoke(
        "prepare_order",
        {"items": [{"book_id": 7, "quantity": 1}]},
        agent_name="single_agent",
        agent_tools=frozenset({"prepare_order"}),
        permissions=frozenset({"order:prepare"}),
        context=context().model_copy(update={"event_sink": sink}),
    )

    assert result.value["total_amount"] == 1000
    assert orders.last_prepare is not None
    assert not hasattr(orders.last_prepare, "user_id")
    assert "confirmation_token" not in result.value
    assert result.value["confirmation_required"] is True
    internal_event = next(event for event in sink.events if event[0] == "confirmation.prepared.internal")
    assert internal_event == ("confirmation.prepared.internal", {
        "confirmation_token": "x" * 43,
        "expires_at": "2026-08-25T12:10:00Z",
        "items": [],
            "total_amount": 1000,
            "currency": "CNY",
            "action_type": "CREATE_ORDER",
            "risk": "COMMIT",
        })


@pytest.mark.asyncio
async def test_prepare_idempotency_key_is_server_derived_and_stable_for_retries() -> None:
    orders = FakeOrderClient()
    registry = build_business_tool_registry(FakeCatalogClient(), orders)

    class EventSink:
        async def emit(self, _event_type: str, _data: dict[str, Any]) -> None:
            return None

    async def prepare(trace_id: str) -> str:
        await registry.invoke(
            "prepare_order",
            {
                "items": [{"book_id": 7, "quantity": 1}],
            },
            agent_name="single_agent",
            agent_tools=frozenset({"prepare_order"}),
            permissions=frozenset({"order:prepare"}),
            context=context().model_copy(update={"trace_id": trace_id, "event_sink": EventSink()}),
        )
        assert orders.last_prepare is not None
        return orders.last_prepare.idempotency_key

    same_run_first = await prepare("trace-a")
    same_run_retry = await prepare("trace-a")
    next_run = await prepare("trace-b")

    assert same_run_first.startswith("prepare-")
    assert same_run_retry == same_run_first
    assert next_run != same_run_first


@pytest.mark.asyncio
async def test_commit_requires_policy_authorization() -> None:
    registry = build_business_tool_registry(FakeCatalogClient(), FakeOrderClient())
    arguments = {
        "confirmation_token": "x" * 43,
        "decision": "APPROVE",
    }
    forged_arguments = {**arguments, "user_id": 999999, "policy_approved": True}

    with pytest.raises(ToolError) as forged:
        await registry.invoke(
            "create_order",
            forged_arguments,
            agent_name="single_agent",
            agent_tools=frozenset({"create_order"}),
            permissions=frozenset({"order:commit"}),
            context=context(user_confirmed=False),
        )
    assert forged.value.code == "INVALID_TOOL_ARGUMENTS"

    with pytest.raises(ToolError) as error:
        await registry.invoke(
            "create_order",
            arguments,
            agent_name="single_agent",
            agent_tools=frozenset({"create_order"}),
            permissions=frozenset({"order:commit"}),
            context=context(user_confirmed=False),
        )
    assert error.value.code == "CONFIRMATION_REQUIRED"

    result = await registry.invoke(
        "create_order",
        arguments,
        agent_name="single_agent",
        agent_tools=frozenset({"create_order"}),
        permissions=frozenset({"order:commit"}),
        context=context(user_confirmed=True),
    )
    assert result.value["order"]["order_id"] == 99


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@pytest.mark.asyncio
async def test_registry_retries_only_retryable_idempotent_calls() -> None:
    calls = 0

    async def flaky(_: BaseModel, __: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ToolError("TEMPORARY", "try again", retryable=True)
        return ToolResult(value={"ok": True})

    registry = ToolRegistry(
        [
            ToolSpec(
                name="flaky_read",
                description="Test retry behavior.",
                input_model=EmptyInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=1,
                idempotent=True,
                required_permissions=frozenset({"test:read"}),
                error_codes=frozenset({"TEMPORARY"}),
                executor=flaky,
            )
        ]
    )

    result = await registry.invoke(
        "flaky_read",
        {},
        agent_name="test",
        agent_tools=frozenset({"flaky_read"}),
        permissions=frozenset({"test:read"}),
        context=context(),
    )

    assert result.value == {"ok": True}
    assert calls == 2


@pytest.mark.asyncio
async def test_guest_tool_policy_rejects_any_read_tool_outside_fixed_public_allowlist() -> None:
    calls = 0

    async def forbidden(_: BaseModel, __: ToolContext) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(value={"secret": True})

    registry = ToolRegistry(
        [
            ToolSpec(
                name="internal_read",
                description="An authenticated internal read.",
                input_model=EmptyInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"internal:read"}),
                error_codes=frozenset({"AUTH_REQUIRED"}),
                executor=forbidden,
            )
        ]
    )
    guest = ToolContext(
        trace_id="temporary-execution-id",
        conversation_id="untrusted-request-correlation",
        guest_mode=True,
    )

    with pytest.raises(ToolError) as raised:
        await registry.invoke(
            "internal_read",
            {},
            agent_name="internal",
            agent_tools=frozenset({"internal_read"}),
            permissions=frozenset({"internal:read"}),
            context=guest,
        )

    assert raised.value.code == "AUTH_REQUIRED"
    assert calls == 0


@pytest.mark.asyncio
async def test_tool_registry_consumes_shared_run_budget_before_business_call() -> None:
    registry = build_business_tool_registry(FakeCatalogClient(), FakeOrderClient())
    budget = ToolCallBudget(1)
    tool_context = context().model_copy(update={"tool_budget": budget})

    await registry.invoke(
        "search_books",
        {"q": "agents"},
        agent_name="single_agent",
        agent_tools=frozenset({"search_books"}),
        permissions=frozenset({"catalog:read"}),
        context=tool_context,
    )
    with pytest.raises(ToolBudgetExceeded):
        await registry.invoke(
            "search_books",
            {"q": "agents"},
            agent_name="single_agent",
            agent_tools=frozenset({"search_books"}),
            permissions=frozenset({"catalog:read"}),
            context=tool_context,
        )
