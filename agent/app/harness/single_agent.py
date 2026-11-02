from __future__ import annotations

"""The v2 online execution loop.

This module is deliberately independent from the retired multi-step
orchestration implementation. It uses the existing ToolRegistry boundary so
permissions, schemas, timeouts, retries and trace hooks remain in one place.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
import tiktoken

from app.models.invocation import invoke_model
from app.models.provider import ModelMessage, ModelProvider, ModelRequest
from app.tools.errors import ToolError
from app.tools.registry import ToolRegistry
from app.tools.types import RiskLevel, ToolContext, ToolResult


class SingleAgentError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class SingleAgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    arguments: dict[str, Any]


class SingleAgentRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    book_id: int = Field(gt=0)
    reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    price: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    available_stock: int | None = Field(default=None, ge=0)
    status: str | None = None
    language_code: str | None = None


class SingleAgentPolicyClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    section_path: str = Field(min_length=1)
    version: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class SingleAgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    tool_calls: list[SingleAgentToolCall] = Field(default_factory=list, max_length=12)
    answer: str | None = Field(default=None, min_length=1, max_length=32_768)
    recommendations: list[SingleAgentRecommendation] = Field(default_factory=list, max_length=20)
    policy_claims: list[SingleAgentPolicyClaim] = Field(default_factory=list, max_length=20)
    addressed_question_ids: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_action(self) -> "SingleAgentAction":
        if self.action not in {"CALL_TOOLS", "FINAL"}:
            raise ValueError("action must be CALL_TOOLS or FINAL")
        if self.action == "CALL_TOOLS":
            if not self.tool_calls:
                raise ValueError("CALL_TOOLS requires at least one tool call")
            if self.answer is not None or self.recommendations or self.policy_claims:
                raise ValueError("CALL_TOOLS cannot contain final answer fields")
        elif self.tool_calls:
            raise ValueError("FINAL cannot contain tool calls")
        elif self.answer is None:
            raise ValueError("FINAL requires answer")
        return self


@dataclass(frozen=True)
class SingleAgentLimits:
    max_rounds: int = 6
    max_tool_calls: int = 12
    max_parallel_reads: int = 3
    max_repair: int = 1
    timeout_seconds: float = 120.0
    rag_max_context_tokens: int = 6000
    tool_result_max_bytes: int = 65_536

    def __post_init__(self) -> None:
        if not 1 <= self.max_rounds <= 6:
            raise ValueError("max_rounds must be between 1 and 6")
        if not 1 <= self.max_tool_calls <= 12:
            raise ValueError("max_tool_calls must be between 1 and 12")
        if not 1 <= self.max_parallel_reads <= 3:
            raise ValueError("max_parallel_reads must be between 1 and 3")
        if self.max_repair != 1 or self.timeout_seconds <= 0:
            raise ValueError("v2 repair and timeout limits are fixed")
        if self.rag_max_context_tokens != 6000 or self.tool_result_max_bytes != 65_536:
            raise ValueError("v2 context limits are fixed")


@dataclass
class SingleAgentResult:
    run_id: str
    answer: str
    action: SingleAgentAction
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    rounds: int = 0
    tool_calls: int = 0
    repaired: bool = False


class SingleAgentRuntime:
    """Execute one model with a fixed allowlist and deterministic budgets."""

    DEFAULT_TOOLS = frozenset(
        {
            "search_books",
            "get_book",
            "search_book_knowledge",
            "search_policy",
            "get_order",
            "list_my_orders",
            "prepare_order",
        }
    )
    GUEST_TOOLS = frozenset({"search_books", "get_book", "search_book_knowledge", "search_policy"})

    def __init__(
        self,
        provider: ModelProvider,
        model: str,
        tools: ToolRegistry,
        *,
        limits: SingleAgentLimits | None = None,
        system_prompt: str = "",
        event_sink=None,
        final_validator: Callable[[SingleAgentAction, list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]] | None = None,
        repair_provider: Callable[[SingleAgentAction, list[dict[str, Any]], list[dict[str, Any]]], Awaitable[SingleAgentAction]] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.tools = tools
        self.limits = limits or SingleAgentLimits()
        self.system_prompt = system_prompt.strip()
        self.event_sink = event_sink
        self.final_validator = final_validator
        self.repair_provider = repair_provider
        self.encoding = tiktoken.get_encoding("cl100k_base")

    async def run(
        self,
        user_request: str,
        *,
        context: ToolContext,
        run_id: str | None = None,
        context_items: list[Mapping[str, Any]] | None = None,
        allowed_tools: frozenset[str] | None = None,
        permissions: frozenset[str] = frozenset(
            {"catalog:read", "policy:read", "book-knowledge:read", "order:read", "order:prepare"}
        ),
        required_reads: tuple[SingleAgentToolCall, ...] = (),
    ) -> SingleAgentResult:
        """Run one request under one timeout covering model, tools and repair."""
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                return await self._run(
                    user_request,
                    context=context,
                    run_id=run_id,
                    context_items=context_items,
                    allowed_tools=allowed_tools,
                    permissions=permissions,
                    required_reads=required_reads,
                )
        except TimeoutError as exc:
            raise SingleAgentError(
                "TOOL_TIMEOUT", "single-agent request exceeded its time budget", retryable=True
            ) from exc

    async def _run(
        self,
        user_request: str,
        *,
        context: ToolContext,
        run_id: str | None = None,
        context_items: list[Mapping[str, Any]] | None = None,
        allowed_tools: frozenset[str] | None = None,
        permissions: frozenset[str] = frozenset(
            {"catalog:read", "policy:read", "book-knowledge:read", "order:read", "order:prepare"}
        ),
        required_reads: tuple[SingleAgentToolCall, ...] = (),
    ) -> SingleAgentResult:
        if not user_request.strip():
            raise SingleAgentError("INVALID_REQUEST", "user request must not be blank")
        current_run = run_id or str(uuid.uuid4())
        allowed = allowed_tools or (self.GUEST_TOOLS if context.guest_mode else self.DEFAULT_TOOLS)
        messages = self._initial_messages(
            user_request,
            context_items or [],
            allowed_tools=allowed,
        )
        results: list[dict[str, Any]] = []
        rag_budget = {"used": 0}
        total_tools = 0
        tool_argument_repairs = 0
        started = time.monotonic()
        final_action: SingleAgentAction | None = None
        read_cache: dict[str, dict[str, Any]] = {}
        if required_reads:
            await self._validate_required_reads(required_reads, allowed)
            if len(required_reads) > self.limits.max_tool_calls:
                raise SingleAgentError("TOOL_BUDGET_EXCEEDED", "required reads exceeded the tool budget")
            for offset in range(0, len(required_reads), self.limits.max_parallel_reads):
                batch = await self._invoke_batch(
                    list(required_reads[offset : offset + self.limits.max_parallel_reads]),
                    context=context,
                    allowed_tools=allowed,
                    permissions=permissions,
                    read_cache=read_cache,
                )
                results.extend(batch)
                total_tools += len(batch)
                for item in batch:
                    messages.append(
                        ModelMessage(
                            role="system",
                            content="REQUIRED_TOOL_RESULT_JSON=" + self._tool_context_content(item, rag_budget),
                        )
                    )
            observed_ids = set(context.catalog_evidence_book_ids)
            for item in results:
                observed_ids.update(_catalog_book_ids(item.get("value")))
            context = context.model_copy(update={"catalog_evidence_book_ids": frozenset(observed_ids)})
        history_book_ids = _latest_history_recommendation_ids(context_items or [])
        for round_number in range(1, self.limits.max_rounds + 1):
            if time.monotonic() - started > self.limits.timeout_seconds:
                raise SingleAgentError("TOOL_TIMEOUT", "single-agent request exceeded its time budget", retryable=True)
            if self.event_sink is not None:
                await self.event_sink.emit("agent.started", {"round": round_number, "agent": "single_agent"})
            action = None
            response = None
            for syntax_attempt in range(2):
                response = await invoke_model(
                    self.provider,
                    caller="single_agent",
                    node_scope=f"single-agent/round-{round_number}",
                    request=ModelRequest(
                        model=self.model,
                        temperature=0,
                        response_schema=SingleAgentAction.model_json_schema(),
                        messages=messages,
                    ),
                )
                try:
                    action = SingleAgentAction.model_validate_json(response.content)
                    break
                except (ValidationError, ValueError, TypeError) as exc:
                    if syntax_attempt:
                        if self.event_sink is not None:
                            await self.event_sink.emit("agent.completed", {"round": round_number, "status": "INVALID"})
                        raise SingleAgentError(
                            "MODEL_OUTPUT_INVALID",
                            "model output did not match the v2 action schema",
                            details={"error": str(exc)},
                        ) from None
                    messages.append(ModelMessage(role="assistant", content=response.content))
                    messages.append(
                        ModelMessage(
                            role="system",
                            content="The previous output failed the strict JSON action schema. Return only a corrected CALL_TOOLS or FINAL JSON object.",
                        )
                    )
            assert action is not None and response is not None
            if action.action == "FINAL":
                final_action = action
                if self.event_sink is not None:
                    await self.event_sink.emit("agent.completed", {"round": round_number, "status": "COMPLETED"})
                break
            await self._validate_tool_batch(action.tool_calls, allowed)
            refresh_ids = self._historical_book_ids_to_refresh(
                action.tool_calls,
                context,
                history_book_ids,
                allowed,
            )
            if total_tools + len(action.tool_calls) + len(refresh_ids) > self.limits.max_tool_calls:
                raise SingleAgentError("TOOL_BUDGET_EXCEEDED", "single-agent tool budget exceeded")
            refreshed: list[dict[str, Any]] = []
            if refresh_ids:
                refresh_calls = [
                    SingleAgentToolCall(
                        call_id=self._refresh_call_id(book_id, action.tool_calls),
                        name="get_book",
                        arguments={"book_id": book_id},
                    )
                    for book_id in refresh_ids
                ]
                for offset in range(0, len(refresh_calls), self.limits.max_parallel_reads):
                    refreshed.extend(
                        await self._invoke_batch(
                            refresh_calls[offset : offset + self.limits.max_parallel_reads],
                            context=context,
                            allowed_tools=allowed,
                            permissions=permissions,
                            read_cache=read_cache,
                        )
                    )
                observed_ids = set(context.catalog_evidence_book_ids)
                for item in refreshed:
                    observed_ids.update(_catalog_book_ids(item.get("value")))
                if not set(refresh_ids).issubset(observed_ids):
                    raise SingleAgentError(
                        "EVIDENCE_MISMATCH",
                        "current Catalog refresh did not return every requested book",
                    )
                context = context.model_copy(update={"catalog_evidence_book_ids": frozenset(observed_ids)})
                total_tools += len(refreshed)
            try:
                batch_results = await self._invoke_batch(
                    action.tool_calls,
                    context=context,
                    allowed_tools=allowed,
                    permissions=permissions,
                    read_cache=read_cache,
                )
            except SingleAgentError as exc:
                # Keep the strict schema, but give a model-created argument
                # shape the same one repair opportunity as an invalid action.
                # Business failures are never retried through this path.
                if exc.code != "INVALID_TOOL_ARGUMENTS" or tool_argument_repairs >= 1:
                    raise
                tool_argument_repairs += 1
                results.extend(refreshed)
                messages.append(ModelMessage(role="assistant", content=response.content))
                for item in refreshed:
                    messages.append(
                        ModelMessage(
                            role="system",
                            content="LOCAL_TOOL_RESULT_JSON=" + self._tool_context_content(item, rag_budget),
                        )
                    )
                messages.append(
                    ModelMessage(
                        role="system",
                        content=(
                            "The previous tool arguments failed the declared JSON Schema. "
                            "Return a corrected CALL_TOOLS action using only fields and types "
                            "from ALLOWED_TOOLS_JSON; do not repeat unknown fields."
                        ),
                    )
                )
                if self.event_sink is not None:
                    await self.event_sink.emit(
                        "agent.completed", {"round": round_number, "status": "INVALID"}
                    )
                continue
            total_tools += len(batch_results)
            batch_results = refreshed + batch_results
            results.extend(batch_results)
            observed_ids = set(context.catalog_evidence_book_ids)
            for item in batch_results:
                observed_ids.update(_catalog_book_ids(item.get("value")))
            context = context.model_copy(update={"catalog_evidence_book_ids": frozenset(observed_ids)})
            messages.append(ModelMessage(role="assistant", content=response.content))
            for item in batch_results:
                reuse_instruction = (
                    " IDENTICAL_READ_REUSED=true; do not repeat this call again. "
                    "Return FINAL if the available evidence answers the request; otherwise change the query or tool arguments."
                    if item.get("reused")
                    else ""
                )
                messages.append(
                    ModelMessage(
                        role="system",
                        content=(
                            "LOCAL_TOOL_RESULT_JSON="
                            + self._tool_context_content(item, rag_budget)
                            + reuse_instruction
                        ),
                    )
                )
            if self.event_sink is not None:
                await self.event_sink.emit("agent.completed", {"round": round_number, "status": "COMPLETED"})
        if final_action is None:
            raise SingleAgentError("TOOL_BUDGET_EXCEEDED", "model did not produce a final answer within six rounds")
        # Recommendations are presentation output, not authoritative facts.
        # Re-read only IDs already observed from this Run's Catalog evidence;
        # this refreshes price/stock/status without allowing the model to
        # smuggle an arbitrary book ID into the final answer.
        refresh_ids: list[int] = []
        for recommendation in final_action.recommendations:
            try:
                book_id = int(recommendation.book_id)
            except (TypeError, ValueError):
                continue
            if book_id in context.catalog_evidence_book_ids and book_id not in refresh_ids:
                refresh_ids.append(book_id)
        if refresh_ids and "get_book" in allowed:
            if total_tools + len(refresh_ids) > self.limits.max_tool_calls:
                raise SingleAgentError("TOOL_BUDGET_EXCEEDED", "catalog refresh exceeded the tool budget")
            refresh_calls = [
                SingleAgentToolCall(
                    call_id=f"catalog-refresh-{book_id}",
                    name="get_book",
                    arguments={"book_id": book_id},
                )
                for book_id in refresh_ids
            ]
            refreshed: list[dict[str, Any]] = []
            for offset in range(0, len(refresh_calls), self.limits.max_parallel_reads):
                batch = await self._invoke_batch(
                    refresh_calls[offset : offset + self.limits.max_parallel_reads],
                    context=context,
                    allowed_tools=allowed,
                    permissions=permissions,
                )
                refreshed.extend(batch)
            results.extend(refreshed)
            total_tools += len(refreshed)
        issues: list[dict[str, Any]] = []
        if self.final_validator is not None:
            issues = await self.final_validator(final_action, results)
        repaired = False
        if issues:
            if self.repair_provider is None:
                final_action = self._conservative(final_action, issues)
            else:
                final_action = await self.repair_provider(final_action, issues, results)
                repaired = True
                if self.final_validator is not None:
                    remaining = await self.final_validator(final_action, results)
                    if remaining:
                        final_action = self._conservative(final_action, remaining)
        if self.event_sink is not None:
            await self.event_sink.emit("verification.completed", {"issue_count": len(issues), "repaired": repaired})
        return SingleAgentResult(
            run_id=current_run,
            answer=final_action.answer or "",
            action=final_action,
            tool_results=results,
            rounds=round_number,
            tool_calls=total_tools,
            repaired=repaired,
        )

    def _historical_book_ids_to_refresh(
        self,
        calls: list[SingleAgentToolCall],
        context: ToolContext,
        history_book_ids: frozenset[int],
        allowed: frozenset[str],
    ) -> list[int]:
        requested: set[int] = set()
        for call in calls:
            if call.name != "search_book_knowledge":
                continue
            try:
                arguments = self.tools.get(call.name).input_model.model_validate(call.arguments)
            except ValidationError:
                # Keep malformed arguments on the registry's existing repair path.
                continue
            requested.update(getattr(arguments, "book_ids", ()))

        missing = requested - set(context.catalog_evidence_book_ids)
        if not missing:
            return []
        if not missing.issubset(history_book_ids):
            raise SingleAgentError(
                "EVIDENCE_MISMATCH",
                "book knowledge IDs must be current Catalog evidence or recently displayed recommendations",
            )
        if "get_book" not in allowed or "get_book" not in self.tools.names:
            raise SingleAgentError(
                "TOOL_PERMISSION_DENIED",
                "refreshing displayed recommendations requires get_book in the tool allowlist",
            )
        return sorted(missing)

    @staticmethod
    def _refresh_call_id(book_id: int, calls: list[SingleAgentToolCall]) -> str:
        call_ids = {call.call_id for call in calls}
        call_id = f"history-refresh-{book_id}"
        while call_id in call_ids:
            call_id += "x"
        return call_id

    def _initial_messages(
        self,
        user_request: str,
        context_items: list[Mapping[str, Any]],
        *,
        allowed_tools: frozenset[str],
    ) -> list[ModelMessage]:
        system = self.system_prompt or (
            "You are the bookstore's single customer-service Agent. "
            "Use only the supplied tools and evidence. Never invent live price, stock, order, policy, or evidence. "
            "Return exactly one JSON action: CALL_TOOLS or FINAL. "
            "Do not call create_order or create_inventory_watch; they are not model tools."
        )
        # The order is part of the v2 context contract.  Keep the current user
        # request in its own final message so trimming can never delete it.
        capabilities: dict[str, Any] = {
            "guest": None,
            "authenticated": None,
            "email_verified": None,
        }
        preferences: list[Mapping[str, Any]] = []
        history: list[Mapping[str, Any]] = []
        effective_request: dict[str, Any] = {}
        for item in context_items:
            kind = item.get("kind")
            if kind == "capabilities":
                capabilities.update({key: value for key, value in item.items() if key != "kind"})
            elif kind == "preference":
                preferences.append(item)
            elif kind == "history":
                history.append(item)
            elif kind == "effective_request":
                effective_request = {key: value for key, value in item.items() if key != "kind"}
        tools = [self.tools.get(name).manifest() for name in sorted(allowed_tools) if name in self.tools.names]
        return [
            ModelMessage(role="system", content=system),
            ModelMessage(role="system", content="ALLOWED_TOOLS_JSON=" + json.dumps(tools, ensure_ascii=False, separators=(",", ":"))),
            ModelMessage(role="system", content="USER_CAPABILITIES_JSON=" + json.dumps(capabilities, ensure_ascii=False, separators=(",", ":"))),
            ModelMessage(role="system", content="EXPLICIT_PREFERENCES_JSON=" + json.dumps(preferences, ensure_ascii=False, separators=(",", ":"))),
            ModelMessage(role="system", content="SHORT_HISTORY_JSON=" + json.dumps(history, ensure_ascii=False, separators=(",", ":"))),
            ModelMessage(role="system", content="EFFECTIVE_REQUEST_JSON=" + json.dumps(effective_request, ensure_ascii=False, separators=(",", ":"))),
            ModelMessage(role="user", content=user_request),
        ]

    def _tool_schemas(self, allowed_tools: frozenset[str]) -> list[dict[str, Any]]:
        return [self.tools.get(name).manifest() for name in sorted(allowed_tools) if name in self.tools.names]

    def _tool_context_content(self, item: dict[str, Any], rag_budget: dict[str, int]) -> str:
        value = item.get("value")
        if not isinstance(value, dict):
            value = {"value": value}
        # Tool results are separately bounded so one pathological description
        # cannot evict the original request. RAG text is trimmed first, while
        # IDs and live Catalog fields remain available for deterministic checks.
        value = json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        self._trim_rag_chunks(value, rag_budget)
        envelope = {
            "tool": item.get("tool"),
            "evidence_ids": list(item.get("evidence_ids") or []),
            "value": value,
        }
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= self.limits.tool_result_max_bytes:
            return encoded

        # Never cut the UTF-8/JSON byte string itself.  Compact low-value
        # narrative fields first, then keep a small deterministic set of live
        # identifiers and facts.  The final fallback is still a valid JSON
        # envelope, and explicitly tells the model that context was lost.
        compact = {
            "tool": str(envelope.get("tool") or "")[:128],
            "evidence_ids": list(envelope.get("evidence_ids") or [])[:64],
            "value": self._compact_value(envelope.get("value")),
            "context_truncated": True,
        }
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= self.limits.tool_result_max_bytes:
            return encoded

        compact["evidence_ids"] = compact["evidence_ids"][:8]
        compact["value"] = self._essential_value(envelope.get("value"))
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= self.limits.tool_result_max_bytes:
            return encoded

        compact["tool"] = "tool"
        compact["evidence_ids"] = []
        compact["value"] = {"truncated": True}
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    def _compact_value(self, value: Any) -> Any:
        if not isinstance(value, dict):
            return {"truncated": True}
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"chunks", "books", "items", "results"} and isinstance(item, list):
                rows = []
                for row in item[:50]:
                    if not isinstance(row, dict):
                        continue
                    copy = dict(row)
                    if "content" in copy:
                        tokens = self.encoding.encode(str(copy["content"]))[:256]
                        copy["content"] = self.encoding.decode(tokens)
                        copy["context_truncated"] = True
                    for noisy in ("description", "body", "text", "subtitle"):
                        copy.pop(noisy, None)
                    rows.append(copy)
                compact[key] = rows
            elif key in {"description", "body", "text"}:
                tokens = self.encoding.encode(str(item))[:128]
                compact[key] = self.encoding.decode(tokens)
            else:
                compact[key] = item
        return compact

    @staticmethod
    def _essential_value(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {"truncated": True}
        essential_keys = {
            "book_id", "isbn", "title", "status", "available_stock", "price", "currency",
            "language_code", "authors", "categories", "order_id", "confirmation_id", "total_amount",
        }
        output = {key: value[key] for key in value if key in essential_keys}
        for key in ("books", "items", "results"):
            rows = value.get(key)
            if isinstance(rows, list):
                output[key] = [
                    {field: row[field] for field in essential_keys if isinstance(row, dict) and field in row}
                    for row in rows[:20]
                    if isinstance(row, dict)
                ]
        output["truncated"] = True
        return output

    def _trim_rag_chunks(self, value: dict[str, Any], rag_budget: dict[str, int]) -> None:
        chunks = value.get("chunks")
        if not isinstance(chunks, list):
            return
        used = rag_budget["used"]
        kept = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            candidate = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
            size = len(self.encoding.encode(candidate))
            if used + size > self.limits.rag_max_context_tokens and kept:
                continue
            if used + size > self.limits.rag_max_context_tokens:
                remaining = self.limits.rag_max_context_tokens - used
                if remaining <= 0:
                    break
                clone = dict(chunk)
                content = str(clone.get("content", ""))
                token_slice = self.encoding.encode(content)[:remaining]
                clone["content"] = self.encoding.decode(token_slice)
                clone["context_truncated"] = True
                kept.append(clone)
                # A truncated first result consumes the complete deterministic
                # RAG budget; later calls cannot inject another 6000 tokens.
                rag_budget["used"] = self.limits.rag_max_context_tokens
                break
            kept.append(chunk)
            used += size
            rag_budget["used"] = used
        value["chunks"] = kept

    async def _validate_tool_batch(self, calls: list[SingleAgentToolCall], allowed: frozenset[str]) -> None:
        call_ids = [call.call_id for call in calls]
        if len(call_ids) != len(set(call_ids)):
            raise SingleAgentError("MODEL_OUTPUT_INVALID", "tool call IDs must be unique")
        if len(calls) > self.limits.max_parallel_reads:
            raise SingleAgentError("TOOL_BUDGET_EXCEEDED", "too many tool calls in one model round")
        try:
            specs = [self.tools.get(call.name) for call in calls]
        except Exception as exc:
            raise SingleAgentError("MODEL_OUTPUT_INVALID", "model selected an unknown tool") from exc
        if any(call.name not in allowed for call in calls):
            raise SingleAgentError("TOOL_PERMISSION_DENIED", "model selected a tool outside the v2 allowlist")
        if len(calls) > 1 and any(spec.risk != RiskLevel.READ_ONLY for spec in specs):
            raise SingleAgentError("MODEL_OUTPUT_INVALID", "only READ_ONLY tools may be batched")
        if any(_contains_unresolved_reference(call.arguments) for call in calls):
            raise SingleAgentError("MODEL_OUTPUT_INVALID", "tool arguments cannot reference another same-round result")

    async def _validate_required_reads(
        self,
        calls: tuple[SingleAgentToolCall, ...],
        allowed: frozenset[str],
    ) -> None:
        """Restrict server-selected prefetches to the normal read-only tool path."""
        call_ids = [call.call_id for call in calls]
        if len(call_ids) != len(set(call_ids)):
            raise SingleAgentError("MODEL_OUTPUT_INVALID", "required read call IDs must be unique")
        for call in calls:
            try:
                spec = self.tools.get(call.name)
            except Exception as exc:
                raise SingleAgentError("MODEL_OUTPUT_INVALID", "required read selected an unknown tool") from exc
            if call.name not in allowed or spec.risk != RiskLevel.READ_ONLY:
                raise SingleAgentError("TOOL_PERMISSION_DENIED", "required reads must use an allowed READ_ONLY tool")
            if _contains_unresolved_reference(call.arguments):
                raise SingleAgentError("MODEL_OUTPUT_INVALID", "required read arguments must be concrete")

    async def _invoke_batch(
        self,
        calls: list[SingleAgentToolCall],
        *,
        context: ToolContext,
        allowed_tools: frozenset[str],
        permissions: frozenset[str],
        read_cache: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        async def invoke(call: SingleAgentToolCall) -> dict[str, Any]:
            call = self._apply_explicit_catalog_constraints(call, context.explicit_constraints)
            spec = self.tools.get(call.name)
            cache_key = json.dumps(
                {"name": call.name, "arguments": call.arguments},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if read_cache is not None and spec.risk == RiskLevel.READ_ONLY and cache_key in read_cache:
                cached = dict(read_cache[cache_key])
                cached["call_id"] = call.call_id
                cached["reused"] = True
                return cached
            if self.event_sink is not None:
                await self.event_sink.emit("tool.started", {"tool": call.name, "call_id": call.call_id})
            try:
                result: ToolResult = await self.tools.invoke(
                    call.name,
                    call.arguments,
                    agent_name="single_agent",
                    agent_tools=allowed_tools,
                    permissions=permissions,
                    context=context,
                )
            except ToolError as exc:
                if self.event_sink is not None:
                    await self.event_sink.emit("tool.completed", {"tool": call.name, "status": "FAILED", "code": exc.code})
                raise SingleAgentError(exc.code, str(exc), retryable=exc.retryable, details=exc.details) from exc
            item = {"call_id": call.call_id, "tool": call.name, "value": result.value, "evidence_ids": result.evidence_ids}
            if read_cache is not None and spec.risk == RiskLevel.READ_ONLY:
                read_cache[cache_key] = dict(item)
            if self.event_sink is not None and result.evidence_ids:
                await self.event_sink.emit(
                    "evidence.retrieved",
                    {"tool": call.name, "evidence_ids": list(result.evidence_ids)},
                )
            if self.event_sink is not None:
                await self.event_sink.emit("tool.completed", {"tool": call.name, "call_id": call.call_id, "status": "COMPLETED", "evidence_ids": result.evidence_ids})
            return item

        if len(calls) == 1:
            return [await invoke(calls[0])]
        # Only READ_ONLY batches reach this branch. Preserve model order when
        # gathering because completion order is not a stable context contract.
        return list(await asyncio.gather(*(invoke(call) for call in calls)))

    @staticmethod
    def _apply_explicit_catalog_constraints(
        call: SingleAgentToolCall,
        constraints: Mapping[str, Any],
    ) -> SingleAgentToolCall:
        """Re-bind hard Catalog filters at the tool boundary.

        The model may choose search keywords and a narrower sort/order, but it
        cannot omit or weaken a condition explicitly stated by the user.  The
        normalized arguments are then what ToolRegistry validates and traces.
        """
        if call.name != "search_books" or not constraints:
            return call
        arguments = dict(call.arguments)
        for field in ("max_price", "language", "in_stock"):
            value = constraints.get(field)
            if value is not None:
                arguments[field] = value
        authors = constraints.get("authors") or []
        if len(authors) == 1:
            arguments["author"] = str(authors[0])
        categories = constraints.get("categories") or []
        if len(categories) == 1:
            arguments["category"] = str(categories[0])
        return call.model_copy(update={"arguments": arguments})

    @staticmethod
    def _conservative(action: SingleAgentAction, issues: list[dict[str, Any]]) -> SingleAgentAction:
        issue_text = "; ".join(str(issue.get("message") or issue.get("code") or "证据校验失败") for issue in issues)
        return SingleAgentAction(
            action="FINAL",
            answer=f"我无法用当前业务证据完整确认这项回答：{issue_text}",
            addressed_question_ids=action.addressed_question_ids,
        )


def _contains_unresolved_reference(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("$tool.") or value.startswith("$result.")
    if isinstance(value, Mapping):
        return any(_contains_unresolved_reference(key) or _contains_unresolved_reference(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_unresolved_reference(item) for item in value)
    return False


def _catalog_book_ids(value: Any) -> set[int]:
    found: set[int] = set()
    if not isinstance(value, dict):
        return found
    rows = value.get("books")
    if isinstance(rows, list):
        candidates = rows
    else:
        candidates = [value]
    for row in candidates:
        if isinstance(row, Mapping) and type(row.get("book_id")) is int and row["book_id"] > 0:
            found.add(row["book_id"])
    return found


def _latest_history_recommendation_ids(context_items: list[Mapping[str, Any]]) -> frozenset[int]:
    for item in reversed(context_items):
        if item.get("kind") != "history":
            continue
        values = item.get("recommendation_book_ids")
        if not isinstance(values, list):
            return frozenset()
        return frozenset(value for value in values if type(value) is int and value > 0)
    return frozenset()
