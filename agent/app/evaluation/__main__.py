from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
import hashlib

from app.evaluation.loader import load_suite
from app.evaluation.judge import EvaluationJudge
from app.evaluation.report import write_reports
from app.evaluation.repository import AsyncMySQLEvaluationRepository
from app.evaluation.runner import EvaluationRunner
from app.evaluation.real_dependencies import RealEvalDependencies, DATABASE
from app.memory.repository import _parse_mysql_url
from app.tools.business import build_business_replay_definitions, build_business_tool_registry
from app.trace.errors import TraceError
from app.trace.manifests import VersionRegistry
from app.trace.repository import AsyncMySQLTraceRepository
from app.trace.types import canonical_json
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.configuration import AGENT_ROOT, ManifestSettings, ModelSettings
from app.models.factory import ModelRuntime, load_versions


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m app.evaluation")
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--suite", required=True)
    run.add_argument("--idempotency-key", required=True)
    run.add_argument("--with-judge", action="store_true")
    run.add_argument("--artifact-dir")
    report = sub.add_parser("report")
    report.add_argument("--eval-run-id", required=True)
    report.add_argument("--artifact-dir")
    return root


async def async_main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    dsn = os.getenv("AGENT_MYSQL_DSN") or os.getenv("AGENT_TRACE_MYSQL_DSN")
    if not dsn:
        print("EVAL_DEPENDENCY_UNAVAILABLE: AGENT_MYSQL_DSN is required", file=sys.stderr)
        return 3
    repository = None
    trace_repository = None
    judge_provider = None
    real_dependencies = None
    try:
        _assert_isolated_dsn(dsn)
        repository = await AsyncMySQLEvaluationRepository.create_pool(dsn)
        trace_repository = await AsyncMySQLTraceRepository.create_pool(dsn)
        if args.command == "run":
            suite = load_suite(Path(args.suite))
            versions = load_versions(ManifestSettings())
            versions.assert_database_references(await trace_repository.manifest_references())
            v2 = suite.manifest.suite_version == "v2"
            if v2:
                real_dependencies = RealEvalDependencies.from_environment()
                if real_dependencies is None:
                    raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "v2 requires explicitly identified real dependencies")
                report_db, fixture_db = _parse_mysql_url(dsn), _parse_mysql_url(real_dependencies.mysql_dsn)
                if report_db["db"] != DATABASE or any(report_db.get(key) != fixture_db.get(key) for key in ("host","port","db")):
                    raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "v2 reports and fixtures must use the same isolated MySQL endpoint")
                await real_dependencies.open()
                # Construction is read-only; no adapter method is called here.
                tool_version = _v2_tool_registry_version()
            else:
                manifests = [item.manifest() for item in build_business_replay_definitions()]
                tool_version = hashlib.sha256(canonical_json(manifests).encode("utf-8")).hexdigest()
            if tool_version != versions.tool_registry_versions["evaluation-v2" if v2 else "evaluation-v1"]:
                raise TraceError(
                    "EVAL_DEPENDENCY_UNAVAILABLE",
                    "evaluation Tool Registry does not match its retained version artifact",
                )
            judge = None
            if args.with_judge:
                model_settings = ModelSettings()
                if not model_settings.llm_judge_enabled:
                    raise TraceError(
                        "EVAL_DEPENDENCY_UNAVAILABLE",
                        "Judge requires explicit LLM_JUDGE_ENABLED configuration and --with-judge",
                    )
                runtime = ModelRuntime(model_settings)
                profile = versions.model_profile(model_settings.judge_model_profile)
                judge_provider = runtime.chat()
                judge = EvaluationJudge(
                    judge_provider,
                    profile.agent,
                    versions.prompt_text(model_settings.judge_prompt_version, "judge"),
                    model_profile=model_settings.judge_model_profile,
                    prompt_version=model_settings.judge_prompt_version,
                )
            run = await EvaluationRunner(
                repository,
                trace_repository=trace_repository,
                versions=versions,
                tool_registry_version=tool_version,
                runner_version=f"evaluation-v1+{os.getenv('BUILD_COMMIT', 'development')}",
                judge=judge,
                real_dependencies=real_dependencies,
            ).run(
                suite,
                idempotency_key=args.idempotency_key,
                with_judge=args.with_judge,
            )
        else:
            run = await repository.get(args.eval_run_id)
        cases = await repository.list_cases(run.eval_run_id)
        artifact_dir = Path(args.artifact_dir or ManifestSettings().eval_artifact_dir)
        if not artifact_dir.is_absolute():
            artifact_dir = AGENT_ROOT / artifact_dir
        json_path, markdown_path = write_reports(artifact_dir, run, cases)
        print(f"eval_run_id={run.eval_run_id}")
        print(f"json_report={json_path}")
        print(f"markdown_report={markdown_path}")
        if run.error_count or any(case.status in {"ERROR", "SKIPPED"} for case in cases):
            return 3
        return 0 if run.deterministic_gate_passed else 2
    except TraceError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 3
    except Exception:
        print("EVAL_DEPENDENCY_UNAVAILABLE: evaluation command failed", file=sys.stderr)
        return 3
    finally:
        if real_dependencies is not None:
            await real_dependencies.aclose()
        if repository is not None:
            await repository.aclose()
        if trace_repository is not None:
            await trace_repository.aclose()
        if judge_provider is not None:
            await judge_provider.aclose()


def _v2_tool_registry_version() -> str:
    # Include the optional knowledge tool used by every v2 production driver.
    # These inert objects are only used to construct the contract, never invoked.
    return build_business_tool_registry(object(), object(), object()).version


def _assert_isolated_dsn(dsn: str) -> None:
    database = str(_parse_mysql_url(dsn)["db"]).casefold()
    if "test" not in database and "eval" not in database:
        raise TraceError(
            "EVAL_DEPENDENCY_UNAVAILABLE",
            "evaluation CLI requires a dedicated test or evaluation database",
        )


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
