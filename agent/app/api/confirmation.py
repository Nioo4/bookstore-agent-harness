from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import AuthUser
from app.api.conversations import ConversationStore
from app.harness.budget import ToolCallBudget
from app.harness.checkpoint import CheckpointStore, RunStatus
from app.harness.plan import PlanLimits
from app.harness.runner import DagRunner
from langgraph.checkpoint.base import BaseCheckpointSaver
from app.tools.errors import ToolError
from app.tools.registry import ToolRegistry
from app.tools.types import ToolContext
from app.trace.recorder import TraceRecorderFactory
from app.trace.types import RunStatus as TraceRunStatus


class ConfirmationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_token: str = Field(min_length=32, max_length=512)
    decision: Literal["APPROVE", "REJECT"]


class ConfirmationService:
    def __init__(
        self,
        tools: ToolRegistry,
        conversations: ConversationStore,
        checkpoints: CheckpointStore,
        langgraph_checkpointer: BaseCheckpointSaver[Any],
        limits: PlanLimits,
        trace_factory: TraceRecorderFactory | None = None,
    ) -> None:
        self.tools = tools
        self.conversations = conversations
        self.checkpoints = checkpoints
        self.langgraph_checkpointer = langgraph_checkpointer
        self.limits = limits
        self.trace_factory = trace_factory

    async def decide(
        self,
        conversation_id: str,
        request: ConfirmationDecision,
        *,
        access_token: str,
        user: AuthUser,
    ) -> dict[str, Any]:
        if not user.email_verified:
            raise ToolError("EMAIL_NOT_VERIFIED", "verified email is required")
        await self.conversations.assert_owner(conversation_id, user.user_id)
        pending = await self.conversations.get_confirmation(request.confirmation_token, conversation_id, user.user_id)
        if pending.action_type != "CREATE_ORDER" or pending.risk != "COMMIT":
            from app.api.conversations import ConversationNotFound

            raise ConversationNotFound
        checkpoint = await self.checkpoints.load_run(pending.run_id)
        if checkpoint is None or checkpoint.status not in {
            RunStatus.WAITING_CONFIRMATION,
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
        }:
            raise ToolError("CONFIRMATION_NOT_FOUND", "confirmation not found")

        recorder = None
        recorder_token = None
        trace_status = "INCOMPLETE" if self.trace_factory is not None else None
        if self.trace_factory is not None:
            try:
                candidate = await self.trace_factory.attach(pending.run_id)
                if (
                    candidate.run.user_id == user.user_id
                    and candidate.run.conversation_id == conversation_id
                ):
                    trace_status = candidate.run.trace_status.value
                    if checkpoint.status is RunStatus.WAITING_CONFIRMATION:
                        recorder = candidate
                        recorder_token = recorder.bind()
            except Exception:
                recorder = None

        async def execute(decision: str, tool_budget: ToolCallBudget) -> dict[str, Any]:
            result = await self.tools.invoke(
                "create_order",
                {"confirmation_token": request.confirmation_token, "decision": decision},
                agent_name="order",
                agent_tools=frozenset({"create_order"}),
                permissions=frozenset({"order:commit"}),
                context=ToolContext(
                    trace_id=pending.trace_id,
                    conversation_id=conversation_id,
                    access_token=access_token,
                    authenticated_user_id=user.user_id,
                    email_verified=user.email_verified,
                    user_confirmed=True,
                    tool_budget=tool_budget,
                ),
            )
            return result.value

        runner = DagRunner(
            {},
            self.checkpoints,
            self.langgraph_checkpointer,
            self.limits,
            confirmation_handler=execute,
        )
        try:
            resumed = await runner.resume_confirmation(
                pending.validated_plan,
                run_id=pending.run_id,
                decision=request.decision,
            )
        finally:
            if recorder is not None and recorder_token is not None:
                recorder.reset(recorder_token)
        value = resumed.outputs["confirmation_result"]
        order = value.get("order")
        if request.decision == "APPROVE":
            if not isinstance(order, dict):
                raise ToolError("DEPENDENCY_UNAVAILABLE", "order confirmation returned no order", retryable=True)
            system_event = {"type": "ORDER_CONFIRMED", "order_id": order["order_id"], "status": order["status"]}
        else:
            system_event = {"type": "ORDER_REJECTED", "confirmation_id": value["confirmation_id"]}

        if not bool(value.get("replayed", False)):
            await self.conversations.append_system_event(conversation_id, system_event)
            await self.conversations.append_trace_event(
                pending.run_id,
                {
                    "type": "confirmation.decision",
                    "confirmation_id": value["confirmation_id"],
                    "decision": request.decision,
                    "status": value["status"],
                    "replayed": False,
                },
            )
            if recorder is not None:
                await recorder.record_confirmation_decision(request.decision, value["status"])
                traced = await recorder.finish_run(
                    status=(
                        TraceRunStatus.COMPLETED
                        if request.decision == "APPROVE"
                        else TraceRunStatus.CANCELLED
                    ),
                    final_answer=recorder.run.final_answer,
                    dag=recorder.run.dag,
                    replayable=True,
                )
                trace_status = traced.trace_status.value
        return {
            "confirmation_id": value["confirmation_id"],
            "decision": request.decision,
            "status": value["status"],
            "order": (
                {
                    "order_id": order["order_id"],
                    "status": order["status"],
                    "total_amount": order["total_amount"],
                    "currency": order["currency"],
                }
                if isinstance(order, dict)
                else None
            ),
            "replayed": bool(value.get("replayed", False)),
            "trace_id": pending.trace_id,
            **({"trace_status": trace_status} if trace_status is not None else {}),
        }
