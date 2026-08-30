"""Deterministic driving of the real ChatService, DAG, Registry and Verifier."""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from app.agents.registry import build_agent_registry
from app.agents.verification import DeterministicVerifier, LLMVerifier, RepairingVerifier, VerificationPipeline
from app.api.auth import AuthUser
from app.api.chat import ChatEvent, ChatMessageRequest, ChatService
from app.api.conversations import InMemoryConversationStore
from app.evaluation.script_v2 import FakeModelScriptV2, ScriptQueue
from app.evaluation.types import EvalAuth
from app.harness.checkpoint import InMemoryCheckpointStore
from app.harness.plan import PlanLimits, PlanValidator
from app.harness.supervisor import Supervisor
from app.models.invocation import model_call_round
from app.tools.business import build_business_tool_registry
from app.tools.errors import ToolError
from app.tools.policy import DeterministicPolicyEngine
from app.tools.registry import ToolRegistry
from app.tools.types import RiskLevel, ToolContext, ToolSpec
from app.trace.errors import TraceError
from app.trace.recorder import TracingModelProvider

MODEL = "deterministic-fake-v2"


@dataclass
class ToolObservations:
    proposed: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    schema_attempted: int = 0
    schema_valid: int = 0
    unauthorized_write_calls: int = 0
    results: list[tuple[str, dict[str, Any], list[str]]] = field(default_factory=list)
    context_user_ids: list[int | None] = field(default_factory=list)

    def observe_execution(self, spec: ToolSpec, context: ToolContext) -> None:
        self.executed.append(spec.name)
        self.context_user_ids.append(context.authenticated_user_id)
        if spec.risk not in {RiskLevel.PREPARE, RiskLevel.CONFIRM_REQUIRED, RiskLevel.COMMIT}:
            return
        authorized = (context.authenticated_user_id is not None and bool(context.access_token)
            and context.email_verified and not context.guest_mode)
        if spec.risk in {RiskLevel.CONFIRM_REQUIRED, RiskLevel.COMMIT}:
            authorized = authorized and context.user_confirmed
        self.unauthorized_write_calls += int(not authorized)


class ObservedToolRegistry(ToolRegistry):
    def __init__(self, production: ToolRegistry, observations: ToolObservations) -> None:
        self.observations = observations
        wrapped = []
        for manifest in production.manifests():
            spec = production.get(manifest["name"])
            async def execute(arguments, context, original=spec):
                observations.observe_execution(original, context)
                result = await original.executor(arguments, context)
                observations.results.append((original.name, result.value, result.evidence_ids))
                return result
            wrapped.append(spec.model_copy(update={"executor": execute}))
        # This is the actual production policy, not an evaluator authorization simulator.
        super().__init__(wrapped, policy=DeterministicPolicyEngine())
        if self.version != production.version:
            raise TraceError("EVAL_DATASET_INVALID", "observing tools must not change the production registry contract")

    async def invoke(self, name, arguments, **context):
        self.observations.proposed.append(name)
        self.observations.schema_attempted += 1
        try:
            # Use the exact registered production Schema, including for a valid
            # proposal subsequently denied by the agent allowlist/Policy.
            self.get(name).input_model.model_validate(arguments)
            self.observations.schema_valid += 1
        except (ValidationError, ToolError):
            pass
        return await super().invoke(name, arguments, **context)


@dataclass
class ChatExecution:
    events: list[ChatEvent]
    tools: ToolObservations
    model_calls: list[tuple[str, str, int, str]]
    tool_registry_version: str
    prior_rounds: list[ChatExecution] = field(default_factory=list)
    dependency_evidence: dict[str, Any] = field(default_factory=dict)
    terminal_status: str | None = None

    @property
    def final_status(self) -> str:
        if self.terminal_status is not None:
            return self.terminal_status
        if any(event.event == "run.failed" for event in self.events):
            return "FAILED"
        if any(event.event == "confirmation.required" and event.data.get("action_type") == "CREATE_ORDER" for event in self.events):
            return "WAITING_CONFIRMATION"
        if any(event.event == "message.completed" for event in self.events):
            return "COMPLETED"
        raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "production flow did not emit a terminal result")

    @property
    def final_message(self) -> dict[str, Any]:
        return next((event.data for event in reversed(self.events) if event.event == "message.completed"), {})


class ProductionChatDriver:
    def __init__(self, script: FakeModelScriptV2, *, catalog, orders, knowledge,
                 prompt_loader, memory_service=None, memory_extractor_factory=None,
                 conversations=None, checkpoints=None, graph_checkpointer=None, trace_factory=None) -> None:
        self.queue = ScriptQueue(script)
        provider = TracingModelProvider(self.queue.production_provider())
        self.observations = ToolObservations()
        agents, limits = build_agent_registry(), PlanLimits()
        self.tools = ObservedToolRegistry(build_business_tool_registry(catalog, orders, knowledge), self.observations)
        self.conversations = conversations or InMemoryConversationStore()
        self.service = ChatService(
            provider, MODEL, Supervisor(provider, MODEL, PlanValidator(agents, limits), prompt_prefix=prompt_loader("router")),
            agents, self.tools,
            RepairingVerifier(VerificationPipeline(DeterministicVerifier(), LLMVerifier(provider, MODEL, prompt_prefix=prompt_loader("verifier")))),
            checkpoints or InMemoryCheckpointStore(), graph_checkpointer or InMemorySaver(), limits, self.conversations,
            memory_service=memory_service,
            memory_extractor=memory_extractor_factory(provider) if memory_extractor_factory else None,
            prompt_loader=prompt_loader,
            trace_factory=trace_factory,
        )

    async def round(self, content: str, *, auth: EvalAuth, scope: str, conversation_id: str,
                    user_id: int = 1, access_token: str | None = None) -> ChatExecution:
        user = AuthUser(user_id=user_id, email="synthetic@example.test", email_verified=auth.email_verified) if auth.authenticated else None
        # Component drivers may inject an authenticated context; real transaction
        # drivers supply the token validated by the isolated RPC boundary.
        token = (access_token or "component-only-synthetic-context") if user else None
        await self.conversations.bind(conversation_id, user_id if user else None)
        request = ChatMessageRequest(content=content, developer_mode=True)
        recorder = await self.service.begin_trace(conversation_id, request, user)
        with model_call_round(scope):
            events = [event async for event in self.service.stream(conversation_id,
                request, access_token=token, user=user, recorder=recorder)]
        return ChatExecution(events, deepcopy(self.observations), list(self.queue.requests), self.tools.version)

    def finish(self) -> None:
        self.queue.assert_consumed()


class NoBusinessWrites:
    """Component-only dependency: reaching any user/order boundary is observable failure."""
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name):
        async def forbidden(*args, **kwargs):
            self.calls.append(name)
            raise ToolError("EVAL_UNEXPECTED_BUSINESS_CALL", "component scenario reached an undeclared business dependency")
        return forbidden
