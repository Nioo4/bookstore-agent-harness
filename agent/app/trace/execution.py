from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.agents.evidence import EvidenceLedger
from app.agents.execution import ProfessionalAgentExecutor, redact_for_model
from app.agents.registry import AgentRegistry
from app.agents.verification import (
    AgentDraft,
    DeterministicVerifier,
    LLMVerifier,
    RepairingVerifier,
    VerificationIssue,
    VerificationPipeline,
    VerificationRequest,
)
from app.agents.intent import INTENT_PROMPT, UserIntent
from app.harness.checkpoint import CheckpointStore, RunStatus as CheckpointRunStatus
from app.harness.plan import PlanLimits, PlanValidator
from app.harness.routing import required_agents_for_request
from app.harness.runner import DagRunner
from app.harness.supervisor import Supervisor
from app.models.provider import ModelMessage, ModelProvider, ModelRequest, ModelResponse
from app.tools.types import ReplayToolDefinition, ToolContext
from app.trace.manifests import VersionRegistry
from app.trace.pricing import PricingCalculator
from app.trace.recorder import TraceRecorder
from app.trace.replay import ReplayToolRegistry
from app.trace.repository import TraceRepository
from app.trace.safety import TraceSafetyProjector
from app.trace.types import StepStatus, StepType, TraceRun
from app.trace.worker import ReplayExecutionResult


class GuardedModelProvider(ModelProvider):
    def __init__(
        self, inner: ModelProvider, ensure_active: Callable[[], Awaitable[None]]
    ) -> None:
        self.inner = inner
        self.ensure_active = ensure_active

    async def complete(self, request: ModelRequest) -> ModelResponse:
        await self.ensure_active()
        return await self.inner.complete(request)


