from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import AuthUser
from app.api.conversation_context import deterministic_intent, request_context as build_request_context
from app.api.conversation_context import resolve_request, updated_context
from app.api.conversations import ConversationContext, ConversationContextLease, ConversationStore, PendingConfirmation
from app.api.guest_telemetry import GuestTelemetry
from app.harness.single_agent import SingleAgentAction, SingleAgentRuntime
from app.models.provider import ModelMessage, ModelProvider, ModelRequest
from app.models.invocation import invoke_model
from app.memory.errors import MemoryError
from app.memory.service import MemoryService
from app.tools.errors import ToolError
from app.tools.registry import ToolRegistry
from app.tools.types import ToolContext
from app.trace.recorder import TraceRecorder, TraceRecorderFactory
from app.trace.types import RunStatus as TraceRunStatus, StepStatus, StepType


_SENSITIVE_KEYS = frozenset(
    {"access_token", "api_key", "confirmation_token", "password", "smtp_password", "token"}
)


def redact_for_model(value: Any) -> Any:
    """Keep the v2 API module independent from the retired DAG executor."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.casefold() in _SENSITIVE_KEYS else redact_for_model(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_model(item) for item in value]
    return value


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=8000)
    developer_mode: bool = False


class ChatEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    data: dict[str, Any]

    def encode_sse(self) -> str:
        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False, separators=(',', ':'))}\n\n"


PUBLIC_SSE_EVENTS = frozenset(
    {
        "run.started",
        "agent.started",
        "tool.started",
        "tool.completed",
        "agent.completed",
        "evidence.retrieved",
        "verification.completed",
        "confirmation.required",
        "watch_confirmation.required",
        "message.delta",
        "message.completed",
        "run.failed",
    }
)


class QueueEventSink:
    def __init__(
        self,
        queue: asyncio.Queue[ChatEvent | None],
        developer_mode: bool,
        *,
        guest: bool,
        trace_persisted: bool,
    ) -> None:
        self.queue = queue
        self.developer_mode = developer_mode
        self.guest = guest
        self.trace_persisted = trace_persisted
        self.pending_confirmations: list[dict[str, Any]] = []

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        data = dict(data)
        if event_type == "confirmation.prepared.internal":
            self.pending_confirmations.append(data)
            return
        if event_type == "memory.trace.internal":
            # Memory IDs and degradation details are trace-only; they are not
            # part of the public SSE contract even in developer mode.
            return
        if event_type not in PUBLIC_SSE_EVENTS:
            return
        if event_type == "run.started":
            data["trace_persisted"] = self.trace_persisted
            if self.guest or not self.developer_mode:
                data.pop("trace_id", None)
        if not self.developer_mode and event_type in {"agent.started", "agent.completed", "tool.started", "tool.completed"}:
            data = {"status": data.get("status", "RUNNING")}
        safe_data = (
            data
            if event_type in {"confirmation.required", "watch_confirmation.required"}
            else redact_for_model(data)
        )
        await self.queue.put(ChatEvent(event=event_type, data=safe_data))


class ChatService:
    def __init__(
        self,
        provider: ModelProvider,
        agent_model: str,
        tools: ToolRegistry,
        conversations: ConversationStore,
        memory_service: MemoryService | None = None,
        inventory_watch_default_ttl_seconds: int = 30 * 24 * 60 * 60,
        trace_factory: TraceRecorderFactory | None = None,
        guest_telemetry: GuestTelemetry | None = None,
        single_agent_runtime: SingleAgentRuntime | None = None,
    ) -> None:
        self.provider = provider
        self.agent_model = agent_model
        self.tools = tools
        self.conversations = conversations
        self.memory_service = memory_service
        self.inventory_watch_default_ttl_seconds = inventory_watch_default_ttl_seconds
        self.trace_factory = trace_factory
        self.guest_telemetry = guest_telemetry
        self.single_agent_runtime = single_agent_runtime

    async def begin_trace(
        self, conversation_id: str, request: ChatMessageRequest, user: AuthUser | None
    ) -> TraceRecorder | None:
        # DQ-008: guests use a request-local execution and never create application Trace rows.
        if user is None or self.trace_factory is None:
            return None
        return await self.trace_factory.start(
            user_id=user.user_id,
            conversation_id=conversation_id,
            user_request=request.content,
        )

    async def stream(
        self,
        conversation_id: str,
        request: ChatMessageRequest,
        *,
        access_token: str | None,
        user: AuthUser | None,
        recorder: TraceRecorder | None = None,
    ) -> AsyncIterator[ChatEvent]:
        queue: asyncio.Queue[ChatEvent | None] = asyncio.Queue()
        sink = QueueEventSink(
            queue,
            request.developer_mode,
            guest=user is None,
            trace_persisted=recorder is not None,
        )
        task = asyncio.create_task(
            self._execute(
                conversation_id,
                request,
                access_token=access_token,
                user=user,
                sink=sink,
                recorder=recorder,
            )
        )
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _execute(
        self,
        conversation_id: str,
        request: ChatMessageRequest,
        *,
        access_token: str | None,
        user: AuthUser | None,
        sink: QueueEventSink,
        recorder: TraceRecorder | None = None,
    ) -> None:
        run_id = recorder.run.run_id if recorder is not None else str(uuid.uuid4())
        trace_id = run_id if recorder is not None else str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        recorder_token = recorder.bind() if recorder is not None else None
        guest_span_manager = (
            self.guest_telemetry.request_span(model_profile=self.agent_model)
            if user is None and self.guest_telemetry is not None
            else None
        )
        guest_span = await guest_span_manager.__aenter__() if guest_span_manager is not None else None
        context_lease: ConversationContextLease | None = None
        effective_request = None
        await sink.emit("run.started", {"run_id": run_id, "trace_id": trace_id})
        try:
            if user is not None:
                # The API normally binds before streaming; keeping the service
                # self-contained also protects direct callers and tests.
                await self.conversations.bind(conversation_id, user.user_id)
                context_lease = await self.conversations.begin_context(conversation_id, user.user_id)
            memory_context: list[dict[str, Any]] = []
            if user is not None and self.memory_service is not None:
                try:
                    context_reader = getattr(self.memory_service, "context_for_request_with_status", None)
                    if context_reader is None:
                        memory_context = await self.memory_service.context_for_request(user.user_id, request.content)
                        memory_degradation = None
                    else:
                        memory_context, memory_degradation = await context_reader(user.user_id, request.content)
                    if memory_context:
                        if recorder is not None:
                            recorder.register_memory_context(
                                memory_context, request_text=request.content
                            )
                        await sink.emit(
                            "memory.trace.internal",
                            {"operation": "READ", "memory_ids": [item["memory_id"] for item in memory_context]},
                        )
                    if memory_degradation is not None:
                        await sink.emit(
                            "memory.trace.internal",
                            {"operation": "READ", "code": memory_degradation},
                        )
                except Exception as exc:
                    await sink.emit(
                        "memory.trace.internal",
                        {"operation": "READ", "code": _memory_error_code(exc)},
                    )
            # v2 has one model role only.  Explicit filters and question
            # coverage labels are parsed deterministically before the bounded
            # SingleAgent loop; they are not a second hidden model call.
            intent = deterministic_intent(request.content)
            effective_request = resolve_request(
                request.content,
                intent,
                context_lease.context if context_lease is not None else ConversationContext(),
            )
            agent_request_context = build_request_context(request.content, effective_request, memory_context)
            if self.single_agent_runtime is not None:
                # v2 online path: one controlled model loop.
                base_context = ToolContext(
                    trace_id=trace_id,
                    conversation_id=conversation_id,
                    access_token=access_token,
                    authenticated_user_id=user.user_id if user else None,
                    email_verified=user.email_verified if user else False,
                    user_confirmed=False,
                    guest_mode=user is None,
                    explicit_constraints=effective_request.constraints.model_dump(mode="json"),
                    event_sink=sink,
                )
                context_items: list[dict[str, Any]] = [
                    {
                        "kind": "capabilities",
                        "guest": user is None,
                        "authenticated": user is not None,
                        "email_verified": bool(user and user.email_verified),
                    },
                    *[
                        {"kind": "preference", **item}
                        for item in memory_context
                    ],
                ]
                if context_lease is not None:
                    history_items = [
                        {
                            "kind": "history",
                            "request": turn.request,
                            "answer": turn.answer,
                        }
                        for turn in context_lease.context.turns
                    ]
                    if history_items and context_lease.context.displayed_books:
                        history_items[-1]["recommendation_book_ids"] = [
                            book.book_id for book in context_lease.context.displayed_books
                        ]
                    context_items.extend(history_items)
                context_items.append({"kind": "effective_request", **agent_request_context})
                from app.harness.single_agent import SingleAgentRuntime, SingleAgentToolCall
                from app.harness.validator import validate_final_action

                async def final_validator(action, tool_results):
                    from app.harness.catalog_fact import project_catalog_fact
                    project_catalog_fact(action, tool_results, request.content, effective_request.intent.required_question_ids)
                    issues = validate_final_action(
                        action,
                        tool_results,
                        constraints=effective_request.constraints,
                        required_question_ids=effective_request.intent.required_question_ids,
                    )
                    if recorder is not None:
                        step = await recorder.start_step(
                            node_id="validator.final",
                            step_type=StepType.VERIFICATION,
                            input_value={"action": action.model_dump(mode="json")},
                            agent_name="single_agent",
                            model_profile=recorder.run.model_profile,
                            prompt_version=recorder.run.prompt_set_version,
                        )
                        await recorder.finish_step(
                            step,
                            status=StepStatus.COMPLETED,
                            output_value={"issues": issues, "approved": not issues},
                        )
                    return issues

                async def repair_provider(action, issues, tool_results):
                    repair_step = None
                    if recorder is not None:
                        repair_step = await recorder.start_step(
                            node_id="validator.repair",
                            step_type=StepType.REPAIR,
                            input_value={"issues": issues},
                            agent_name="single_agent",
                            model_profile=recorder.run.model_profile,
                            prompt_version=recorder.run.prompt_set_version,
                        )
                    try:
                        response = await invoke_model(
                            self.provider,
                            caller="answer_repair",
                            node_scope="single-agent/repair",
                            request=ModelRequest(
                                model=self.agent_model,
                                temperature=0,
                                response_schema=SingleAgentAction.model_json_schema(),
                                messages=[
                                    ModelMessage(
                                        role="system",
                                        content=(
                                            "Repair the FINAL JSON using only the supplied Catalog and RAG evidence. "
                                            "Do not add a book, price, stock, policy or evidence ID that is absent. "
                                            "Return a FINAL action only."
                                        ),
                                    ),
                                    ModelMessage(
                                        role="user",
                                        content=json.dumps(
                                            {
                                                "request": request.content,
                                                "previous": action.model_dump(mode="json"),
                                                "issues": issues,
                                                "tool_results": result_tool_values(tool_results),
                                            },
                                            ensure_ascii=False,
                                        ),
                                    ),
                                ],
                            ),
                        )
                        repaired = SingleAgentAction.model_validate_json(response.content)
                        if repaired.action != "FINAL":
                            raise ValueError("repair must return FINAL")
                    except (Exception, asyncio.CancelledError) as exc:
                        if recorder is not None:
                            await recorder.finish_step(
                                repair_step,
                                status=StepStatus.FAILED,
                                output_value=None,
                                error_code="RUN_CANCELLED" if isinstance(exc, asyncio.CancelledError) else self._error_code(exc),
                                error_message="answer repair failed",
                            )
                        raise
                    if recorder is not None:
                        await recorder.finish_step(
                            repair_step,
                            status=StepStatus.COMPLETED,
                            output_value={"action": repaired.model_dump(mode="json")},
                        )
                    return repaired

                runtime = SingleAgentRuntime(
                    self.single_agent_runtime.provider,
                    self.single_agent_runtime.model,
                    self.single_agent_runtime.tools,
                    limits=self.single_agent_runtime.limits,
                    system_prompt=self.single_agent_runtime.system_prompt,
                    event_sink=sink,
                    final_validator=final_validator,
                    repair_provider=repair_provider,
                )
                required_reads: tuple[SingleAgentToolCall, ...] = ()
                watch = effective_request.inventory_watch
                if watch is not None and watch.book_identifier:
                    tool_name, arguments = _watch_catalog_read(watch.book_identifier)
                    required_reads = (
                        SingleAgentToolCall(
                            call_id="watch-catalog-refresh",
                            name=tool_name,
                            arguments=arguments,
                        ),
                    )
                result = await runtime.run(
                    request.content,
                    context=base_context,
                    run_id=run_id,
                    context_items=context_items,
                    allowed_tools=self._allowed_tools_for_request(
                        effective_request.intent.required_question_ids,
                        authenticated=user is not None,
                    ),
                    required_reads=required_reads,
                )
                current_books = self._catalog_books(result.tool_results)
                recommendation_ids = []
                for item in result.action.recommendations:
                    try:
                        recommendation_ids.append(int(item.book_id))
                    except (TypeError, ValueError):
                        continue

                # The only model-created write is a non-mutating order preview.
                # Confirmation is still delivered as a card and resolved by
                # the separate deterministic endpoint.
                for preview in sink.pending_confirmations:
                    if user is None:
                        raise ToolError("AUTH_REQUIRED", "authentication is required for order confirmation")
                    await self.conversations.register_confirmation(
                        str(preview["confirmation_token"]),
                        PendingConfirmation(
                            run_id=run_id,
                            trace_id=trace_id,
                            conversation_id=conversation_id,
                            user_id=user.user_id,
                            action_type=str(preview.get("action_type", "CREATE_ORDER")),
                            risk=str(preview.get("risk", "COMMIT")),
                        ),
                    )
                    await sink.emit("confirmation.required", preview)

                if effective_request.inventory_watch is not None and current_books and user is not None:
                    from app.agents.intent import watch_books

                    watch = effective_request.inventory_watch
                    for book in watch_books(current_books, watch):
                        await sink.emit(
                            "watch_confirmation.required",
                            self._watch_confirmation_payload(
                                conversation_id,
                                book,
                                watch,
                                self.inventory_watch_default_ttl_seconds,
                            ),
                        )

                answer = result.answer
                if user is None:
                    answer = answer.rstrip() + "\n\n请登录后使用订单、补货监听、长期记忆或 Trace。游客模式不会执行或保存这些操作。"
                if recorder is not None:
                    if sink.pending_confirmations:
                        traced = await recorder.pause_for_confirmation(final_answer=answer)
                    else:
                        traced = await recorder.finish_run(
                            status=TraceRunStatus.COMPLETED,
                            final_answer=answer,
                        )
                    trace_status = traced.trace_status.value
                else:
                    trace_status = None
                if context_lease is not None and user is not None:
                    await self.conversations.commit_context(
                        conversation_id,
                        user.user_id,
                        context_lease.generation,
                        updated_context(
                            context_lease.context,
                            request.content,
                            answer,
                            effective_request,
                            current_books,
                            recommendation_ids,
                        ),
                    )
                for start in range(0, len(answer), 80):
                    await sink.emit("message.delta", {"content": answer[start : start + 80]})
                await sink.emit(
                    "message.completed",
                    {
                        "content": answer,
                        "recommendations": [
                            item.model_dump(mode="json")
                            for item in result.action.recommendations
                        ],
                        "run_id": run_id,
                        "trace_persisted": recorder is not None,
                        **({"trace_id": trace_id, "trace_status": trace_status} if recorder is not None else {}),
                        "message_id": message_id,
                    },
                )
                from app.telemetry import TASKS
                TASKS.labels("COMPLETED").inc()
                return
            raise RuntimeError("single-agent runtime is required")
        except asyncio.CancelledError:
            from app.telemetry import TASKS
            TASKS.labels("CANCELLED").inc()
            raise
        except Exception as exc:
            from app.telemetry import TASKS
            import logging
            TASKS.labels("FAILED").inc()
            logging.getLogger("bookstore.agent").warning("chat run failed")
            trace_status = None
            if recorder is not None:
                traced_run = await recorder.finish_run(
                    status=TraceRunStatus.FAILED,
                    final_answer=None,
                    error_code=self._error_code(exc),
                    error_message="agent run failed",
                )
                trace_status = traced_run.trace_status.value
            await sink.emit(
                "run.failed",
                {
                    "code": self._error_code(exc),
                    "retryable": self._retryable(exc),
                    "run_id": run_id,
                    "trace_persisted": recorder is not None,
                    **({"trace_id": trace_id, "trace_status": trace_status} if recorder is not None else {}),
                },
            )
            if guest_span is not None:
                guest_span.fail(self._error_code(exc))
        finally:
            if recorder is not None and recorder_token is not None:
                recorder.reset(recorder_token)
            if guest_span_manager is not None:
                await guest_span_manager.__aexit__(None, None, None)
            await sink.queue.put(None)

    @staticmethod
    def _catalog_books(value: Any) -> dict[int, dict[str, Any]]:
        found: dict[int, dict[str, Any]] = {}

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                if {"book_id", "title", "available_stock"}.issubset(item):
                    try:
                        found[int(item["book_id"])] = item
                    except (TypeError, ValueError):
                        pass
                for nested in item.values():
                    visit(nested)
            elif isinstance(item, list):
                for nested in item:
                    visit(nested)

        visit(value)
        return found

    @staticmethod
    def _watch_confirmation_payload(
        conversation_id: str,
        book: dict[str, Any],
        watch: Any,
        default_ttl_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Build the public watch card without creating a task or trusting the model."""
        expires_at = (now or datetime.now(UTC)) + timedelta(
            seconds=watch.duration_seconds or default_ttl_seconds
        )
        return {
            "action_type": "CREATE_INVENTORY_WATCH",
            "risk": "CONFIRM_REQUIRED",
            "conversation_id": conversation_id,
            "book": {
                "book_id": book["book_id"],
                "title": book["title"],
                "available_stock": book.get("available_stock", 0),
            },
            "desired_quantity": watch.desired_quantity,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "submit_path": "/api/tasks/inventory-watches",
        }

    @staticmethod
    def _allowed_tools_for_request(
        question_ids: list[str], *, authenticated: bool
    ) -> frozenset[str]:
        """Expose only tools relevant to deterministic request classes.

        This is a capability boundary, not another model/router. Combined
        requests receive the union; a recommendation cannot drift into policy
        retrieval merely because both tools exist in the global registry.
        """
        labels = set(question_ids)
        allowed: set[str] = set()
        if "catalog_fact" in labels:
            allowed.update({"search_books", "get_book"})
        if "policy" in labels:
            allowed.add("search_policy")
        if labels & {"recommendation", "comparison", "inventory_watch", "answer"}:
            allowed.update({"search_books", "get_book", "search_book_knowledge"})
        if authenticated and "recommendation" in labels:
            allowed.add("prepare_order")
        if authenticated and "order" in labels:
            allowed.update({"get_order", "list_my_orders"})
        return frozenset(allowed or {"search_books", "get_book", "search_book_knowledge"})

    @staticmethod
    def _error_code(exc: Exception) -> str:
        from app.harness.errors import ToolBudgetExceeded
        from app.harness.single_agent import SingleAgentError

        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, ToolError):
                return current.code
            if isinstance(current, SingleAgentError):
                return current.code
            if isinstance(current, ToolBudgetExceeded):
                return "AGENT_BUDGET_EXCEEDED"
            if isinstance(current, (NameError, AttributeError, TypeError)):
                return "INTERNAL_ERROR"
            current = current.__cause__
        return "DEPENDENCY_UNAVAILABLE"

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, ToolError):
                return current.retryable
            current = current.__cause__
        return False


def _watch_catalog_read(identifier: str) -> tuple[str, dict[str, Any]]:
    """Choose the narrowest authoritative read for a parsed watch target."""
    normalized = identifier.strip()
    if normalized.isdigit() and len(normalized) <= 8:
        return "get_book", {"book_id": int(normalized)}
    if 9 <= len(normalized) <= 17 and all(char.isdigit() or char in "Xx" for char in normalized):
        return "get_book", {"isbn": normalized}
    # A title is not a unique business identifier. Search broadly enough to
    # detect duplicates; watch_books will emit a card only for one exact match.
    return "search_books", {"q": normalized, "limit": 50}


def _memory_error_code(exc: Exception) -> str:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, MemoryError):
            return current.code
        current = current.__cause__
    return "MEMORY_DEPENDENCY_UNAVAILABLE"


def result_tool_values(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep repair input bounded and free of internal executor objects."""
    return [
        {
            "tool": item.get("tool"),
            "value": redact_for_model(item.get("value", {})),
            "evidence_ids": list(item.get("evidence_ids") or []),
        }
        for item in results
    ]
