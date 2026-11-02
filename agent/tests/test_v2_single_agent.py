from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict

from app.api.chat import ChatEvent, ChatService, QueueEventSink, _watch_catalog_read
from app.agents.intent import InventoryWatchIntent
from app.api.traces import build_trace_router
from app.harness.single_agent import SingleAgentError, SingleAgentLimits, SingleAgentRuntime, SingleAgentToolCall
from app.harness.validator import validate_final_action
from app.agents.verification import UserConstraints
from app.models.fake import FakeModelProvider
from app.models.provider import ModelProvider, ModelRequest, ModelResponse
from app.tools.schemas import GetBookInput, SearchBooksInput
from app.tools.registry import ToolRegistry
from app.tools.types import RiskLevel, ToolContext, ToolResult, ToolSpec


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    book_ids: list[int]


def registry(*, include_prepare: bool = False) -> ToolRegistry:
    async def execute(arguments, context):
        return ToolResult(value={"chunks": [{"content": "x" * 20000, "document_type": "POLICY"}]}, evidence_ids=["policy:x"])

    specs = [
        ToolSpec(
            name="search_policy",
            description="read policy",
            input_model=EmptyInput,
            risk=RiskLevel.READ_ONLY,
            timeout_seconds=1,
            max_retries=0,
            idempotent=True,
            required_permissions=frozenset({"policy:read"}),
            error_codes=frozenset({"TOOL_TIMEOUT"}),
            executor=execute,
        ),
        ToolSpec(
            name="get_book",
            description="read book",
            input_model=EmptyInput,
            risk=RiskLevel.READ_ONLY,
            timeout_seconds=1,
            max_retries=0,
            idempotent=True,
            required_permissions=frozenset({"catalog:read"}),
            error_codes=frozenset({"TOOL_TIMEOUT"}),
            executor=execute,
        ),
    ]
    if include_prepare:
        specs.append(
            ToolSpec(
                name="prepare_order",
                description="preview order",
                input_model=EmptyInput,
                risk=RiskLevel.PREPARE,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"order:prepare"}),
                error_codes=frozenset({"TOOL_TIMEOUT"}),
                executor=execute,
            )
        )
    return ToolRegistry(specs)


def context() -> ToolContext:
    return ToolContext(trace_id="trace", conversation_id="conversation", guest_mode=True)


@pytest.mark.asyncio
async def test_single_agent_strict_loop_preserves_parallel_model_order_and_caps_rag() -> None:
    provider = FakeModelProvider(
        [
            ModelResponse(
                content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"a","name":"search_policy","arguments":{}},{"call_id":"b","name":"get_book","arguments":{}}]}',
                model="fake",
            ),
            ModelResponse(
                content='{"action":"FINAL","answer":"有依据的回答","recommendations":[],"policy_claims":[],"addressed_question_ids":["q1"]}',
                model="fake",
            ),
        ]
    )
    runtime = SingleAgentRuntime(provider, "fake", registry(), system_prompt="system")

    result = await runtime.run(
        "规则是什么？",
        context=context(),
        context_items=[
            {"kind": "capabilities", "guest": True, "authenticated": False, "email_verified": False},
            {"kind": "preference", "key": "PREFERRED_LANGUAGE", "value": "zh-CN"},
            {"kind": "history", "role": "user", "content": "此前问题"},
        ],
    )

    assert result.answer == "有依据的回答"
    assert [row["call_id"] for row in result.tool_results] == ["a", "b"]
    assert len(provider.requests) == 2
    assert all(request.tools == [] for request in provider.requests)
    assert [message.role for message in provider.requests[0].messages[:7]] == ["system"] * 6 + ["user"]
    assert "ALLOWED_TOOLS_JSON=" in provider.requests[0].messages[1].content
    assert provider.requests[1].messages[-1].role == "system"
    assert provider.requests[1].messages[-1].content.startswith("LOCAL_TOOL_RESULT_JSON=")
    assert "EFFECTIVE_REQUEST_JSON=" in provider.requests[0].messages[5].content
    assert len(provider.requests[1].messages[-1].content) < 65_536


