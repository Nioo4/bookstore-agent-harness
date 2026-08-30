"""Version-specific production dispatch. Missing real dependencies are ERROR, not PASS."""
from __future__ import annotations

import time
import re
from uuid import UUID

from app.evaluation.component_cases import COMPONENT_SCENARIOS, run_component_case
from app.evaluation.harness import aggregate_metrics
from app.evaluation.memory_cases import MEMORY_IDS, run_memory_case
from app.evaluation.read_cases import READ_SCENARIOS, run_read_case
from app.evaluation.trace_v2 import EvaluationTraceFactory
from app.evaluation.types import CaseExecution
from app.models.configuration import CURRENT_PROMPT_SET
from app.trace.errors import TraceError
from app.trace.safety import TraceSafetyProjector
from app.trace.types import TraceStatus


class ProductionEvalHarness:
    def __init__(self, versions, *, runner_version, trace_repository=None, real_dependencies=None, retention_days=180):
        self.versions, self.runner_version = versions, runner_version
        self.trace_repository, self.real_dependencies = trace_repository, real_dependencies
        self.retention_days = retention_days
        self.evidence = {}
        self.executions = {}

    async def run_suite(self, suite, *, eval_run_id, previous_cases=None):
        if suite.manifest.suite_version != "v2":
            raise TraceError("EVAL_DATASET_INVALID", "production v2 executor requires a v2 suite")
        results = []
        for case in suite.cases:
            previous = (previous_cases or {}).get(case.case_id)
            if previous is None:
                results.append(await self.run_case(suite, case, eval_run_id=eval_run_id))
            else:
                evidence = previous.deterministic_metrics.get("production_evidence", {})
                self.evidence[case.case_id] = evidence
                results.append(CaseExecution(case_id=case.case_id, category=case.category,
                    status=previous.status, final_status=evidence.get("final_status", "NOT_COMPLETED"),
                    failure_codes=previous.failure_codes, duration_ms=previous.duration_ms,
                    deterministic_metrics={key:value for key,value in previous.deterministic_metrics.items() if key != "production_evidence"}))
        return results, aggregate_metrics(suite.cases, results)

    async def run_case(self, suite, case, *, eval_run_id):
        started = time.perf_counter()
        factory = EvaluationTraceFactory(self.trace_repository, self.versions, eval_run_id=eval_run_id,
            case_id=case.case_id, runner_version=self.runner_version, retention_days=self.retention_days) if self.trace_repository else None
        counters = dict(schema_valid=0, schema_attempted=0, severe_hallucinations=0,
            unauthorized_write_calls=0, transaction_assertions_passed=False, recovery_succeeded=False)
        evidence = {"production_entry":"ChatService.stream", "trace_run_ids":[],
            "dependency_scope":"CONTROLLED_CATALOG_KNOWLEDGE_MEMORY_COMPONENT_STORAGE"}
        try:
            if case.case_id in READ_SCENARIOS:
                execute = run_read_case
            elif case.case_id in COMPONENT_SCENARIOS:
                execute = run_component_case
            elif case.case_id in MEMORY_IDS:
                execute = run_memory_case
            elif case.category in {"ORDER_CONFIRMATION_CONCURRENCY", "PERSISTENT_TASK_RECOVERY"}:
                evidence["dependency_scope"] = "ISOLATED_REAL_DEPENDENCIES_REQUIRED"
                evidence["production_entry"] = "ConfirmationService.decide" if case.category == "ORDER_CONFIRMATION_CONCURRENCY" else "InventoryEventWorker"
                if self.real_dependencies is None:
                    raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "real transaction/recovery dependencies were not configured")
                execute = self.real_dependencies.run_case
            else:
                raise TraceError("EVAL_DATASET_INVALID", "case has no approved v2 driver")
            execution, failures = await execute(case, suite.fake_model(case), suite.fixture(case),
                lambda role:self.versions.prompt_text(CURRENT_PROMPT_SET, role), trace_factory=factory)
            self.executions[case.case_id] = execution
            counters.update(schema_valid=execution.tools.schema_valid, schema_attempted=execution.tools.schema_attempted,
                severe_hallucinations=failures.count("SEVERE_FACT_HALLUCINATION"),
                unauthorized_write_calls=execution.tools.unauthorized_write_calls)
            if execution.tool_registry_version != self.versions.tool_registry_versions["evaluation-v2"]:
                failures.append("TOOL_REGISTRY_VERSION_MISMATCH")
            if factory:
                for run_id in factory.run_ids:
                    recorded = await factory.repository.get_worker_run(run_id)
                    if recorded.trace_status is not TraceStatus.COMPLETE:
                        failures.append("EVAL_TRACE_INCOMPLETE")
            counters.update(transaction_assertions_passed=not failures and case.category == "ORDER_CONFIRMATION_CONCURRENCY",
                recovery_succeeded=not failures and case.category == "PERSISTENT_TASK_RECOVERY")
            evidence.update(observed_tools=execution.tools.executed, proposed_tools=execution.tools.proposed,
                dependency_evidence=execution.dependency_evidence,
                model_calls=[dict(caller=caller, scope=scope, ordinal=ordinal, schema_sha256=digest)
                    for caller,scope,ordinal,digest in execution.model_calls],
                evidence_ids=sorted({item for _,_,ids in execution.tools.results for item in ids}),
                rounds=[dict(final_status=run.final_status, final_answer=run.final_message.get("content", ""),
                    recommendations=run.final_message.get("recommendations", []),
                    event_types=[event.event for event in run.events]) for run in [*execution.prior_rounds, execution]])
            status, final_status = ("FAILED" if failures else "PASSED"), execution.final_status
        except TraceError as exc:
            status, final_status, failures = "ERROR", "NOT_COMPLETED", [exc.code]
        except Exception:
            # Do not serialize exception text: real adapters may include DSNs/tokens.
            status, final_status, failures = "ERROR", "NOT_COMPLETED", ["EVAL_DEPENDENCY_UNAVAILABLE"]
        evidence["trace_run_ids"] = list(factory.run_ids) if factory else []
        evidence["final_status"] = final_status
        projected = TraceSafetyProjector().json(evidence)
        if projected.truncated:
            status, failures = "ERROR", ["EVAL_EVIDENCE_TRUNCATED"]
        safe = projected.value
        # These are generated/Schema-validated identifiers, not free-form model
        # text. Telephone-pattern redaction must not corrupt Trace links or hashes.
        safe["trace_run_ids"] = [str(UUID(value)) for value in evidence["trace_run_ids"]]
        for index, call in enumerate(evidence.get("model_calls", []) if not projected.truncated else []):
            if re.fullmatch(r"[0-9a-f]{64}", call["schema_sha256"]):
                safe["model_calls"][index]["schema_sha256"] = call["schema_sha256"]
        if not projected.truncated:
            _restore_runtime_identifiers(evidence.get("dependency_evidence", {}), safe.get("dependency_evidence", {}))
            for index, identifier in enumerate(evidence.get("evidence_ids", [])):
                if _structured_evidence_id(identifier):
                    safe["evidence_ids"][index] = identifier
            for round_index, round_value in enumerate(evidence.get("rounds", [])):
                for rec_index, rec in enumerate(round_value["recommendations"]):
                    for index, identifier in enumerate(rec.get("evidence_ids", [])):
                        if _structured_evidence_id(identifier):
                            safe["rounds"][round_index]["recommendations"][rec_index]["evidence_ids"][index] = identifier
        self.evidence[case.case_id] = safe
        return CaseExecution(case_id=case.case_id, category=case.category, status=status, final_status=final_status,
            failure_codes=sorted(set(failures)), deterministic_metrics=counters,
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)))