class ReplayChatExecutor:
    """Runs a new model workflow over source fixtures without holding any real tool client."""

    def __init__(
        self,
        repository: TraceRepository,
        provider: ModelProvider,
        agents: AgentRegistry,
        tool_definitions: tuple[ReplayToolDefinition, ...] | list[ReplayToolDefinition],
        checkpoint_store: CheckpointStore,
        langgraph_checkpointer: BaseCheckpointSaver[Any],
        limits: PlanLimits,
        versions: VersionRegistry,
        safety: TraceSafetyProjector,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.agents = agents
        # These definitions cannot retain executors, clients, policies, or network handles.
        self.tool_definitions = tuple(tool_definitions)
        self.checkpoint_store = checkpoint_store
        self.langgraph_checkpointer = langgraph_checkpointer
        self.limits = limits
        self.versions = versions
        self.safety = safety

    async def execute(
        self, run: TraceRun, ensure_active: Callable[[], Awaitable[None]]
    ) -> ReplayExecutionResult:
        if run.source_run_id is None or run.conversation_id is None or run.user_id is None:
            raise ValueError("replay run is missing its source identity")
        source = await self.repository.get_worker_run(run.source_run_id)
        content = source.input.get("content")
        if not isinstance(content, str) or not content:
            raise ValueError("replay source input is unavailable")
        profile = self.versions.model_profile(run.model_profile)
        self.versions.prompt_set(run.prompt_set_version)
        fixtures = await self.repository.list_fixtures(source.run_id)
        evidence = await self.repository.list_evidence(source.run_id)
        evidence_by_step: dict[str, list[str]] = {}
        for item in evidence:
            evidence_by_step.setdefault(item.source_step_id, []).append(item.evidence_id)
        replay_tools = ReplayToolRegistry(
            self.tool_definitions,
            fixtures,
            evidence_ids_by_step=evidence_by_step,
            ensure_active=ensure_active,
        )
        guarded_provider = GuardedModelProvider(self.provider, ensure_active)
        recorder = TraceRecorder(
            self.repository,
            self.safety,
            self.versions,
            PricingCalculator(self.versions.pricing_manifest),
            run=run,
        )
        token = recorder.bind()
        try:
            intent = await self._extract_intent(guarded_provider, profile.agent, content)
            plan_step = await recorder.start_step(
                node_id="supervisor.plan",
                step_type=StepType.PLAN,
                input_value={"request": content},
                agent_name="supervisor",
                model_profile=run.model_profile,
                prompt_version=run.prompt_set_version,
            )
            supervisor = Supervisor(
                guarded_provider,
                profile.router,
                PlanValidator(self.agents, self.limits),
                prompt_prefix=self.versions.prompt_text(run.prompt_set_version, "router"),
            )
            validated = await supervisor.plan(content, memory_context=[])
            await recorder.finish_step(
                plan_step,
                status=StepStatus.COMPLETED,
                output_value=validated.plan.model_dump(mode="json"),
            )
            ledger = EvidenceLedger()
            base_context = ToolContext(
                trace_id=run.run_id,
                conversation_id=run.conversation_id,
                authenticated_user_id=run.user_id,
                email_verified=True,
                user_confirmed=False,
            )
            executor = ProfessionalAgentExecutor(
                guarded_provider,
                profile.agent,
                self.agents,
                replay_tools,  # type: ignore[arg-type]
                base_context,
                ledger,
                prompt_loader=lambda role: self.versions.prompt_text(run.prompt_set_version, role),
                request_context={"user_request": content, **intent.model_dump(mode="json")},
            )

            async def confirmation_handler(decision: str, budget) -> dict[str, Any]:
                await ensure_active()
                result = await replay_tools.invoke(
                    "create_order",
                    {"confirmation_token": "replay-fixture-placeholder-token-000000", "decision": decision},
                    agent_name="order",
                    agent_tools=frozenset({"create_order"}),
                    permissions=frozenset({"order:commit"}),
                    context=base_context.model_copy(
                        update={"user_confirmed": True, "tool_budget": budget}
                    ),
                )
                ledger.record_tool_result("create_order", result)
                return result.value

            runner = DagRunner(
                executor.handlers(),
                self.checkpoint_store,
                self.langgraph_checkpointer,
                self.limits,
                confirmation_handler=confirmation_handler,
            )
            run_result = await runner.run(
                validated,
                run_id=run.run_id,
                user_request=json.dumps(
                    {"content": content, "memory_context": []},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            if run_result.status is CheckpointRunStatus.WAITING_CONFIRMATION:
                decision_fixture = replay_tools.consume_system_fixture("confirmation_decision")
                decision = (
                    decision_fixture.request.get("decision")
                    if decision_fixture is not None
                    else None
                )
                if decision not in {"APPROVE", "REJECT"}:
                    from app.tools.errors import ToolError

                    raise ToolError(
                        "REPLAY_FIXTURE_MISMATCH",
                        "confirmation decision fixture is missing",
                    )
                run_result = await runner.resume_confirmation(
                    validated, run_id=run.run_id, decision=str(decision)
                )

            draft = await self._draft(guarded_provider, profile.agent, content, run_result.outputs, ledger)
            verification_step = await recorder.start_step(
                node_id="verifier.final",
                step_type=StepType.VERIFICATION,
                input_value={"draft": draft.model_dump(mode="json")},
                agent_name="verifier",
                model_profile=run.model_profile,
                prompt_version=run.prompt_set_version,
            )
            request = VerificationRequest(
                draft=draft,
                user_request=content,
                constraints=intent.constraints,
                required_question_ids=intent.required_question_ids,
                requires_catalog_evidence="catalog" in required_agents_for_request(content),
                requires_policy_evidence="knowledge" in required_agents_for_request(content),
                evidence=ledger,
            )
            verifier = RepairingVerifier(
                VerificationPipeline(
                    DeterministicVerifier(),
                    LLMVerifier(guarded_provider, profile.verifier, prompt_prefix=self.versions.prompt_text(run.prompt_set_version, "verifier")),
                )
            )

            async def repair(previous: AgentDraft, issues: list[VerificationIssue]) -> AgentDraft:
                repair_step = await recorder.start_step(
                    node_id="verifier.repair",
                    step_type=StepType.REPAIR,
                    input_value={
                        "draft": previous.model_dump(mode="json"),
                        "issue_codes": [issue.code for issue in issues],
                    },
                    agent_name="verifier",
                    model_profile=run.model_profile,
                    prompt_version=run.prompt_set_version,
                )
                response = await guarded_provider.complete(
                    ModelRequest(
                        model=profile.agent,
                        temperature=0,
                        response_schema=AgentDraft.model_json_schema(),
                        messages=[
                            ModelMessage(
                                role="system",
                                content=self.versions.prompt_text(run.prompt_set_version, "verifier")
                                + "\nRepair once using only supplied evidence. Return JSON only.",
                            ),
                            ModelMessage(
                                role="user",
                                content=json.dumps(
                                    {
                                        "draft": previous.model_dump(mode="json"),
                                        "user_request": content,
                                        "constraints": intent.constraints.model_dump(mode="json"),
                                        "required_question_ids": intent.required_question_ids,
                                        "issues": [item.model_dump(mode="json") for item in issues],
                                        "evidence": _safe_evidence(ledger),
                                    },
                                    ensure_ascii=False,
                                ),
                            ),
                        ],
                    )
                )
                repaired = AgentDraft.model_validate_json(response.content)
                await recorder.finish_step(
                    repair_step,
                    status=StepStatus.COMPLETED,
                    output_value=repaired.model_dump(mode="json"),
                )
                return repaired

            verified = await verifier.verify_once_with_repair(request, repair)
            await recorder.finish_step(
                verification_step,
                status=StepStatus.COMPLETED,
                output_value={
                    "approved": verified.verification.approved,
                    "repaired": verified.repaired,
                    "issue_codes": [item.code for item in verified.verification.issues],
                },
            )
            answer = (
                verified.draft.answer
                if verified.draft is not None
                else verified.conservative_answer or "Unable to verify an answer."
            )
            usage, usage_status, cost = await recorder.usage_and_cost()
            return ReplayExecutionResult(
                final_answer=answer,
                dag=validated.plan.model_dump(mode="json"),
                token_usage=usage,
                usage_status=usage_status,
                trace_incomplete=recorder.degraded,
                cost=cost,
                unused_fixture_count=replay_tools.unused_fixture_count,
            )
        finally:
            recorder.reset(token)

    async def _extract_intent(
        self, provider: ModelProvider, model: str, content: str
    ) -> UserIntent:
        response = await provider.complete(
            ModelRequest(
                model=model,
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

    async def _draft(
        self,
        provider: ModelProvider,
        model: str,
        content: str,
        outputs: dict[str, Any],
        evidence: EvidenceLedger,
    ) -> AgentDraft:
        for output in reversed(list(outputs.values())):
            try:
                return AgentDraft.model_validate(output)
            except Exception:
                continue
        response = await provider.complete(
            ModelRequest(
                model=model,
                temperature=0,
                response_schema=AgentDraft.model_json_schema(),
                messages=[
                    ModelMessage(
                        role="system",
                        content="Synthesize only from supplied outputs and evidence. Return JSON only.",
                    ),
                    ModelMessage(
                        role="user",
                        content=json.dumps(
                            {
                                "request": content,
                                "outputs": redact_for_model(outputs),
                                "evidence": _safe_evidence(evidence),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ],
            )
        )
        return AgentDraft.model_validate_json(response.content)


def _safe_evidence(evidence: EvidenceLedger) -> dict[str, Any]:
    return {
        evidence_id: redact_for_model(record.model_dump(mode="json"))
        for evidence_id, record in evidence.snapshot().items()
    }