@pytest.mark.asyncio
async def test_identical_read_tool_call_is_reused_within_one_run() -> None:
    executions = 0

    async def execute(arguments, context):
        nonlocal executions
        executions += 1
        return ToolResult(value={"books": [{"book_id": 1, "title": "One"}]}, evidence_ids=["catalog:1"])

    search_registry = ToolRegistry(
        [
            ToolSpec(
                name="search_books",
                description="search catalog",
                input_model=SearchBooksInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"catalog:read"}),
                error_codes=frozenset({"TOOL_TIMEOUT"}),
                executor=execute,
            )
        ]
    )
    provider = FakeModelProvider(
        [
            ModelResponse(content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"s1","name":"search_books","arguments":{"q":"distributed"}}]}', model="fake"),
            ModelResponse(content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"s2","name":"search_books","arguments":{"q":"distributed"}}]}', model="fake"),
            ModelResponse(content='{"action":"FINAL","answer":"done"}', model="fake"),
        ]
    )

    result = await SingleAgentRuntime(provider, "fake", search_registry).run(
        "recommend",
        context=context(),
        allowed_tools=frozenset({"search_books"}),
        permissions=frozenset({"catalog:read"}),
    )

    assert result.answer == "done"
    assert executions == 1
    assert result.tool_results[1]["reused"] is True


@pytest.mark.asyncio
async def test_single_agent_rejects_duplicate_ids_and_parallel_prepare() -> None:
    duplicate = FakeModelProvider(
        [ModelResponse(content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"a","name":"get_book","arguments":{}},{"call_id":"a","name":"get_book","arguments":{}}]}', model="fake")]
    )
    with pytest.raises(SingleAgentError, match="unique"):
        await SingleAgentRuntime(duplicate, "fake", registry()).run("x", context=context())

    prepare = FakeModelProvider(
        [ModelResponse(content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"a","name":"prepare_order","arguments":{}},{"call_id":"b","name":"get_book","arguments":{}}]}', model="fake")]
    )
    with pytest.raises(SingleAgentError, match="READ_ONLY"):
        await SingleAgentRuntime(prepare, "fake", registry(include_prepare=True)).run(
            "x", context=context(), allowed_tools=frozenset({"prepare_order", "get_book"}), permissions=frozenset({"order:prepare", "catalog:read"})
        )


@pytest.mark.asyncio
async def test_single_agent_allows_one_same_round_schema_repair() -> None:
    provider = FakeModelProvider(
        [
            ModelResponse(content='{"action":"bad"}', model="fake"),
            ModelResponse(content='{"action":"FINAL","answer":"ok"}', model="fake"),
        ]
    )
    result = await SingleAgentRuntime(provider, "fake", registry()).run("x", context=context())
    assert result.answer == "ok"
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_single_agent_allows_one_tool_argument_schema_repair() -> None:
    executions = 0

    async def execute(arguments, context):
        nonlocal executions
        executions += 1
        return ToolResult(value={"books": []}, evidence_ids=["catalog:empty"])

    search_registry = ToolRegistry(
        [
            ToolSpec(
                name="search_books",
                description="search catalog",
                input_model=SearchBooksInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"catalog:read"}),
                error_codes=frozenset({"INVALID_TOOL_ARGUMENTS"}),
                executor=execute,
            )
        ]
    )
    provider = FakeModelProvider(
        [
            ModelResponse(content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"bad","name":"search_books","arguments":{"q":"distributed","languages":["zh-CN"]}}]}', model="fake"),
            ModelResponse(content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"fixed","name":"search_books","arguments":{"q":"distributed","language":"zh-CN"}}]}', model="fake"),
            ModelResponse(content='{"action":"FINAL","answer":"done"}', model="fake"),
        ]
    )

    result = await SingleAgentRuntime(provider, "fake", search_registry).run(
        "recommend",
        context=context(),
        allowed_tools=frozenset({"search_books"}),
        permissions=frozenset({"catalog:read"}),
    )

    assert result.answer == "done"
    assert executions == 1
    assert len(provider.requests) == 3
    assert "failed the declared JSON Schema" in provider.requests[1].messages[-1].content


