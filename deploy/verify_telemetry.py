"""Verify optional V2 observability for a fixture-backed Compose run.

The check covers technical spans, metric movement and privacy projection.  It
does not turn the observability profile into a required production service and
never calls a paid model provider.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import secrets
import subprocess
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
TEST_PROJECT = re.compile(r"bookstore-(?:stage11|test)[a-z0-9-]*")
FORBIDDEN_SERVICES = {
    "etcd",
    "add",
    "add-rpc",
    "check",
    "check-rpc",
    "auth",
    "auth-rpc",
    "order",
    "order-rpc",
    "outbox-publisher",
    "knowledge-index",
    "trace-worker",
}
QUERIES = {
    "completed": 'sum(bookstore_agent_tasks_total{status="COMPLETED"}) or vector(0)',
    "failed": 'sum(bookstore_agent_tasks_total{status="FAILED"}) or vector(0)',
    "input_tokens": 'sum(bookstore_agent_model_tokens_total{direction="input"}) or vector(0)',
    "output_tokens": 'sum(bookstore_agent_model_tokens_total{direction="output"}) or vector(0)',
    "tool_errors": 'sum(bookstore_agent_tool_errors_total) or vector(0)',
}


def metric_snapshot(client: httpx.Client, base: str = "http://127.0.0.1:9090", auth=None) -> dict[str, float]:
    values: dict[str, float] = {}
    for name, query in QUERIES.items():
        response = client.get(base + "/api/v1/query", params={"query": query}, auth=auth)
        response.raise_for_status()
        result = response.json()
        assert result.get("status") == "success" and len(result.get("data", {}).get("result", [])) == 1, "METRIC_RESULT_MISSING"
        values[name] = float(result["data"]["result"][0]["value"][1])
        assert math.isfinite(values[name]), "METRIC_NOT_FINITE"
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify optional V2 observability")
    parser.add_argument("--project", default="bookstore-stage11")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    args = parser.parse_args()
    if not TEST_PROJECT.fullmatch(args.project):
        raise SystemExit("TEST_PROJECT_REQUIRED")
    if args.base_url.rstrip("/") != "http://127.0.0.1:8088":
        raise SystemExit("TEST_BASE_URL_REQUIRED")

    # Observability is intentionally opt-in in compose.yaml.  This script is
    # the explicit profile check, not a reason to start it in every deployment.
    compose = [
        "docker",
        "compose",
        "--profile",
        "observability",
        "-p",
        args.project,
        "-f",
        "compose.yaml",
        "-f",
        "compose.e2e.yaml",
    ]

    def command(parts: list[str], *, timeout: int = 60) -> str:
        result = subprocess.run(
            compose + parts,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError("TELEMETRY_COMPOSE_COMMAND_FAILED")
        return result.stdout.strip()

    services = set(command(["config", "--services"]).splitlines())
    assert not services & FORBIDDEN_SERVICES, "V1_SERVICE_PRESENT"
    for service, port, expected in (
        ("nginx", "80", "127.0.0.1:8088"),
        ("prometheus", "9090", "127.0.0.1:9090"),
        ("tempo", "3200", "127.0.0.1:3200"),
        ("grafana", "3000", "127.0.0.1:3001"),
    ):
        assert command(["port", service, port]) == expected, "TEST_ENDPOINT_MISMATCH"
    command(
        [
            "exec",
            "-T",
            "agent-api",
            "python",
            "-c",
            "import os; assert os.environ['MODEL_BASE_URL']=='http://model-fixture:8010/v1'; assert os.environ['ENVIRONMENT']=='test'",
        ]
    )

    started = datetime.now(UTC).isoformat()
    trace_id = secrets.token_hex(16)
    sentinel = "telemetry-private-" + secrets.token_hex(8)
    parent = f"00-{trace_id}-{secrets.token_hex(8)}-01"
    with httpx.Client(timeout=125, headers={"Origin": args.base_url}) as client:
        baseline = metric_snapshot(client)
        response = client.post(
            args.base_url + "/api/chat/conversations/telemetry-v2/messages",
            headers={"traceparent": parent, "User-Agent": sentinel, "baggage": "secret=" + sentinel},
            json={"content": "查看书籍 1 当前库存 " + sentinel},
        )
        assert response.status_code == 200 and "event: message.completed" in response.text, "TELEMETRY_CHAT_FAILED"

        # The fixture emits usage, so a completed run and model counters must
        # move.  Tool-error movement is intentionally not required: a healthy
        # fixture run has zero tool errors.
        after = baseline
        for _ in range(30):
            after = metric_snapshot(client)
            if after["completed"] > baseline["completed"] and after["input_tokens"] >= baseline["input_tokens"] and after["output_tokens"] >= baseline["output_tokens"]:
                break
            time.sleep(1)
        assert after["completed"] > baseline["completed"], "METRICS_DID_NOT_REFLECT_RUN"

        expected_spans = (
            "http.request",
            "guest.chat",
            "agent.node",
            "tool.invoke",
            "model.complete",
            "bookstore-service",
            "bookstore-agent-api",
        )
        body: dict = {}
        for _ in range(30):
            trace_response = client.get("http://127.0.0.1:3200/api/traces/" + trace_id)
            if trace_response.status_code == 200:
                body = trace_response.json()
                serialized = json.dumps(body, ensure_ascii=False)
                # Child spans can arrive before the HTTP parent.
                if all(expected in serialized for expected in expected_spans):
                    break
            time.sleep(1)
        serialized = json.dumps(body, ensure_ascii=False)
        for expected in expected_spans:
            assert expected in serialized, "MISSING_TECHNICAL_SPAN_" + expected
        forbidden = (
            sentinel,
            "telemetry-v2",
            "supervisor",
            "DAG",
            "checkpoint",
            "redis.stream",
            "bookstore-check-rpc",
            "bookstore-add-rpc",
            "bookstore-auth-rpc",
            "bookstore-order-rpc",
            "db.statement",
            "user_agent",
            "client.address",
            "http.url",
            "exception.message",
            "baggage",
            "confirmation_token",
        )
        for value in forbidden:
            assert value not in serialized, "SENSITIVE_OR_V1_TELEMETRY_FIELD"

        logs = command(["logs", "--no-color", "--no-log-prefix", "--since", started, "agent-api"])
        assert sentinel not in logs and "telemetry-v2" not in logs, "PRIVATE_PROCESS_LOG"

        # Verify the optional dashboard consumes the same Prometheus source,
        # without asserting old V1 panel names or RPC target counts.
        grafana_id = command(["ps", "-q", "grafana"])
        inspected = subprocess.run(
            ["docker", "inspect", grafana_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert inspected.returncode == 0, "GRAFANA_INSPECTION_FAILED"
        environment = dict(
            item.split("=", 1)
            for item in json.loads(inspected.stdout)[0]["Config"]["Env"]
            if "=" in item
        )
        auth = httpx.BasicAuth(environment["GF_SECURITY_ADMIN_USER"], environment["GF_SECURITY_ADMIN_PASSWORD"])
        dashboard = client.get("http://127.0.0.1:3001/api/dashboards/uid/bookstore-agent-overview", auth=auth)
        dashboard.raise_for_status()
        panels = dashboard.json()["dashboard"].get("panels", [])
        assert panels and all(panel.get("datasource", {}).get("uid") == "prometheus" for panel in panels), "GRAFANA_DATASOURCE_MISMATCH"
        displayed = metric_snapshot(client, "http://127.0.0.1:3001/api/datasources/proxy/uid/prometheus", auth)
        assert displayed["completed"] >= after["completed"], "GRAFANA_DATA_NOT_CURRENT"

        targets = client.get("http://127.0.0.1:9090/api/v1/targets").json()["data"]["activeTargets"]
        jobs = {target.get("labels", {}).get("job") for target in targets if target.get("health") == "up"}
        assert jobs == {"bookstore-go", "bookstore-agent"}, "PROMETHEUS_TARGET_GRAPH_MISMATCH"
        print(
            json.dumps(
                {
                    "result": "TELEMETRY_PASS",
                    "trace_id": trace_id,
                    "metric_before": baseline,
                    "metric_after": after,
                    "grafana_values": displayed,
                    "healthy_jobs": sorted(jobs),
                    "paid_model_calls": False,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        location = next((frame.lineno for frame in reversed(frames) if frame.name == "main"), 0)
        raise SystemExit(f"TELEMETRY_FAILED main:{location}; {type(exc).__name__}; sensitive details omitted") from None
