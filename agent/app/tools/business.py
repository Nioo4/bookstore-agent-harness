from __future__ import annotations

import hashlib

from app.tools.clients import CatalogClient, OrderClient
from app.tools.errors import ToolError
from app.tools.knowledge import PolicyKnowledgeService
from app.tools.policy import DeterministicPolicyEngine
from app.tools.registry import ToolRegistry
from app.tools.schemas import (
    CreateOrderInput,
    GetBookInput,
    GetOrderInput,
    ListMyOrdersInput,
    PrepareOrderInput,
    SearchPolicyInput,
    SearchBooksInput,
)
from app.tools.types import ReplayToolDefinition, RiskLevel, ToolContext, ToolResult, ToolSpec, TraceProjectionSpec


def _book_evidence(book: dict[str, object]) -> str:
    return f"book:{book['book_id']}:v{book['version']}"


def _prepare_idempotency_key(trace_id: str) -> str:
    """Bind a preview retry to the server-created run, not model output."""
    return "prepare-" + hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:48]


def build_business_tool_registry(
    catalog: CatalogClient,
    orders: OrderClient,
    knowledge: PolicyKnowledgeService | None = None,
) -> ToolRegistry:
    async def search_books(arguments: SearchBooksInput, _: ToolContext) -> ToolResult:
        value = await catalog.search_books(arguments, timeout=3)
        evidence_ids = [_book_evidence(book) for book in value["books"]]
        if not evidence_ids:
            # An authoritative empty search is evidence of no candidates, not
            # missing dependency evidence. Never invent a book record for it.
            evidence_ids = ["catalog-query:" + hashlib.sha256(arguments.model_dump_json().encode()).hexdigest()]
        return ToolResult(value=value, evidence_ids=evidence_ids)

    async def get_book(arguments: GetBookInput, _: ToolContext) -> ToolResult:
        value = await catalog.get_book(arguments, timeout=3)
        return ToolResult(value=value, evidence_ids=[_book_evidence(value)])

    async def search_policy(arguments: SearchPolicyInput, _: ToolContext) -> ToolResult:
        assert knowledge is not None
        try:
            chunks = await knowledge.search(arguments.query, arguments.limit)
        except Exception as exc:
            raise ToolError("DEPENDENCY_UNAVAILABLE", "policy knowledge service is unavailable", retryable=True) from exc
        return ToolResult(
            value={"chunks": chunks},
            evidence_ids=[f"policy:{chunk['content_hash']}" for chunk in chunks],
        )

    async def prepare_order(arguments: PrepareOrderInput, context: ToolContext) -> ToolResult:
        request = arguments.model_copy(
            update={"idempotency_key": _prepare_idempotency_key(context.trace_id)}
        )
        value = await orders.prepare_order(request, context.access_token or "", context.conversation_id, timeout=5)
        token_digest = hashlib.sha256(value["confirmation_token"].encode()).hexdigest()[:16]
        if context.event_sink is None:
            raise ToolError("DEPENDENCY_UNAVAILABLE", "confirmation delivery channel is unavailable")
        await context.event_sink.emit(
            "confirmation.prepared.internal",
            {**value, "action_type": "CREATE_ORDER", "risk": "COMMIT"},
        )
        safe_value = {key: item for key, item in value.items() if key != "confirmation_token"}
        safe_value["confirmation_required"] = True
        return ToolResult(value=safe_value, evidence_ids=[f"order-preview:{token_digest}"])

    async def create_order(arguments: CreateOrderInput, context: ToolContext) -> ToolResult:
        value = await orders.create_order(
            arguments,
            context.access_token or "",
            context.conversation_id,
            timeout=8,
        )
        order = value.get("order")
        evidence_ids = [f"confirmation:{value['confirmation_id']}"]
        if isinstance(order, dict) and order.get("order_id"):
            evidence_ids.append(f"order:{order['order_id']}")
        return ToolResult(value=value, evidence_ids=evidence_ids)

    async def get_order(arguments: GetOrderInput, context: ToolContext) -> ToolResult:
        value = await orders.get_order(arguments, context.access_token or "", timeout=3)
        return ToolResult(value=value, evidence_ids=[f"order:{value['order_id']}"])

    async def list_my_orders(arguments: ListMyOrdersInput, context: ToolContext) -> ToolResult:
        value = await orders.list_my_orders(arguments, context.access_token or "", timeout=3)
        return ToolResult(value=value, evidence_ids=[f"order:{order['order_id']}" for order in value["orders"]])

    specs = [
            ToolSpec(name="search_books", description="Search authoritative real-time catalog data.", input_model=SearchBooksInput, risk=RiskLevel.READ_ONLY, timeout_seconds=3, max_retries=1, idempotent=True, required_permissions=frozenset({"catalog:read"}), error_codes=frozenset({"INVALID_TOOL_ARGUMENTS", "TOOL_TIMEOUT", "BUSINESS_RPC_ERROR"}), trace_projection=TraceProjectionSpec(schema_version="search_books-v1", request_fields=frozenset(SearchBooksInput.model_fields), response_fields=frozenset({"*"})), executor=search_books),
            ToolSpec(name="get_book", description="Get one book by ID or ISBN from the authoritative catalog.", input_model=GetBookInput, risk=RiskLevel.READ_ONLY, timeout_seconds=3, max_retries=1, idempotent=True, required_permissions=frozenset({"catalog:read"}), error_codes=frozenset({"BOOK_NOT_FOUND", "INVALID_TOOL_ARGUMENTS", "TOOL_TIMEOUT"}), trace_projection=TraceProjectionSpec(schema_version="get_book-v1", request_fields=frozenset(GetBookInput.model_fields), response_fields=frozenset({"*"})), executor=get_book),
            ToolSpec(name="prepare_order", description="Create a non-mutating order preview and one-time confirmation action.", input_model=PrepareOrderInput, risk=RiskLevel.PREPARE, timeout_seconds=5, max_retries=1, idempotent=True, required_permissions=frozenset({"order:prepare"}), error_codes=frozenset({"AUTH_REQUIRED", "EMAIL_NOT_VERIFIED", "BOOK_INACTIVE", "INVENTORY_INSUFFICIENT", "IDEMPOTENCY_CONFLICT", "TOOL_TIMEOUT"}), trace_projection=TraceProjectionSpec(schema_version="prepare_order-v1", request_fields=frozenset({"items"}), response_fields=frozenset({"items", "total_amount", "currency", "expires_at", "confirmation_required"}), simulated_on_replay=True), executor=prepare_order),
            ToolSpec(name="create_order", description="Apply an explicit APPROVE or REJECT decision through the transactional Order RPC.", input_model=CreateOrderInput, risk=RiskLevel.COMMIT, timeout_seconds=8, max_retries=1, idempotent=True, required_permissions=frozenset({"order:commit"}), error_codes=frozenset({"AUTH_REQUIRED", "EMAIL_NOT_VERIFIED", "CONFIRMATION_REQUIRED", "CONFIRMATION_EXPIRED", "CONFIRMATION_NOT_FOUND", "CONFIRMATION_ALREADY_DECIDED", "PRICE_CHANGED", "BOOK_INACTIVE", "INVENTORY_INSUFFICIENT", "TOOL_TIMEOUT"}), trace_projection=TraceProjectionSpec(schema_version="create_order-v1", request_fields=frozenset({"decision"}), response_fields=frozenset({"confirmation_id", "decision", "status", "order", "replayed"}), simulated_on_replay=True), executor=create_order),
            ToolSpec(name="get_order", description="Read one order scoped to the authenticated user.", input_model=GetOrderInput, risk=RiskLevel.USER_SCOPED_READ, timeout_seconds=3, max_retries=1, idempotent=True, required_permissions=frozenset({"order:read"}), error_codes=frozenset({"AUTH_REQUIRED", "ORDER_NOT_FOUND", "TOOL_TIMEOUT"}), trace_projection=TraceProjectionSpec(schema_version="get_order-v1", request_fields=frozenset(GetOrderInput.model_fields), response_fields=frozenset({"*"})), executor=get_order),
            ToolSpec(name="list_my_orders", description="List orders scoped to the authenticated user.", input_model=ListMyOrdersInput, risk=RiskLevel.USER_SCOPED_READ, timeout_seconds=3, max_retries=1, idempotent=True, required_permissions=frozenset({"order:read"}), error_codes=frozenset({"AUTH_REQUIRED", "TOOL_TIMEOUT"}), trace_projection=TraceProjectionSpec(schema_version="list_my_orders-v1", request_fields=frozenset(ListMyOrdersInput.model_fields), response_fields=frozenset({"*"})), executor=list_my_orders),
    ]
    if knowledge is not None:
        specs.append(
            ToolSpec(name="search_policy", description="Search versioned policy knowledge and return source citations.", input_model=SearchPolicyInput, risk=RiskLevel.READ_ONLY, timeout_seconds=5, max_retries=1, idempotent=True, required_permissions=frozenset({"policy:read"}), error_codes=frozenset({"INVALID_TOOL_ARGUMENTS", "TOOL_TIMEOUT", "DEPENDENCY_UNAVAILABLE"}), trace_projection=TraceProjectionSpec(schema_version="search_policy-v1", request_fields=frozenset(SearchPolicyInput.model_fields), response_fields=frozenset({"*"})), executor=search_policy)
        )
    return ToolRegistry(
        specs,
        policy=DeterministicPolicyEngine(),
    )