@pytest.mark.asyncio
async def test_single_agent_default_permissions_include_book_knowledge() -> None:
    async def execute(arguments, context):
        return ToolResult(value={"chunks": [{"book_id": 1, "content": "guide"}]}, evidence_ids=["book-guide:1:x"])

    knowledge_registry = ToolRegistry(
        [
            ToolSpec(
                name="search_book_knowledge",
                description="read curated book knowledge",
                input_model=KnowledgeInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"book-knowledge:read"}),
                error_codes=frozenset({"TOOL_TIMEOUT"}),
                executor=execute,
            )
        ]
    )
    provider = FakeModelProvider(
        [
            ModelResponse(
                content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"k1","name":"search_book_knowledge","arguments":{"query":"入门","book_ids":[1]}}]}',
                model="fake",
            ),
            ModelResponse(content='{"action":"FINAL","answer":"有书籍知识证据"}', model="fake"),
        ]
    )
    result = await SingleAgentRuntime(provider, "fake", knowledge_registry).run(
        "这本书适合入门吗？",
        context=ToolContext(
            trace_id="trace",
            conversation_id="conversation",
            authenticated_user_id=1,
            access_token="token",
            guest_mode=False,
            catalog_evidence_book_ids=frozenset({1}),
        ),
        allowed_tools=frozenset({"search_book_knowledge"}),
    )
    assert result.answer == "有书籍知识证据"


class _Events:
    def __init__(self) -> None:
        self.values: list[tuple[str, dict[str, object]]] = []

    async def emit(self, name: str, data: dict[str, object]) -> None:
        self.values.append((name, data))


@pytest.mark.asyncio
async def test_single_agent_refreshes_recently_displayed_book_before_knowledge_search() -> None:
    execution_order: list[str] = []

    async def get_book(arguments, _context):
        execution_order.append("get_book")
        return ToolResult(
            value={"book_id": arguments.book_id, "price": 4750, "available_stock": 2},
            evidence_ids=[f"book:{arguments.book_id}:v9"],
        )

    async def search_book_knowledge(arguments, tool_context):
        execution_order.append("search_book_knowledge")
        assert set(arguments.book_ids).issubset(tool_context.catalog_evidence_book_ids)
        return ToolResult(value={"chunks": [{"book_id": 1, "content": "guide"}]}, evidence_ids=["book-guide:1:v9"])

    tools = ToolRegistry(
        [
            ToolSpec(
                name="get_book",
                description="read current Catalog record",
                input_model=GetBookInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"catalog:read"}),
                error_codes=frozenset({"TOOL_TIMEOUT"}),
                executor=get_book,
            ),
            ToolSpec(
                name="search_book_knowledge",
                description="read curated book knowledge",
                input_model=KnowledgeInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"book-knowledge:read"}),
                error_codes=frozenset({"TOOL_TIMEOUT"}),
                executor=search_book_knowledge,
            ),
        ]
    )
    provider = FakeModelProvider(
        [
            ModelResponse(
                content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"k1","name":"search_book_knowledge","arguments":{"query":"入门","book_ids":[1]}}]}',
                model="fake",
            ),
            ModelResponse(content='{"action":"FINAL","answer":"有书籍知识证据"}', model="fake"),
        ]
    )
    events = _Events()

    result = await SingleAgentRuntime(provider, "fake", tools, event_sink=events).run(
        "比较刚才推荐的书",
        context=context(),
        context_items=[
            {
                "kind": "history",
                "request": "推荐一本入门书",
                "answer": "上次显示的价格是 3000 分。",
                "recommendation_book_ids": [1],
            }
        ],
        allowed_tools=frozenset({"get_book", "search_book_knowledge"}),
    )

    assert execution_order == ["get_book", "search_book_knowledge"]
    assert [item["tool"] for item in result.tool_results] == ["get_book", "search_book_knowledge"]
    assert result.tool_results[0]["value"]["price"] == 4750
    assert result.tool_calls == 2
    assert [data["tool"] for name, data in events.values if name == "tool.started"] == [
        "get_book",
        "search_book_knowledge",
    ]


