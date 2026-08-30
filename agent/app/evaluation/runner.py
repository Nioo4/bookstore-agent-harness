from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from app.evaluation.harness import DeterministicEvalHarness
from app.evaluation.harness_v2 import ProductionEvalHarness
from app.evaluation.build import runner_build_version
from app.models.configuration import CURRENT_PROMPT_SET
from app.evaluation.judge import EvaluationJudge
from app.evaluation.loader import LoadedSuite
from app.evaluation.repository import (
    EvaluationCaseRecord,
    EvaluationRepository,
    EvaluationRunRecord,
)
from app.trace.errors import TraceError
from app.trace.manifests import VersionRegistry
from app.trace.repository import TraceRepository
from app.trace.types import (
    CostStatus,
    RunKind,
    RunStatus,
    StepDraft,
    StepStatus,
    StepType,
    TokenUsage,
    TraceCost,
    TraceRun,
    TraceStatus,
    WorkflowType,
    canonical_json,
    sha256_json,
)


class EvaluationRunner:
    def __init__(
        self,
        repository: EvaluationRepository,
        *,
        runner_version: str = "evaluation-v1",
        retention_days: int = 180,
        trace_repository: TraceRepository | None = None,
        versions: VersionRegistry | None = None,
        tool_registry_version: str = "evaluation-tools-v1",
        judge: EvaluationJudge | None = None,
        real_dependencies=None,
    ) -> None:
        self.repository = repository
        self.runner_version = runner_version
        self.retention_days = retention_days
        self.trace_repository = trace_repository
        self.versions = versions
        self.tool_registry_version = tool_registry_version
        self.judge = judge
        self.real_dependencies = real_dependencies
        if (trace_repository is None) != (versions is None):
            raise ValueError("evaluation Trace repository and version registry must be configured together")

    async def run(
        self,
        suite: LoadedSuite,
        *,
        idempotency_key: str,
        model_profile: str | None = None,
        prompt_set_version: str | None = None,
        with_judge: bool = False,
        now: datetime | None = None,
    ) -> EvaluationRunRecord:
        if not 16 <= len(idempotency_key) <= 128:
            raise TraceError("EVAL_DATASET_INVALID", "idempotency key must be 16 to 128 characters")
        v2 = suite.manifest.suite_version == "v2"
        model_profile = model_profile or ("deterministic-fake-v2" if v2 else "deterministic-fake-v1")
        prompt_set_version = prompt_set_version or (CURRENT_PROMPT_SET if v2 else "v1")
        runner_version = runner_build_version() if v2 else self.runner_version
        if v2 and (self.versions is None or model_profile != "deterministic-fake-v2" or prompt_set_version != CURRENT_PROMPT_SET):
            raise TraceError("EVAL_DATASET_INVALID", "v2 requires the retained production profile and prompts")
        timestamp = now or datetime.now(UTC)
        mode = "DETERMINISTIC_WITH_JUDGE" if with_judge else "DETERMINISTIC"
        normalized = {
            "suite_version": suite.manifest.suite_version,
            "dataset_hash": suite.dataset_hash.hex(),
            "runner_version": runner_version,
            "mode": mode,
            "model_profile": model_profile,
            "prompt_set_version": prompt_set_version,
        }
        if with_judge and self.judge:
            normalized.update(judge_model_profile=self.judge.model_profile, judge_prompt_version=self.judge.prompt_version)
        run = EvaluationRunRecord(
            eval_run_id=str(uuid.uuid4()),
            idempotency_key=hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            request_hash=sha256_json(normalized),
            suite_version=suite.manifest.suite_version,
            dataset_hash=suite.dataset_hash,
            runner_version=runner_version,
            mode=mode,
            status="RUNNING",
            model_profile=model_profile,
            prompt_set_version=prompt_set_version,
            judge_model_profile=self.judge.model_profile if with_judge and self.judge else None,
            judge_prompt_version=self.judge.prompt_version if with_judge and self.judge else None,
            judge_status="PENDING" if with_judge else "DISABLED",
            case_count=len(suite.cases),
            cost_status=CostStatus.UNAVAILABLE,
            retention_expires_at=timestamp + timedelta(days=self.retention_days),
            started_at=timestamp,
            created_at=timestamp,
            updated_at=timestamp,
        )
        current, replayed = await self.repository.create(run)
        if replayed and current.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return current
        existing_cases = {
            item.case_id: item for item in await self.repository.list_cases(current.eval_run_id)
        }
        production = None
        if v2:
            production = ProductionEvalHarness(self.versions, runner_version=runner_version,
                trace_repository=self.trace_repository, real_dependencies=self.real_dependencies,
                retention_days=self.retention_days)
            results, metrics = await production.run_suite(suite, eval_run_id=current.eval_run_id, previous_cases=existing_cases)
        else:
            results, metrics = DeterministicEvalHarness().run_suite(suite)
        judge_incomplete = with_judge and self.judge is None
        for case, result in zip(suite.cases, results, strict=True):
            existing = existing_cases.get(case.case_id)
            agent_run_id = existing.agent_run_id if existing is not None else None
            if existing is not None:
                continue
            judge_scores = None
            judge_reason = None
            if with_judge and case.category == "MULTI_CONSTRAINT_RECOMMENDATION":
                score = None
                if self.judge is not None:
                    if production:
                        observed = production.executions.get(case.case_id)
                        if observed is not None and result.status == "PASSED":
                            score = await self.judge.score_observed(case, observed)
                    else:
                        score = await self.judge.score(case, suite.fake_model(case), suite.fixture(case))
                if score is None:
                    judge_incomplete = True
                else:
                    judge_scores = score.model_dump(mode="json", exclude={"reason"})
                    judge_scores["informational_passed"] = score.informational_passed
                    judge_reason = score.reason
            evidence = production.evidence.get(case.case_id, {}) if production else None
            if production:
                ids = evidence.get("trace_run_ids", [])
                agent_run_id = ids[0] if ids else None
            elif self.trace_repository is not None and self.versions is not None:
                agent_run_id = await self._write_case_trace(
                    current.eval_run_id, suite, case, result, timestamp
                )
            fixture_bytes = suite.file_bytes(case.fixture_ref)
            await self.repository.put_case(
                EvaluationCaseRecord(
                    eval_run_id=current.eval_run_id,
                    case_id=case.case_id,
                    agent_run_id=agent_run_id,
                    category=case.category,
                    status=result.status,
                    case_snapshot=case.model_dump(mode="json"),
                    fixture_manifest_hash=hashlib.sha256(fixture_bytes).digest(),
                    deterministic_metrics={**result.deterministic_metrics, **({"production_evidence":evidence} if evidence is not None else {})},
                    failure_codes=result.failure_codes,
                    judge_scores=judge_scores,
                    judge_reason=judge_reason,
                    duration_ms=result.duration_ms,
                    created_at=timestamp,
                )
            )
        passed = sum(result.status == "PASSED" for result in results)
        failed = sum(result.status == "FAILED" for result in results)
        errors = sum(result.status in {"ERROR", "SKIPPED"} for result in results)
        completed_at = now or datetime.now(UTC)
        return await self.repository.finish(
            current.eval_run_id,
            status="COMPLETED" if errors == 0 else "FAILED",
            passed_count=passed,
            failed_count=failed,
            error_count=errors,
            metrics=metrics.model_dump(mode="json"),
            deterministic_gate_passed=metrics.deterministic_gate_passed,
            judge_status=(
                "INCOMPLETE" if judge_incomplete else ("COMPLETED" if with_judge else "DISABLED")
            ),
            known_cost_microusd=None,
            cost_status=CostStatus.UNAVAILABLE,
            completed_at=completed_at,
            updated_at=completed_at,
        )

    async def _write_case_trace(self, eval_run_id, suite, case, result, timestamp: datetime) -> str:
        assert self.trace_repository is not None and self.versions is not None
        run_id = str(uuid.uuid4())
        script = suite.fake_model(case)
        run = TraceRun(
            run_id=run_id,
            user_id=None,
            conversation_id=None,
            run_kind=RunKind.EVALUATION,
            workflow_type=WorkflowType.EVALUATION_CASE,
            status=RunStatus.RUNNING,
            trace_status=TraceStatus.RECORDING,
            replayable=False,
            replay_block_reason="EVALUATION_RUN",
            input={"eval_run_id": eval_run_id, "case_id": case.case_id, "user_request": case.user_request},
            model_profile="deterministic-fake-v1",
            model_manifest_hash=self.versions.model_profile_hash("deterministic-fake-v1"),
            prompt_set_version="v1",
            prompt_manifest_hash=self.versions.prompt_set_hash("v1"),
            tool_registry_version=self.tool_registry_version,
            harness_version=self.runner_version,
            cost=TraceCost(status=CostStatus.UNAVAILABLE),
            retention_expires_at=timestamp + timedelta(days=self.retention_days),
            started_at=timestamp,
            created_at=timestamp,
            updated_at=timestamp,
        )
        await self.trace_repository.create_run(run)
        step = await self.trace_repository.append_step(
            StepDraft(
                step_id=str(uuid.uuid4()),
                run_id=run_id,
                node_id="evaluation.case",
                step_type=StepType.SYSTEM,
                input={
                    "case_id": case.case_id,
                    "category": case.category,
                    "tool_calls": [call.name for call in script.tool_calls],
                },
                model_profile="deterministic-fake-v1",
                prompt_version="v1",
                tool_version=self.tool_registry_version,
                started_at=timestamp,
            )
        )
        await self.trace_repository.finish_step(
            run_id,
            step.step_id,
            status=StepStatus.COMPLETED if result.status == "PASSED" else StepStatus.FAILED,
            output={
                "case_status": result.status,
                "failure_codes": result.failure_codes,
                "deterministic_metrics": result.deterministic_metrics,
            },
            evidence_ids=[],
            token_usage=TokenUsage.no_calls(),
            cost=TraceCost(status=CostStatus.UNAVAILABLE),
            error_code=result.failure_codes[0] if result.failure_codes else None,
            error_message="deterministic evaluation case failed" if result.failure_codes else None,
            completed_at=timestamp,
        )
        final_status = RunStatus.COMPLETED if result.status == "PASSED" else RunStatus.FAILED
        await self.trace_repository.finish_run(
            run_id,
            status=final_status,
            trace_status=TraceStatus.COMPLETE,
            replayable=False,
            replay_block_reason="EVALUATION_RUN",
            final_answer=script.final_answer,
            dag={
                "case_id": case.case_id,
                "tool_calls": [call.model_dump(mode="json") for call in script.tool_calls],
            },
            token_usage=TokenUsage.no_calls(),
            cost=TraceCost(status=CostStatus.UNAVAILABLE),
            unused_fixture_count=None,
            error_code=result.failure_codes[0] if result.failure_codes else None,
            error_message="deterministic evaluation case failed" if result.failure_codes else None,
            completed_at=timestamp,
        )
        return run_id
