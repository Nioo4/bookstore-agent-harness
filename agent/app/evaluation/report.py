from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from app.evaluation.repository import EvaluationCaseRecord, EvaluationRunRecord
from app.trace.types import utc_text
from app.trace.errors import TraceError


def report_payload(run: EvaluationRunRecord, cases: list[EvaluationCaseRecord]) -> dict:
    return {
        "eval_run_id": run.eval_run_id,
        "suite_version": run.suite_version,
        "dataset_sha256": run.dataset_hash.hex(),
        "model_profile": run.model_profile,
        "prompt_set_version": run.prompt_set_version,
        "proof_scope": "HISTORICAL_V1_EXECUTOR_ONLY_NOT_RELEASE_EVIDENCE" if run.suite_version == "v1" else "PRODUCTION_HARNESS_V2",
        "runner_version": run.runner_version,
        "mode": run.mode,
        "status": run.status,
        "judge_status": run.judge_status,
        "case_count": run.case_count,
        "passed_count": run.passed_count,
        "failed_count": run.failed_count,
        "error_count": run.error_count,
        "deterministic_gate_passed": run.deterministic_gate_passed,
        "metrics": run.metrics,
        "cost": {
            "status": run.cost_status.value,
            "known_microusd": run.known_cost_microusd,
            "currency": "USD",
        },
        "started_at": utc_text(run.started_at),
        "completed_at": utc_text(run.completed_at),
        "cases": [
            {
                "case_id": case.case_id,
                "agent_run_id": case.agent_run_id,
                "category": case.category,
                "status": case.status,
                "failure_codes": case.failure_codes,
                "deterministic_metrics": {key:value for key,value in case.deterministic_metrics.items() if key != "production_evidence"},
                **({"production_evidence":case.deterministic_metrics["production_evidence"]} if "production_evidence" in case.deterministic_metrics else {}),
                "judge_scores": case.judge_scores,
                "judge_reason": case.judge_reason,
                "duration_ms": case.duration_ms,
            }
            for case in cases
        ],
    }


def write_reports(
    artifact_dir: Path, run: EvaluationRunRecord, cases: list[EvaluationCaseRecord]
) -> tuple[Path, Path]:
    try:
        UUID(run.eval_run_id)
    except ValueError:
        raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "invalid evaluation report identity") from None
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = report_payload(run, cases)
    json_path = artifact_dir / f"{run.eval_run_id}.json"
    markdown_path = artifact_dir / f"{run.eval_run_id}.md"
    json_content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    metrics = payload.get("metrics") or {}
    lines = [
        f"# Evaluation Report {run.eval_run_id}",
        "",
        f"- Suite: `{run.suite_version}`",
        f"- Dataset SHA256: `{run.dataset_hash.hex()}`",
        f"- Runner: `{run.runner_version}`",
        f"- Profile / Prompt: `{run.model_profile}` / `{run.prompt_set_version}`",
        f"- Proof scope: `{payload['proof_scope']}`",
        f"- Status: `{run.status}`",
        f"- Deterministic gate: `{'PASS' if run.deterministic_gate_passed else 'FAIL'}`",
        f"- Cases: `{run.passed_count} passed / {run.failed_count} failed / {run.error_count} error`",
        f"- Judge: `{run.judge_status}` (informational only)",
        "",
        "## Hard metrics",
        "",
        f"- Critical transactions: `{metrics.get('critical_transaction_passed', 0)}/{metrics.get('critical_transaction_total', 0)}`",
        f"- Severe hallucinations: `{metrics.get('severe_hallucinations', 0)}`",
        f"- Tool Schema: `{metrics.get('tool_schema_valid', 0)}/{metrics.get('tool_schema_attempted', 0)}`",
        f"- Fixed task completion: `{metrics.get('task_completed', 0)}/{metrics.get('task_total', 0)}`",
        f"- Recovery: `{metrics.get('recovery_passed', 0)}/{metrics.get('recovery_total', 0)}`",
        f"- Unauthorized write calls: `{metrics.get('unauthorized_write_calls', 0)}`",
        "",
        "## Failed cases",
        "",
    ]
    failed = [case for case in cases if case.status != "PASSED"]
    lines.extend(
        [f"- `{case.case_id}`: {', '.join(case.failure_codes) or case.status}" for case in failed]
        or ["None."]
    )
    outputs = [(json_path, json_content.encode("utf-8")), (markdown_path, ("\n".join(lines) + "\n").encode("utf-8"))]
    # Preflight both files, so an existing conflict cannot cause the other report
    # to be changed. Exclusive create also closes the concurrent-writer race.
    for path, content in outputs:
        if path.is_symlink() or (path.exists() and path.read_bytes() != content):
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "immutable evaluation report conflicts with existing content")
    for path, content in outputs:
        try:
            with path.open("xb") as output:
                output.write(content)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "immutable evaluation report conflicts with existing content") from None
    return json_path, markdown_path
