from __future__ import annotations

import time
from fractions import Fraction

from pydantic import ValidationError

from app.evaluation.loader import LoadedSuite
from app.evaluation.types import CaseExecution, EvalCaseV1, EvalMetrics
from app.tools.business import build_business_replay_definitions
from app.tools.types import RiskLevel
from app.trace.types import canonical_json
from app.trace.errors import TraceError


class DeterministicEvalHarness:
    def __init__(self) -> None:
        definitions = build_business_replay_definitions()
        self.definitions = {item.name: item for item in definitions}

    def run_suite(self, suite: LoadedSuite) -> tuple[list[CaseExecution], EvalMetrics]:
        if suite.manifest.suite_version != "v1":
            raise TraceError("EVAL_DATASET_INVALID", "the historical v1 executor cannot execute a v2 suite")
        results = [self.run_case(suite, case) for case in suite.cases]
        return results, aggregate_metrics(suite.cases, results)

    def run_case(self, suite: LoadedSuite, case: EvalCaseV1) -> CaseExecution:
        if suite.manifest.suite_version != "v1":
            raise TraceError("EVAL_DATASET_INVALID", "the historical v1 executor cannot execute a v2 case")
        return _run_legacy_case(self, suite, case)


def aggregate_metrics(cases: list[EvalCaseV1], results: list[CaseExecution]) -> EvalMetrics:
        if len(cases) != len(results) or any(case.case_id != result.case_id or case.category != result.category for case, result in zip(cases, results, strict=True)):
            raise TraceError("EVAL_DATASET_INVALID", "execution records do not match the suite")
        critical = [
            result
            for result, case in zip(results, cases, strict=True)
            if "critical_transaction" in case.tags
        ]
        task_cases = [
            result
            for result, case in zip(results, cases, strict=True)
            if "task_expected" in case.tags
        ]
        recovery = [
            result
            for result in results
            if result.category == "PERSISTENT_TASK_RECOVERY"
        ]
        schema_valid = sum(int(result.deterministic_metrics["schema_valid"]) for result in results)
        schema_attempted = sum(
            int(result.deterministic_metrics["schema_attempted"]) for result in results
        )
        hallucinations = sum(
            int(result.deterministic_metrics["severe_hallucinations"]) for result in results
        )
        unauthorized = sum(
            int(result.deterministic_metrics["unauthorized_write_calls"]) for result in results
        )
        critical_passed = sum(
            result.status == "PASSED"
            and bool(result.deterministic_metrics["transaction_assertions_passed"])
            for result in critical
        )
        task_passed = sum(result.status == "PASSED" for result in task_cases)
        recovery_passed = sum(
            result.status == "PASSED" and bool(result.deterministic_metrics["recovery_succeeded"])
            for result in recovery
        )
        valid_dataset = schema_attempted > 0
        gate = (
            len(results) == 40
            and not any(result.status in {"ERROR", "SKIPPED"} for result in results)
            and len(critical) == 5
            and critical_passed == 5
            and hallucinations == 0
            and valid_dataset
            and Fraction(schema_valid, schema_attempted) >= Fraction(99, 100)
            and bool(task_cases)
            and Fraction(task_passed, len(task_cases)) >= Fraction(85, 100)
            and len(recovery) == 4
            and recovery_passed == 4
            and unauthorized == 0
        )
        return EvalMetrics(
            critical_transaction_passed=critical_passed,
            critical_transaction_total=len(critical),
            severe_hallucinations=hallucinations,
            tool_schema_valid=schema_valid,
            tool_schema_attempted=schema_attempted,
            task_completed=task_passed,
            task_total=len(task_cases),
            recovery_passed=recovery_passed,
            recovery_total=len(recovery),
            unauthorized_write_calls=unauthorized,
            deterministic_gate_passed=gate,
        )

