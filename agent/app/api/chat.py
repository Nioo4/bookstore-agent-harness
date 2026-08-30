from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict, Field

from app.agents.evidence import EvidenceLedger
from app.agents.execution import ProfessionalAgentExecutor, redact_for_model
from app.agents.registry import AgentRegistry
from app.agents.intent import INTENT_PROMPT, UserIntent, watch_books
from app.agents.verification import (
    AgentDraft,
    PolicyClaim,
    RepairingVerifier,
    UserConstraints,
    VerificationIssue,
    VerificationRequest,
)
from app.api.auth import AuthUser
from app.api.conversation_context import request_context as build_request_context
from app.api.conversation_context import resolve_request, updated_context
from app.api.conversations import ConversationContext, ConversationContextLease, ConversationStore, PendingConfirmation
from app.api.guest_telemetry import GuestTelemetry
from app.harness.checkpoint import CheckpointStore, InMemoryCheckpointStore
from app.harness.errors import PlanGenerationError, ToolBudgetExceeded
from app.harness.plan import PlanLimits
from app.harness.runner import DagRunner
from app.harness.routing import required_agents_for_request
from app.harness.supervisor import Supervisor
from app.models.provider import ModelMessage, ModelProvider, ModelRequest
from app.models.invocation import invoke_model
from app.memory.extractor import MemoryExtractor
from app.memory.errors import MemoryError
from app.memory.service import MemoryService
from app.tools.errors import ToolError
from app.tools.registry import ToolRegistry
from app.tools.types import ToolContext
from app.trace.recorder import TraceRecorder, TraceRecorderFactory
from app.trace.types import RunStatus as TraceRunStatus, StepStatus, StepType, TraceStatus


_VAGUE_BOOK_REQUEST = re.compile(
    r"(?:想(?:买|找)|推荐).{0,20}(?:技术书|图书|书籍).{0,40}(?:还没想好|没想好|不确定)",
    re.IGNORECASE,
)
_PREFERENCE_SAVE_REQUEST = re.compile(r"(?:请|帮我)?记住[，,:：]|保存.{0,16}?(?:偏好|记忆)", re.IGNORECASE)
_PREFERENCE_POLICY_QUERY = "可保存的阅读偏好"


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

    @staticmethod
    def _safe_memory_trace(data: dict[str, Any]) -> dict[str, Any]:
        safe = dict(data)
        memory_ids = safe.get("memory_ids")
        if isinstance(memory_ids, list):
            safe["memory_ids"] = ["[REDACTED_MEMORY]" for _ in memory_ids]
        return safe

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        data = dict(data)
        if event_type == "confirmation.prepared.internal":
            self.pending_confirmations.append(data)
            return
        if event_type == "memory.trace.internal":
            if self.developer_mode:
                await self.queue.put(ChatEvent(event="memory.trace", data=self._safe_memory_trace(data)))
            return
        if event_type == "run.started":
            data["trace_persisted"] = self.trace_persisted
            if self.guest or not self.developer_mode:
                data.pop("trace_id", None)
        if not self.developer_mode and event_type in {"agent.started", "agent.completed", "tool.started", "tool.completed"}:
            data = {"status": data.get("status", "RUNNING")}
        safe_data = data if event_type == "confirmation.required" else redact_for_model(data)
        await self.queue.put(ChatEvent(event=event_type, data=safe_data))