@pytest.mark.asyncio
async def test_single_agent_rejects_book_knowledge_id_absent_from_recent_history() -> None:
    executions: list[str] = []

    async def get_book(arguments, _context):
        executions.append("get_book")
        return ToolResult(value={"book_id": arguments.book_id}, evidence_ids=[f"book:{arguments.book_id}:v1"])

    async def search_book_knowledge(_arguments, _context):
        executions.append("search_book_knowledge")
        return ToolResult(value={"chunks": []}, evidence_ids=[])

    tools = ToolRegistry(
        [
            ToolSpec(
                name="get_book",
                description="read current Catalog record",
                input_model=GetBookInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"catalog:read"}),
                error_codes=frozenset({"TOOL_TIMEOUT"}),
                executor=get_book,
            ),
            ToolSpec(
                name="search_book_knowledge",
                description="read curated book knowledge",
                input_model=KnowledgeInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"book-knowledge:read"}),
                error_codes=frozenset({"TOOL_TIMEOUT"}),
                executor=search_book_knowledge,
            ),
        ]
    )
    provider = FakeModelProvider(
        [
            ModelResponse(
                content='{"action":"CALL_TOOLS","tool_calls":[{"call_id":"k1","name":"search_book_knowledge","arguments":{"query":"入门","book_ids":[99]}}]}',
                model="fake",
            )
        ]
    )

    with pytest.raises(SingleAgentError) as exc_info:
        await SingleAgentRuntime(provider, "fake", tools).run(
            "比较刚才推荐的书",
            context=context(),
            context_items=[
                {
                    "kind": "history",
                    "request": "推荐一本入门书",
                    "answer": "推荐一本书。",
                    "recommendation_book_ids": [1],
                }
            ],
            allowed_tools=frozenset({"get_book", "search_book_knowledge"}),
        )

    assert exc_info.value.code == "EVIDENCE_MISMATCH"
    assert executions == []


@pytest.mark.asyncio
async def test_final_emits_agent_completed_and_tool_context_is_valid_json() -> None:
    events = _Events()
    provider = FakeModelProvider(
        [ModelResponse(content='{"action":"FINAL","answer":"ok"}', model="fake")]
    )
    runtime = SingleAgentRuntime(provider, "fake", registry(), event_sink=events)
    result = await runtime.run("x", context=context())
    assert result.answer == "ok"
    assert ("agent.completed", {"round": 1, "status": "COMPLETED"}) in events.values

    encoded = runtime._tool_context_content(
        {"tool": "search_policy", "value": {"description": "x" * 200_000}, "evidence_ids": ["p"]},
        {"used": 0},
    )
    parsed = json.loads(encoded)
    assert parsed["context_truncated"] is True
    assert len(encoded.encode("utf-8")) <= runtime.limits.tool_result_max_bytes


def test_rag_budget_is_exhausted_after_first_truncated_chunk() -> None:
    runtime = SingleAgentRuntime(FakeModelProvider([]), "fake", registry())
    budget = {"used": 0}
    first = json.loads(
        runtime._tool_context_content(
            {"tool": "search_policy", "value": {"chunks": [{"content": "x" * 50_000}]}, "evidence_ids": []},
            budget,
        )
    )
    second = json.loads(
        runtime._tool_context_content(
            {"tool": "search_policy", "value": {"chunks": [{"content": "y" * 50_000}]}, "evidence_ids": []},
            budget,
        )
    )
    assert first["value"]["chunks"][0]["context_truncated"] is True
    assert budget["used"] == runtime.limits.rag_max_context_tokens
    assert second["value"]["chunks"] == []