def _run_legacy_case(self: DeterministicEvalHarness, suite: LoadedSuite, case: EvalCaseV1) -> CaseExecution:
        started = time.perf_counter()
        failures: list[str] = []
        fixture = suite.fixture(case)
        script = suite.fake_model(case)
        attempted_names: list[str] = []
        schema_attempted = 0
        schema_valid = 0
        unauthorized_write_calls = 0
        evidence_facts: list[dict] = []
        fixture_index = 0

        for call in script.tool_calls:
            schema_attempted += 1
            attempted_names.append(call.name)
            definition = self.definitions.get(call.name)
            if definition is None:
                failures.append("UNKNOWN_TOOL")
                continue
            try:
                parsed = definition.input_model.model_validate(call.arguments)
                projected = definition.trace_projection.project_request(
                    parsed.model_dump(mode="json")
                )
                schema_valid += 1
            except ValidationError:
                failures.append("INVALID_TOOL_ARGUMENTS")
                continue
            if fixture_index >= len(fixture.calls):
                failures.append("FIXTURE_MISSING")
                continue
            expected_call = fixture.calls[fixture_index]
            fixture_index += 1
            if (
                expected_call.tool_name != call.name
                or canonical_json(expected_call.arguments) != canonical_json(projected)
            ):
                failures.append("FIXTURE_MISMATCH")
                continue
            authorized = _authorized(case, definition.risk, script.confirmation_decision)
            if not authorized:
                # Policy denies before execution, so this never increments the write-execution metric.
                failures.append(
                    "EMAIL_NOT_VERIFIED"
                    if case.auth.authenticated and not case.auth.email_verified
                    else "AUTH_REQUIRED"
                )
                continue
            if expected_call.outcome != "SUCCESS":
                if expected_call.error_code not in case.expected.allowed_error_codes:
                    failures.append(expected_call.error_code or "UNEXPECTED_TOOL_ERROR")
                continue
            evidence_facts.extend(expected_call.evidence_facts)

        if fixture_index != len(fixture.calls):
            failures.append("UNUSED_REQUIRED_FIXTURE")
        for required in case.expected.required_tools:
            if required not in attempted_names:
                failures.append("REQUIRED_TOOL_MISSING")
        for forbidden in case.expected.forbidden_tools:
            if forbidden in attempted_names:
                failures.append("FORBIDDEN_TOOL_ATTEMPTED")
        if script.final_status != case.expected.final_status:
            failures.append("FINAL_STATUS_MISMATCH")
        if script.error_code is not None and script.error_code not in case.expected.allowed_error_codes:
            failures.append("UNEXPECTED_ERROR_CODE")
        for required in case.expected.required_constraints:
            if required not in script.satisfied_constraints:
                failures.append("REQUIRED_CONSTRAINT_MISSING")
        hallucinations = sum(
            1 for fact in script.facts if not _contains_fact(evidence_facts, fact)
        )
        if hallucinations:
            failures.append("SEVERE_FACT_HALLUCINATION")
        for fact in case.expected.required_facts:
            if not _contains_fact(script.facts, fact):
                failures.append("REQUIRED_FACT_MISSING")

        recovery_succeeded = True
        if case.category == "PERSISTENT_TASK_RECOVERY":
            recovery_succeeded = _simulate_recovery(fixture)
            if not recovery_succeeded:
                failures.append("RECOVERY_FAILED")
        transaction_passed = all(fixture.transaction_assertions.values())
        if "critical_transaction" in case.tags:
            transaction_passed = (
                transaction_passed
                and len(fixture.transaction_assertions) >= 4
                and script.confirmation_decision in {"APPROVE", "REJECT"}
            )
            if not transaction_passed:
                failures.append("TRANSACTION_ASSERTION_FAILED")

        # Expected auth/degradation errors describe successful safe behavior, not a failed Case.
        failures = [
            code
            for code in failures
            if code not in case.expected.allowed_error_codes
        ]
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        return CaseExecution(
            case_id=case.case_id,
            category=case.category,
            status="PASSED" if not failures else "FAILED",
            final_status=script.final_status,
            failure_codes=sorted(set(failures)),
            deterministic_metrics={
                "schema_attempted": schema_attempted,
                "schema_valid": schema_valid,
                "severe_hallucinations": hallucinations,
                "unauthorized_write_calls": unauthorized_write_calls,
                "transaction_assertions_passed": transaction_passed,
                "recovery_succeeded": recovery_succeeded,
            },
            duration_ms=duration_ms,
        )


def _authorized(case: EvalCaseV1, risk: RiskLevel, decision: str | None) -> bool:
    if risk is RiskLevel.READ_ONLY:
        return True
    if not case.auth.authenticated or not case.auth.email_verified:
        return False
    if risk is RiskLevel.COMMIT and decision not in {"APPROVE", "REJECT"}:
        return False
    return True


def _contains_fact(values: list[dict], expected: dict) -> bool:
    return any(canonical_json(item) == canonical_json(expected) for item in values)


def _simulate_recovery(fixture) -> bool:
    recovery = fixture.recovery
    if recovery is None:
        return False
    consumed_event_ids: set[str] = set()
    notifications: set[str] = set()
    event_id = f"event:{recovery.crash_point}"
    for _ in range(recovery.deliveries):
        if event_id in consumed_event_ids:
            continue
        # Idempotency markers and notification IDs are committed atomically on successful recovery.
        notifications.add(event_id)
        consumed_event_ids.add(event_id)
    return len(notifications) == recovery.expected_unique_notifications