def build_business_replay_definitions() -> list[ReplayToolDefinition]:
    """Pure replay schemas: deliberately contains no executor, client, policy, or network handle."""
    return [
        ReplayToolDefinition(
            name="search_books",
            input_model=SearchBooksInput,
            risk=RiskLevel.READ_ONLY,
            required_permissions=frozenset({"catalog:read"}),
            trace_projection=TraceProjectionSpec(
                schema_version="search_books-v1",
                request_fields=frozenset(SearchBooksInput.model_fields),
                response_fields=frozenset({"*"}),
            ),
        ),
        ReplayToolDefinition(
            name="get_book",
            input_model=GetBookInput,
            risk=RiskLevel.READ_ONLY,
            required_permissions=frozenset({"catalog:read"}),
            trace_projection=TraceProjectionSpec(
                schema_version="get_book-v1",
                request_fields=frozenset(GetBookInput.model_fields),
                response_fields=frozenset({"*"}),
            ),
        ),
        ReplayToolDefinition(
            name="search_policy",
            input_model=SearchPolicyInput,
            risk=RiskLevel.READ_ONLY,
            required_permissions=frozenset({"policy:read"}),
            trace_projection=TraceProjectionSpec(
                schema_version="search_policy-v1",
                request_fields=frozenset(SearchPolicyInput.model_fields),
                response_fields=frozenset({"*"}),
            ),
        ),
        ReplayToolDefinition(
            name="prepare_order",
            input_model=PrepareOrderInput,
            risk=RiskLevel.PREPARE,
            required_permissions=frozenset({"order:prepare"}),
            trace_projection=TraceProjectionSpec(
                schema_version="prepare_order-v1",
                request_fields=frozenset({"items"}),
                response_fields=frozenset(
                    {"items", "total_amount", "currency", "expires_at", "confirmation_required"}
                ),
                simulated_on_replay=True,
            ),
        ),
        ReplayToolDefinition(
            name="create_order",
            input_model=CreateOrderInput,
            risk=RiskLevel.COMMIT,
            required_permissions=frozenset({"order:commit"}),
            trace_projection=TraceProjectionSpec(
                schema_version="create_order-v1",
                request_fields=frozenset({"decision"}),
                response_fields=frozenset(
                    {"confirmation_id", "decision", "status", "order", "replayed"}
                ),
                simulated_on_replay=True,
            ),
        ),
        ReplayToolDefinition(
            name="get_order",
            input_model=GetOrderInput,
            risk=RiskLevel.USER_SCOPED_READ,
            required_permissions=frozenset({"order:read"}),
            trace_projection=TraceProjectionSpec(
                schema_version="get_order-v1",
                request_fields=frozenset(GetOrderInput.model_fields),
                response_fields=frozenset({"*"}),
            ),
        ),
        ReplayToolDefinition(
            name="list_my_orders",
            input_model=ListMyOrdersInput,
            risk=RiskLevel.USER_SCOPED_READ,
            required_permissions=frozenset({"order:read"}),
            trace_projection=TraceProjectionSpec(
                schema_version="list_my_orders-v1",
                request_fields=frozenset(ListMyOrdersInput.model_fields),
                response_fields=frozenset({"*"}),
            ),
        ),
    ]