@pytest.mark.asyncio
async def test_public_sse_sink_drops_internal_events() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    sink = QueueEventSink(queue, developer_mode=True, guest=False, trace_persisted=True)
    await sink.emit("evidence.retrieved", {"evidence_ids": ["secret"]})
    await sink.emit("memory.trace.internal", {"memory_ids": ["secret"]})
    event = await queue.get()
    assert event.event == "evidence.retrieved"
    assert queue.empty()

    await sink.emit("confirmation.required", {"action_type": "CREATE_ORDER"})
    event = await queue.get()
    assert event.event == "confirmation.required"

    await sink.emit("watch_confirmation.required", {"action_type": "CREATE_INVENTORY_WATCH"})
    event = await queue.get()
    assert event.event == "watch_confirmation.required"

    await sink.emit("plan.created", {"nodes": []})
    assert queue.empty()


def test_trace_router_exposes_no_replay_routes() -> None:
    router = build_trace_router(object(), object())  # type: ignore[arg-type]
    assert not any("/replays" in route.path for route in router.routes)


def test_request_tool_allowlist_is_scoped_by_deterministic_intent() -> None:
    assert ChatService._allowed_tools_for_request(["policy"], authenticated=False) == frozenset({"search_policy"})
    assert ChatService._allowed_tools_for_request(["recommendation"], authenticated=False) == frozenset(
        {"search_books", "get_book", "search_book_knowledge"}
    )
    assert "prepare_order" in ChatService._allowed_tools_for_request(["recommendation"], authenticated=True)
    assert "search_policy" not in ChatService._allowed_tools_for_request(["recommendation"], authenticated=True)


def test_message_completed_recommendation_payload_is_json_serializable() -> None:
    from app.harness.single_agent import SingleAgentRecommendation

    recommendation = SingleAgentRecommendation(book_id=7, reason="evidence-backed")
    event = ChatEvent(
        event="message.completed",
        data={"recommendations": [recommendation.model_dump(mode="json")]},
    )
    assert '"book_id":7' in event.encode_sse()


def test_watch_confirmation_card_contains_expiry_and_submission_contract() -> None:
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    payload = ChatService._watch_confirmation_payload(
        "conversation-1",
        {"book_id": 7, "title": "Example", "available_stock": 0},
        InventoryWatchIntent(book_identifier="7", desired_quantity=2, duration_seconds=3600),
        86400,
        now=now,
    )
    assert payload == {
        "action_type": "CREATE_INVENTORY_WATCH",
        "risk": "CONFIRM_REQUIRED",
        "conversation_id": "conversation-1",
        "book": {"book_id": 7, "title": "Example", "available_stock": 0},
        "desired_quantity": 2,
        "expires_at": "2026-09-25T13:00:00Z",
        "submit_path": "/api/tasks/inventory-watches",
    }


def test_watch_catalog_read_distinguishes_book_id_isbn_and_title() -> None:
    assert _watch_catalog_read("11") == ("get_book", {"book_id": 11})
    assert _watch_catalog_read("9780000000217") == ("get_book", {"isbn": "9780000000217"})
    assert _watch_catalog_read("到货通知练习册") == (
        "search_books",
        {"q": "到货通知练习册", "limit": 50},
    )


class _SlowProvider(ModelProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(0.2)
        return ModelResponse(content='{"action":"FINAL","answer":"late"}', model="slow")


@pytest.mark.asyncio
async def test_single_agent_timeout_covers_provider_call() -> None:
    runtime = SingleAgentRuntime(
        _SlowProvider(),
        "slow",
        registry(),
        limits=SingleAgentLimits(timeout_seconds=0.01),
    )
    with pytest.raises(SingleAgentError, match="time budget") as error:
        await runtime.run("x", context=context())
    assert error.value.code == "TOOL_TIMEOUT"


@pytest.mark.asyncio
async def test_required_catalog_read_is_budgeted_and_given_to_model() -> None:
    executions: list[int] = []

    async def get_book(arguments: GetBookInput, _context):
        executions.append(arguments.book_id or 0)
        return ToolResult(
            value={"book_id": arguments.book_id, "title": "缺货书", "available_stock": 0, "status": "ACTIVE"},
            evidence_ids=["book:11:v1"],
        )

    tools = ToolRegistry(
        [
            ToolSpec(
                name="get_book",
                description="read book",
                input_model=GetBookInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"catalog:read"}),
                error_codes=frozenset({"BOOK_NOT_FOUND"}),
                executor=get_book,
            )
        ]
    )
    provider = FakeModelProvider(
        [ModelResponse(content='{"action":"FINAL","answer":"已读取实时库存","addressed_question_ids":["inventory_watch"]}', model="fake")]
    )
    result = await SingleAgentRuntime(provider, "fake", tools).run(
        "书籍 #11 缺货时提醒我",
        context=context(),
        allowed_tools=frozenset({"get_book"}),
        permissions=frozenset({"catalog:read"}),
        required_reads=(
            SingleAgentToolCall(call_id="watch-catalog-refresh", name="get_book", arguments={"book_id": 11}),
        ),
    )

    assert executions == [11]
    assert result.tool_calls == 1
    assert result.tool_results[0]["evidence_ids"] == ["book:11:v1"]
    assert any(message.content.startswith("REQUIRED_TOOL_RESULT_JSON=") for message in provider.requests[0].messages)