def _structured_evidence_id(value):
    if re.fullmatch(r"(?:book:[1-9][0-9]*:v[0-9]+|order:[1-9][0-9]*|(?:policy|catalog-query):[0-9a-f]{64}|order-preview:[0-9a-f]{16})", value):
        return True
    if value.startswith("confirmation:"):
        try:
            return str(UUID(value.partition(":")[2])) == value.partition(":")[2]
        except ValueError:
            pass
    return False


def _restore_runtime_identifiers(source, projected):
    """Preserve only typed IDs from SQL/worker metadata, never arbitrary strings."""
    if isinstance(source, dict) and isinstance(projected, dict):
        for key, value in source.items():
            if key in {"task_id", "run_id", "thread_id", "event_id", "trigger_event_id", "notification_id", "mysql_server_uuid"} and isinstance(value, str):
                try:
                    if str(UUID(value)) == value:
                        projected[key] = value
                except ValueError:
                    pass
            elif key == "redis_run_id" and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
                projected[key] = value
            elif key in {"helper_sha256", "token_hash", "snapshot_sha256"} and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                projected[key] = value
            elif key == "build" and isinstance(value, str) and re.fullmatch(r"evaluation-v2\+sha256:[A-Za-z0-9_-]{43}", value):
                projected[key] = value
            elif key in projected:
                _restore_runtime_identifiers(value, projected[key])
    elif isinstance(source, list) and isinstance(projected, list):
        for original, safe in zip(source, projected):
            _restore_runtime_identifiers(original, safe)