class ChatService:
    def __init__(
        self,
        provider: ModelProvider,
        agent_model: str,
        supervisor: Supervisor,
        agents: AgentRegistry,
        tools: ToolRegistry,
        verifier: RepairingVerifier,
        checkpoint_store: CheckpointStore,
        langgraph_checkpointer: BaseCheckpointSaver[Any],
        limits: PlanLimits,
        conversations: ConversationStore,
        memory_service: MemoryService | None = None,
        memory_extractor: MemoryExtractor | None = None,
        inventory_watch_default_ttl_seconds: int = 30 * 24 * 60 * 60,
        trace_factory: TraceRecorderFactory | None = None,
        guest_telemetry: GuestTelemetry | None = None,
        prompt_loader=None,
    ) -> None:
        self.provider = provider
        self.agent_model = agent_model
        self.supervisor = supervisor
        self.agents = agents
        self.tools = tools
        self.verifier = verifier
        self.checkpoint_store = checkpoint_store
        self.langgraph_checkpointer = langgraph_checkpointer
        self.limits = limits
        self.conversations = conversations
        self.memory_service = memory_service
        self.memory_extractor = memory_extractor
        self.inventory_watch_default_ttl_seconds = inventory_watch_default_ttl_seconds
        self.trace_factory = trace_factory
        self.guest_telemetry = guest_telemetry
        self.prompt_loader = prompt_loader

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
        validated_plan = None
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
            intent = await self._extract_intent(request.content)
            effective_request = resolve_request(
                request.content,
                intent,
                context_lease.context if context_lease is not None else ConversationContext(),
            )
            agent_request_context = build_request_context(request.content, effective_request, memory_context)
            plan_step = (
                await recorder.start_step(
                    node_id="supervisor.plan",
                    step_type=StepType.PLAN,
                    input_value={"request": request.content, "memory_ids": [item.get("memory_id") for item in memory_context]},
                    agent_name="supervisor",
                    model_profile=recorder.run.model_profile,
                    prompt_version=recorder.run.prompt_set_version,
                )
                if recorder is not None
                else None
            )
            validated_plan = await self.supervisor.plan(
                request.content,
                memory_context=memory_context,
                conversation_context=effective_request.context_for_agents,
                allowed_agents=(
                    frozenset({"catalog", "recommendation", "knowledge", "verifier"})
                    if user is None
                    else None
                ),
            )
            if recorder is not None:
                await recorder.finish_step(
                    plan_step,
                    status=StepStatus.COMPLETED,
                    output_value=validated_plan.plan.model_dump(mode="json"),
                )
            plan_data: dict[str, Any]
            if request.developer_mode:
                plan_data = validated_plan.plan.model_dump()
            else:
                plan_data = {"goal": validated_plan.plan.goal, "node_count": len(validated_plan.plan.nodes)}
            await sink.emit("plan.created", plan_data)

            evidence = EvidenceLedger()
            base_context = ToolContext(
                trace_id=trace_id,
                conversation_id=conversation_id,
                access_token=access_token,
                authenticated_user_id=user.user_id if user else None,
                email_verified=user.email_verified if user else False,
                user_confirmed=False,
                guest_mode=user is None,
                event_sink=sink,
            )
            executor = ProfessionalAgentExecutor(
                self.provider,
                self.agent_model,
                self.agents,
                self.tools,
                base_context,
                evidence,
                prompt_loader=self.prompt_loader,
                request_context=agent_request_context,
            )
            runner = DagRunner(
                executor.handlers(),
                self.checkpoint_store if user is not None else InMemoryCheckpointStore(),
                self.langgraph_checkpointer if user is not None else InMemorySaver(),
                self.limits,
                event_sink=sink,
            )
            run_result = await runner.run(
                validated_plan,
                run_id=run_id,
                user_request=json.dumps(
                    {"content": request.content, "memory_context": memory_context},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            if user is not None:
                await self._add_preference_policy_evidence(
                    request.content,
                    evidence,
                    base_context,
                )
            draft = self._safety_boundary_draft(
                request.content,
                effective_request.intent.required_question_ids,
            ) or self._clarification_draft(
                request.content,
                effective_request.intent.required_question_ids,
            ) or self._metadata_gap_draft(
                request.content,
                evidence,
                effective_request.intent.required_question_ids,
            ) or self._preference_memory_draft(
                request.content,
                evidence,
                effective_request.intent.required_question_ids,
            ) or await self._draft_answer(
                request.content,
                run_result.outputs,
                evidence,
                agent_request_context,
            )

            required_routes = set(required_agents_for_request(request.content))
            if effective_request.uses_context and "knowledge" not in required_routes:
                # A resolved page reference is only a locator. Its answer/card
                # still needs a fresh catalog observation in this run. Do not
                # add an unrelated catalog requirement to a policy-only
                # follow-up that happens to share the same page session.
                required_routes.add("catalog")
            verification_request = VerificationRequest(
                draft=draft,
                user_request=request.content,
                constraints=effective_request.constraints,
                required_question_ids=effective_request.intent.required_question_ids,
                requires_catalog_evidence="catalog" in required_routes,
                requires_policy_evidence="knowledge" in required_routes,
                evidence=evidence,
            )

            async def repair(previous: AgentDraft, issues: list[VerificationIssue]) -> AgentDraft:
                repair_step = (
                    await recorder.start_step(
                        node_id="verifier.repair",
                        step_type=StepType.REPAIR,
                        input_value={
                            "draft": previous.model_dump(mode="json"),
                            "issue_codes": [issue.code for issue in issues],
                        },
                        agent_name="verifier",
                        model_profile=recorder.run.model_profile,
                        prompt_version=recorder.run.prompt_set_version,
                    )
                    if recorder is not None
                    else None
                )
                response = await invoke_model(self.provider, caller="answer_synthesizer", node_scope="answer_repair",
                    request=ModelRequest(
                        model=self.agent_model,
                        temperature=0,
                        response_schema=AgentDraft.model_json_schema(),
                        messages=[
                            ModelMessage(
                                role="system",
                                content=(
                                    "Repair the draft once using only existing evidence. Do not invent facts or evidence IDs. "
                                    "Rules claims must include [《document title》vversion，section]. If search_policy returned no "
                                    "chunks, say exactly 无法从当前知识库确定该规则。 and emit no policy claims."
                                ),
                            ),
                            ModelMessage(
                                role="user",
                                content=json.dumps(
                                    {
                                        "draft": previous.model_dump(),
                                        "user_request": request.content,
                                        "constraints": effective_request.constraints.model_dump(),
                                        "required_question_ids": effective_request.intent.required_question_ids,
                                        "issues": [issue.model_dump() for issue in issues],
                                        "evidence": self._safe_evidence(evidence),
                                    },
                                    ensure_ascii=False,
                                ),
                            ),
                        ],
                    )
                )
                repaired = AgentDraft.model_validate_json(response.content)
                if recorder is not None:
                    await recorder.finish_step(
                        repair_step,
                        status=StepStatus.COMPLETED,
                        output_value=repaired.model_dump(mode="json"),
                    )
                return repaired

            verification_step = (
                await recorder.start_step(
                    node_id="verifier.final",
                    step_type=StepType.VERIFICATION,
                    input_value={"draft": draft.model_dump(mode="json")},
                    agent_name="verifier",
                    model_profile=recorder.run.model_profile,
                    prompt_version=recorder.run.prompt_set_version,
                )
                if recorder is not None
                else None
            )
            verified = await self.verifier.verify_once_with_repair(verification_request, repair)
            if verified.draft is None:
                empty_catalog_draft = self._empty_catalog_draft(
                    evidence,
                    effective_request.intent.required_question_ids,
                )
                if empty_catalog_draft is not None:
                    empty_catalog_request = verification_request.model_copy(
                        update={"draft": empty_catalog_draft}
                    )
                    empty_catalog_verification = self.verifier.pipeline.deterministic.verify(
                        empty_catalog_request
                    )
                    if empty_catalog_verification.approved:
                        verified = verified.model_copy(
                            update={
                                "draft": empty_catalog_draft,
                                "verification": empty_catalog_verification,
                                "conservative_answer": None,
                            }
                        )
            if recorder is not None:
                await recorder.finish_step(
                    verification_step,
                    status=StepStatus.COMPLETED,
                    output_value={
                        "approved": verified.verification.approved,
                        "repaired": verified.repaired,
                        "issue_codes": [issue.code for issue in verified.verification.issues],
                    },
                )
            await sink.emit(
                "verification.completed",
                {
                    "approved": verified.verification.approved,
                    "repaired": verified.repaired,
                    "issues": [issue.code for issue in verified.verification.issues],
                },
            )

            if (
                verified.verification.approved
                and user is not None
                and self.memory_service is not None
                and self.memory_extractor is not None
            ):
                try:
                    candidates = await self.memory_extractor.extract(request.content, source_message_id=message_id)
                    results = (
                        await self.memory_service.apply_candidates(
                            user.user_id,
                            candidates,
                            conversation_id=conversation_id,
                            message_id=message_id,
                        )
                        if candidates
                        else []
                    )
                    memory_ids = [result.memory.memory_id for result in results if result.memory is not None]
                    await sink.emit(
                        "memory.trace.internal",
                        {"operation": "WRITE", "memory_ids": memory_ids, "candidate_count": len(candidates)},
                    )
                except Exception as exc:
                    await sink.emit(
                        "memory.trace.internal",
                        {"operation": "WRITE", "code": _memory_error_code(exc)},
                    )

            previews = sink.pending_confirmations
            for preview in previews:
                if user is None:
                    raise ToolError("AUTH_REQUIRED", "authentication is required for order confirmation")
                await self.conversations.register_confirmation(
                    str(preview["confirmation_token"]),
                    PendingConfirmation(
                        run_id=run_id,
                        trace_id=trace_id,
                        conversation_id=conversation_id,
                        user_id=user.user_id,
                        action_type=str(preview.get("action_type", "")),
                        risk=str(preview.get("risk", "")),
                        validated_plan=validated_plan,
                    ),
                )
                await sink.emit("confirmation.required", preview)

            if verified.draft is not None and user is not None:
                from app.agents.grounding import catalog_books
                current_books = catalog_books(evidence)
                watch = effective_request.inventory_watch
                candidates = watch_books(current_books, watch) if watch is not None else []
                for book in candidates:
                    expires_at = datetime.now(UTC) + timedelta(seconds=(
                        watch.duration_seconds if watch and watch.duration_seconds is not None
                        else self.inventory_watch_default_ttl_seconds
                    ))
                    await sink.emit(
                        "confirmation.required",
                        {
                            "action_type": "CREATE_INVENTORY_WATCH",
                            "risk": "COMMIT",
                            "conversation_id": conversation_id,
                            "book": {
                                "book_id": book["book_id"],
                                "title": str(book["title"]),
                                "available_stock": book["available_stock"],
                            },
                            "desired_quantity": watch.desired_quantity if watch else 1,
                            "expires_at": expires_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                            "submit_path": "/api/tasks/inventory-watches",
                        },
                    )

            answer = verified.draft.answer if verified.draft is not None else verified.conservative_answer or "Unable to verify an answer."
            if user is None:
                answer = (
                    answer.rstrip()
                    + "\n\n请登录后使用订单、补货监听、长期记忆或可查询 Trace 功能；游客模式不会执行或保存这些操作。"
                )
            if verified.draft is not None and user is not None and context_lease is not None:
                from app.agents.grounding import catalog_books
                await self.conversations.commit_context(
                    conversation_id,
                    user.user_id,
                    context_lease.generation,
                    updated_context(
                        context_lease.context,
                        request.content,
                        answer,
                        effective_request,
                        catalog_books(evidence),
                        [item.book_id for item in verified.draft.recommendations],
                    ),
                )
            trace_status = None
            if recorder is not None:
                if run_result.status.value == "WAITING_CONFIRMATION":
                    traced_run = await recorder.pause_for_confirmation(
                        final_answer=answer,
                        dag=validated_plan.plan.model_dump(mode="json"),
                    )
                else:
                    traced_run = await recorder.finish_run(
                        status=TraceRunStatus.COMPLETED,
                        final_answer=answer,
                        dag=validated_plan.plan.model_dump(mode="json"),
                        replayable=not bool(memory_context) and not effective_request.uses_context,
                        replay_block_reason=(
                            "CONVERSATION_CONTEXT_USED"
                            if effective_request.uses_context
                            else "MEMORY_CONTEXT_USED" if memory_context else None
                        ),
                    )
                trace_status = traced_run.trace_status.value
            for start in range(0, len(answer), 80):
                await sink.emit("message.delta", {"content": answer[start : start + 80]})
            await sink.emit(
                "message.completed",
                {
                    "content": answer,
                    "recommendations": (
                        [item.model_dump() for item in verified.draft.recommendations]
                        if verified.draft is not None
                        else []
                    ),
                    "run_id": run_id,
                    "trace_persisted": recorder is not None,
                    **({"trace_id": trace_id, "trace_status": trace_status} if recorder is not None else {}),
                    "message_id": message_id,
                },
            )
            if guest_span is not None:
                guest_span.complete()
            from app.telemetry import TASKS
            TASKS.labels("COMPLETED").inc()
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
                    dag=(validated_plan.plan.model_dump(mode="json") if validated_plan is not None else None),
                    error_code=self._error_code(exc),
                    error_message="agent run failed",
                    replayable=False,
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

    async def _extract_intent(self, content: str) -> UserIntent:
        response = await invoke_model(self.provider, caller="intent_extractor",
            request=ModelRequest(
                model=self.agent_model,
                temperature=0,
                response_schema=UserIntent.model_json_schema(),
                messages=[
                    ModelMessage(
                        role="system",
                        content=INTENT_PROMPT,
                    ),
                    ModelMessage(role="user", content=content),
                ],
            )
        )
        return UserIntent.model_validate_json(response.content)

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
    def _metadata_gap_draft(
        user_request: str,
        evidence: EvidenceLedger,
        required_question_ids: list[str],
    ) -> AgentDraft | None:
        """Answer a named book's missing-metadata question without inventing it."""
        if not re.search(r"(?:章节|目录|前置知识|阅读基础|适合.*零基础|零基础.*适合)", user_request):
            return None
        title_match = re.search(r"《([^》]{1,120})》", user_request)
        if title_match is None:
            return None
        from app.agents.grounding import catalog_books

        title = title_match.group(1)
        matches = [book for book in catalog_books(evidence).values() if book.get("title") == title]
        if len(matches) != 1:
            return None
        book = matches[0]
        if str(book.get("description") or "").strip() or str(book.get("subtitle") or "").strip():
            return None
        return AgentDraft(
            answer=(
                f"《{title}》当前书目未提供目录、简介或阅读基础说明；"
                "无法根据现有资料判断它有哪些章节，也无法可靠判断是否适合零基础读者。"
            ),
            addressed_question_ids=list(required_question_ids),
        )

    @staticmethod
    def _empty_catalog_draft(
        evidence: EvidenceLedger,
        required_question_ids: list[str],
    ) -> AgentDraft | None:
        """Keep an observed empty catalog result from falling through to LLM prose."""
        catalog_records = [
            record for record in evidence.snapshot().values() if record.source_tool == "search_books"
        ]
        if not catalog_records:
            return None
        if any(record.payload.get("books") for record in catalog_records):
            return None
        return AgentDraft(
            answer="根据目录查询结果，未找到符合当前条件的书籍。",
            addressed_question_ids=list(required_question_ids),
        )

    @staticmethod
    def _clarification_draft(
        user_request: str,
        required_question_ids: list[str],
    ) -> AgentDraft | None:
        if _VAGUE_BOOK_REQUEST.search(user_request) is None:
            return None
        draft = AgentDraft(
            answer=(
                "可以。请告诉我你更想学习的方向或目前的基础（任选一个即可），"
                "例如分布式、Python、数据库，或刚学过的语言；我再按实时书目推荐。"
            ),
            addressed_question_ids=list(required_question_ids),
        )
        draft._suppress_catalog_listing = True
        return draft

    @staticmethod
    def _preference_memory_draft(
        user_request: str,
        evidence: EvidenceLedger,
        required_question_ids: list[str],
    ) -> AgentDraft | None:
        if _PREFERENCE_SAVE_REQUEST.search(user_request) is None:
            return None
        for record in evidence.snapshot().values():
            if record.source_tool != "search_policy":
                continue
            for chunk in record.payload.get("chunks", []):
                if not isinstance(chunk, dict) or chunk.get("section") != "可保存的阅读偏好":
                    continue
                title = str(chunk.get("document_title", ""))
                version = str(chunk.get("version", "")).removeprefix("v").removeprefix("V")
                source = str(chunk.get("source_path", ""))
                if not title or not version or not source:
                    continue
                citation = f"[《{title}》v{version}，可保存的阅读偏好]"
                return AgentDraft(
                    answer=(
                        "已收到这项明确偏好；后续推荐会优先考虑中文技术书，"
                        "但当前轮的明确条件仍优先。你可以在“记忆”页查看或删除它。 "
                        + citation
                    ),
                    policy_claims=[
                        PolicyClaim(
                            claim="当前规则允许保存用户明确表达的阅读偏好，且用户可以查看和删除自己的长期记忆。",
                            source=source,
                            section="可保存的阅读偏好",
                            version=str(chunk["version"]),
                            evidence_ids=[record.evidence_id],
                        )
                    ],
                    addressed_question_ids=list(required_question_ids),
                )
        return None

    async def _add_preference_policy_evidence(
        self,
        user_request: str,
        evidence: EvidenceLedger,
        context: ToolContext,
    ) -> None:
        """Retrieve the fixed preference rule without changing the planned DAG."""
        if _PREFERENCE_SAVE_REQUEST.search(user_request) is None:
            return
        if any(record.source_tool == "search_policy" for record in evidence.snapshot().values()):
            return
        try:
            knowledge_agent = self.agents.get("knowledge")
            self.tools.get("search_policy")
            result = await self.tools.invoke(
                "search_policy",
                {"query": _PREFERENCE_POLICY_QUERY, "limit": 5},
                agent_name=knowledge_agent.name,
                agent_tools=knowledge_agent.allowed_tools,
                permissions=knowledge_agent.permissions,
                context=context,
            )
        except (ToolError, ValueError):
            # Keep the existing conservative response when the optional policy
            # dependency is unavailable; the durable-memory write path remains
            # separately verified below.
            return
        evidence.record_tool_result("search_policy", result)

    @staticmethod
    def _safety_boundary_draft(
        user_request: str,
        required_question_ids: list[str],
    ) -> AgentDraft | None:
        asks_for_sensitive_value = re.search(
            r"(?:显示|提供|泄露|保存).{0,24}?(?:token|令牌|密码|access[_ -]?token)",
            user_request,
            re.IGNORECASE,
        )
        asks_to_bypass_confirmation = re.search(
            r"(?:忽略|绕过).{0,16}?(?:确认|规则)|(?:直接).{0,16}?(?:购买|下单)",
            user_request,
            re.IGNORECASE,
        )
        # This code-owned response is deliberately narrow: ordinary safety
        # discussion and prompt-injection tests still exercise the normal
        # evidence/verifier path. It covers the concrete combined attempt to
        # disclose a credential while bypassing the order confirmation gate.
        if asks_for_sensitive_value is None or asks_to_bypass_confirmation is None:
            return None
        from app.agents.grounding import SECURITY_BOUNDARY_ANSWER

        return AgentDraft(
            answer=SECURITY_BOUNDARY_ANSWER,
            addressed_question_ids=list(required_question_ids),
        )

    async def _draft_answer(
        self,
        user_request: str,
        outputs: dict[str, Any],
        evidence: EvidenceLedger,
        effective_request: dict[str, Any] | None = None,
    ) -> AgentDraft:
        for output in reversed(list(outputs.values())):
            try:
                return AgentDraft.model_validate(output)
            except Exception:
                continue
        response = await invoke_model(self.provider, caller="answer_synthesizer",
            request=ModelRequest(
                model=self.agent_model,
                temperature=0,
                response_schema=AgentDraft.model_json_schema(),
                messages=[
                    ModelMessage(
                        role="system",
                        content=(
                            "Synthesize an answer using only supplied outputs and evidence. Never copy [REDACTED] values, "
                            "invent facts, or claim an unsupported policy. Rules claims must include "
                            "[《document title》vversion，section]. If search_policy returned no chunks, say exactly "
                            "无法从当前知识库确定该规则。 and emit no policy claims. Return JSON only."
                        ),
                    ),
                    ModelMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "request": user_request,
                                "effective_request": effective_request or {"user_request": user_request},
                                "outputs": redact_for_model(outputs),
                                "evidence": self._safe_evidence(evidence),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ],
            )
        )
        return AgentDraft.model_validate_json(response.content)

    @staticmethod
    def _safe_evidence(evidence: EvidenceLedger) -> dict[str, Any]:
        return {
            evidence_id: redact_for_model(record.model_dump(mode="json"))
            for evidence_id, record in evidence.snapshot().items()
        }

    @staticmethod
    def _error_code(exc: Exception) -> str:
        current: BaseException | None = exc
        while current is not None:
            if isinstance(current, ToolError):
                return current.code
            if isinstance(current, PlanGenerationError):
                return "AGENT_PLAN_INVALID"
            if isinstance(current, ToolBudgetExceeded):
                return "AGENT_BUDGET_EXCEEDED"
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


def _memory_error_code(exc: Exception) -> str:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, MemoryError):
            return current.code
        current = current.__cause__
    return "MEMORY_DEPENDENCY_UNAVAILABLE"