@pytest.mark.asyncio
async def test_search_books_cannot_drop_explicit_hard_filters() -> None:
    captured: list[SearchBooksInput] = []

    async def search(arguments, _context):
        captured.append(arguments)
        return ToolResult(value={"books": []})

    search_registry = ToolRegistry(
        [
            ToolSpec(
                name="search_books",
                description="search catalog",
                input_model=SearchBooksInput,
                risk=RiskLevel.READ_ONLY,
                timeout_seconds=1,
                max_retries=0,
                idempotent=True,
                required_permissions=frozenset({"catalog:read"}),
                error_codes=frozenset({"TOOL_TIMEOUT"}),
                executor=search,
            )
        ]
    )
    provider = FakeModelProvider(
        [
            ModelResponse(
                content=(
                    '{"action":"CALL_TOOLS","tool_calls":[{"call_id":"s1","name":"search_books",'
                    '"arguments":{"q":"分布式系统","max_price":999999,"language":"en",'
                    '"in_stock":false}}]}'
                ),
                model="fake",
            ),
            ModelResponse(content='{"action":"FINAL","answer":"没有匹配书籍"}', model="fake"),
        ]
    )
    runtime = SingleAgentRuntime(provider, "fake", search_registry)
    constraints = {
        "max_price": 10000,
        "language": "zh-CN",
        "in_stock": True,
        "authors": ["Alice"],
        "categories": ["分布式"],
    }

    await runtime.run(
        "推荐中文、每本100元以内且有货的Alice分布式书",
        context=context().model_copy(update={"explicit_constraints": constraints}),
        allowed_tools=frozenset({"search_books"}),
        permissions=frozenset({"catalog:read"}),
    )

    assert len(captured) == 1
    assert captured[0].max_price == 10000
    assert captured[0].language == "zh-CN"
    assert captured[0].in_stock is True
    assert captured[0].author == "Alice"
    assert captured[0].category == "分布式"


def test_final_validator_rejects_books_that_violate_explicit_constraints() -> None:
    from app.harness.single_agent import SingleAgentAction

    action = SingleAgentAction(
        action="FINAL",
        answer="推荐这本",
        recommendations=[{"book_id": 7}],
    )
    issues = validate_final_action(
        action,
        [
            {
                "tool": "search_books",
                "value": {
                    "books": [
                        {
                            "book_id": 7,
                            "price": 20_000,
                            "currency": "CNY",
                            "language_code": "en",
                            "authors": ["Bob"],
                            "categories": ["Java"],
                            "available_stock": 0,
                            "status": "ACTIVE",
                        }
                    ]
                },
                "evidence_ids": [],
            }
        ],
        constraints=UserConstraints(
            max_price=10_000,
            total_budget=10_000,
            language="zh-CN",
            authors=["Alice"],
            categories=["分布式"],
            in_stock=True,
        ),
    )
    codes = {issue["code"] for issue in issues}
    assert {
        "BUDGET_VIOLATION",
        "TOTAL_BUDGET_VIOLATION",
        "LANGUAGE_CONSTRAINT_VIOLATION",
        "AUTHOR_CONSTRAINT_VIOLATION",
        "CATEGORY_CONSTRAINT_VIOLATION",
        "STOCK_CONSTRAINT_VIOLATION",
    } <= codes


