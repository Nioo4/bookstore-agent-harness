"""Actual production Recorder calls tagged as non-replayable evaluation runs."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.trace.pricing import PricingCalculator
from app.trace.recorder import TraceRecorder
from app.trace.safety import TraceSafetyProjector
from app.models.configuration import CURRENT_PROMPT_SET
from app.trace.types import CostStatus, RunKind, RunStatus, TraceCost, TraceRun, TraceStatus, WorkflowType


class EvaluationTraceFactory:
    def __init__(self, repository, versions, *, eval_run_id, case_id, runner_version, retention_days=180):
        self.repository, self.versions = repository, versions
        self.eval_run_id, self.case_id = eval_run_id, case_id
        self.runner_version, self.retention_days = runner_version, retention_days
        self.safety = TraceSafetyProjector()
        self.pricing = PricingCalculator(versions.pricing_manifest)
        self.run_ids = []

    async def start(self, *, user_id, conversation_id, user_request):
        now = datetime.now(UTC)
        run = TraceRun(run_id=str(uuid.uuid4()), user_id=None, conversation_id=None,
            run_kind=RunKind.EVALUATION, workflow_type=WorkflowType.EVALUATION_CASE,
            status=RunStatus.RUNNING, trace_status=TraceStatus.RECORDING,
            replayable=False, replay_block_reason="EVALUATION_RUN",
            input=self.safety.json({"eval_run_id":self.eval_run_id, "case_id":self.case_id,
                "conversation_id":conversation_id, "content":user_request}).value,
            model_profile="deterministic-fake-v2",
            model_manifest_hash=self.versions.model_profile_hash("deterministic-fake-v2"),
            prompt_set_version=CURRENT_PROMPT_SET,
            prompt_manifest_hash=self.versions.prompt_set_hash(CURRENT_PROMPT_SET),
            tool_registry_version=self.versions.tool_registry_versions["evaluation-v2"],
            harness_version=self.runner_version, cost=TraceCost(status=CostStatus.UNAVAILABLE),
            retention_expires_at=now + timedelta(days=self.retention_days), started_at=now, created_at=now, updated_at=now)
        await self.repository.create_run(run)
        self.run_ids.append(run.run_id)
        return TraceRecorder(self.repository, self.safety, self.versions, self.pricing, run=run)

    async def attach(self, run_id):
        if run_id not in self.run_ids:
            raise ValueError("evaluation Trace belongs to a different case")
        run = await self.repository.get_worker_run(run_id)
        return TraceRecorder(self.repository, self.safety, self.versions, self.pricing, run=run)