def test_final_validator_accepts_direct_get_book_result_shape() -> None:
    from app.harness.single_agent import SingleAgentAction

    book = {
        "book_id": 7,
        "price": 8_800,
        "currency": "CNY",
        "language_code": "zh-CN",
        "authors": ["Alice"],
        "categories": ["分布式"],
        "available_stock": 3,
        "status": "ACTIVE",
    }
    action = SingleAgentAction(
        action="FINAL",
        answer="当前库存为3本",
        recommendations=[{"book_id": 7, "price": 8_800, "available_stock": 3}],
        addressed_question_ids=["recommendation"],
    )

    issues = validate_final_action(
        action,
        [{"tool": "get_book", "value": book, "evidence_ids": ["book:7:v1"]}],
        constraints=UserConstraints(),
        required_question_ids=["recommendation"],
    )

    assert issues == []


def test_final_validator_rejects_undocumented_get_book_wrapper() -> None:
    from app.harness.single_agent import SingleAgentAction

    action = SingleAgentAction(
        action="FINAL",
        answer="推荐这本",
        recommendations=[{"book_id": 7}],
    )
    issues = validate_final_action(
        action,
        [{"tool": "get_book", "value": {"book": {"book_id": 7}}, "evidence_ids": []}],
    )

    assert {issue["code"] for issue in issues} == {"RECOMMENDATION_BOOK_UNGROUNDED"}


def test_order_preview_recommendation_accepts_matching_preview_evidence() -> None:
    from app.harness.single_agent import SingleAgentAction

    action = SingleAgentAction(
        action="FINAL",
        answer="订单预览已生成，等待确认",
        recommendations=[{"book_id": 26, "price": 3000, "evidence_ids": ["order-preview:abc"]}],
        addressed_question_ids=["recommendation"],
    )
    issues = validate_final_action(
        action,
        [{
            "tool": "prepare_order",
            "value": {"items": [{"book_id": 26, "unit_price": 3000}], "total_amount": 3000},
            "evidence_ids": ["order-preview:abc"],
        }],
        required_question_ids=["recommendation"],
    )
    assert issues == []


def test_order_preview_with_catalog_result_does_not_require_book_guide_evidence() -> None:
    from app.harness.single_agent import SingleAgentAction

    action = SingleAgentAction(
        action="FINAL",
        answer="实时商品已核验，订单预览等待确认",
        recommendations=[{
            "book_id": 26,
            "price": 3000,
            "available_stock": 25,
            "evidence_ids": ["book:26:v1", "order-preview:abc"],
        }],
        addressed_question_ids=["recommendation"],
    )
    issues = validate_final_action(
        action,
        [
            {
                "tool": "get_book",
                "value": {"book_id": 26, "price": 3000, "available_stock": 25, "status": "ACTIVE"},
                "evidence_ids": ["book:26:v1"],
            },
            {
                "tool": "prepare_order",
                "value": {"items": [{"book_id": 26, "unit_price": 3000}]},
                "evidence_ids": ["order-preview:abc"],
            },
        ],
        required_question_ids=["recommendation"],
    )
    assert issues == []


def test_order_preview_recommendation_rejects_missing_preview_evidence() -> None:
    from app.harness.single_agent import SingleAgentAction

    action = SingleAgentAction(
        action="FINAL",
        answer="订单预览已生成",
        recommendations=[{"book_id": 26, "price": 3000}],
        addressed_question_ids=["recommendation"],
    )
    issues = validate_final_action(
        action,
        [{
            "tool": "prepare_order",
            "value": {"items": [{"book_id": 26, "unit_price": 3000}]},
            "evidence_ids": ["order-preview:abc"],
        }],
        required_question_ids=["recommendation"],
    )
    assert {issue["code"] for issue in issues} == {"EVIDENCE_MISMATCH"}
