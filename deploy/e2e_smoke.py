"""Fixture-only V2 Compose smoke test.

The fixture replaces the chat and embedding providers only.  MySQL, Redis,
Qdrant, Mailpit, the Go business service, Agent API/Worker and the Vite SPA
still run for real.  This script never enables or calls a paid provider.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import time
import traceback
import uuid
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
TEST_PROJECT = re.compile(r"bookstore-(?:stage11|test|v2-fixture)[a-z0-9-]*")
V2_SERVICES = {
    "mysql",
    "redis",
    "qdrant",
    "mailpit",
    "migrate",
    "bootstrap",
    "bookstore-service",
    "agent-api",
    "agent-worker",
    "nginx",
    "model-fixture",
}
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the V2 deterministic fixture smoke")
    parser.add_argument("--project", default="bookstore-stage11")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    args = parser.parse_args()
    if not TEST_PROJECT.fullmatch(args.project):
        raise SystemExit("TEST_PROJECT_REQUIRED")
    if args.base_url.rstrip("/") != "http://127.0.0.1:8088":
        raise SystemExit("TEST_BASE_URL_REQUIRED")

    compose = [
        "docker",
        "compose",
        "--profile",
        "v2-app",
        "-p",
        args.project,
        "-f",
        "compose.yaml",
        "-f",
        "compose.e2e.yaml",
    ]

    def command(parts: list[str], input_text: str | None = None, *, timeout: int = 120) -> str:
        result = subprocess.run(
            compose + parts,
            input=input_text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=ROOT,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError("COMPOSE_COMMAND_FAILED")
        return result.stdout.strip()

    def sql(statement: str) -> str:
        return command(
            [
                "exec",
                "-T",
                "mysql",
                "sh",
                "-c",
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -N -B -uroot -D "$MYSQL_DATABASE"',
            ],
            statement,
        )

    def privilege_probe(service: str, dsn_env: str, grant_marker: str,
                        allowed: list[str], forbidden: list[str]) -> None:
        """Exercise the service account inside its own container.

        The DSN is read from the container environment and is never printed.
        Allowed writes use a zero-row transaction and are rolled back; denied
        statements are reads or zero-impact updates, so this probe cannot
        create users, orders, stock changes, or other business rows.
        """
        probe = f'''\
import asyncio, os
from urllib.parse import urlsplit
import asyncmy

dsn = urlsplit(os.environ[{dsn_env!r}])
allowed = {allowed!r}
forbidden = {forbidden!r}

async def run():
    connection = await asyncmy.connect(
        host=dsn.hostname,
        port=dsn.port or 3306,
        user=dsn.username,
        password=dsn.password,
        db=dsn.path.lstrip("/"),
    )
    try:
        async with connection.cursor() as cursor:
            await cursor.execute("SHOW GRANTS")
            grants = [str(row[0]) for row in await cursor.fetchall()]
            if not any({grant_marker!r} in grant for grant in grants):
                raise RuntimeError("EXPECTED_GRANT_MISSING")
            await cursor.execute("START TRANSACTION")
            for statement in allowed:
                await cursor.execute(statement)
            await cursor.execute("ROLLBACK")
            for statement in forbidden:
                try:
                    await cursor.execute(statement)
                except Exception as error:
                    if int(error.args[0]) != 1142:
                        raise RuntimeError("UNEXPECTED_PRIVILEGE_ERROR") from error
                else:
                    raise RuntimeError("FORBIDDEN_OPERATION_ALLOWED")
    finally:
        connection.close()

asyncio.run(run())
'''
        command_args = ["exec", "-T", service, "python", "-c", "exec(" + repr(probe) + ")"]
        for attempt in range(30):
            try:
                command(command_args)
                return
            except RuntimeError:
                if attempt == 29:
                    raise AssertionError(f"PRIVILEGE_PROBE_FAILED_{service}") from None
                time.sleep(1)

    def json_payload(response: httpx.Response, expected: int = 200) -> dict:
        if response.status_code != expected:
            try:
                error = response.json().get("error", {})
                code = error.get("code", "UNKNOWN") if isinstance(error, dict) else "UNKNOWN"
            except ValueError:
                code = "NON_JSON"
            raise AssertionError(f"HTTP_{response.status_code}_{code}_EXPECTED_{expected}")
        body = response.json()
        if not isinstance(body, dict):
            raise AssertionError("JSON_OBJECT_EXPECTED")
        return body

    def parse_sse(response: httpx.Response, expected: int = 200) -> list[tuple[str, dict]]:
        if response.status_code != expected:
            json_payload(response, expected)
        events: list[tuple[str, dict]] = []
        for block in response.text.replace("\r\n", "\n").split("\n\n"):
            event = next((line[6:].strip() for line in block.splitlines() if line.startswith("event:")), "")
            data = "\n".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
            if data:
                value = json.loads(data)
                if isinstance(value, dict):
                    events.append((event, value))
        if any(event == "run.failed" for event, _ in events):
            failed = next(data for event, data in events if event == "run.failed")
            raise AssertionError("CHAT_" + str(failed.get("code", "FAILED")))
        if not any(event == "message.completed" for event, _ in events):
            raise AssertionError("CHAT_NO_COMPLETION")
        return events

    def chat(client: httpx.Client, content: str, conversation_id: str | None = None) -> tuple[str, list[tuple[str, dict]]]:
        conversation_id = conversation_id or str(uuid.uuid4())
        response = client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            json={"content": content, "developer_mode": True},
        )
        return conversation_id, parse_sse(response)

    def completed(events: list[tuple[str, dict]]) -> dict:
        return next(data for event, data in events if event == "message.completed")

    def wait_for(predicate, *, attempts: int = 60, delay: float = 1.0) -> None:
        for _ in range(attempts):
            if predicate():
                return
            time.sleep(delay)
        raise AssertionError("POLL_TIMEOUT")

    def agent_api_ready() -> bool:
        try:
            command(
                [
                    "exec",
                    "-T",
                    "agent-api",
                    "python",
                    "-c",
                    (
                        "import json,urllib.request; "
                        "payload=json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)); "
                        "assert payload.get('status')=='ok'"
                    ),
                ],
                timeout=10,
            )
            return True
        except (RuntimeError, subprocess.TimeoutExpired):
            return False

    # Compile-time/runtime guard: the fixture overlay must remain a test-only
    # provider and the service graph must be the V2 graph, not a renamed V1 one.
    services = set(command(["config", "--services"]).splitlines())
    assert V2_SERVICES <= services, "V2_SERVICE_SET_INCOMPLETE"
    assert not services & FORBIDDEN_SERVICES, "V1_SERVICE_PRESENT"
    assert command(["port", "nginx", "80"]) == "127.0.0.1:8088", "TEST_INGRESS_MISMATCH"
    command(
        [
            "exec",
            "-T",
            "agent-api",
            "python",
            "-c",
            "import os; assert os.environ['MODEL_BASE_URL']=='http://model-fixture:8010/v1'; assert os.environ['EMBEDDING_BASE_URL']=='http://model-fixture:8010/v1'; assert os.environ['ENVIRONMENT']=='test'",
        ]
    )
    print("PASS V2 service graph + fixture-only model boundary", flush=True)

    # Verify the least-privilege accounts against the live MySQL instance.
    # The worker may read inventory to match stock events, but it cannot write
    # inventory or read users/orders; the Agent account has no business-table
    # access and only performs a zero-row write on its own trace table.
    privilege_probe(
        "agent-api",
        "AGENT_MYSQL_DSN",
        "agent_run",
        ["SELECT COUNT(*) FROM agent_run", "UPDATE agent_run SET run_id=run_id WHERE 1=0"],
        ["SELECT COUNT(*) FROM user_account", "SELECT COUNT(*) FROM customer_order", "SELECT COUNT(*) FROM inventory"],
    )
    privilege_probe(
        "agent-worker",
        "WORKER_MYSQL_DSN",
        "outbox_event",
        ["SELECT COUNT(*) FROM inventory", "UPDATE outbox_event SET attempt_count=attempt_count WHERE 1=0"],
        ["SELECT COUNT(*) FROM user_account", "SELECT COUNT(*) FROM customer_order", "UPDATE inventory SET available_stock=available_stock WHERE 1=0"],
    )
    print("PASS least-privilege Agent/Worker MySQL probes", flush=True)

    # Test rows are uniquely scoped and never reset or delete existing data.
    unique = uuid.uuid4().hex[:12].translate(str.maketrans("0123456789", "ghijklmnop"))
    email = f"v2-smoke.{unique}@example.test"
    password = secrets.token_urlsafe(18)
    title = f"V2 Fixture Systems {unique}"
    out_of_stock_title = f"V2 Fixture Restock {unique}"
    book_id = int(
        sql(
            f"INSERT INTO book(isbn,title,price,stock_count,language_code) VALUES('v2-{unique}','{title}',4200,8,'en'); "
            "SET @book=LAST_INSERT_ID(); INSERT INTO inventory(book_id,available_stock) VALUES(@book,8); SELECT @book;"
        )
    )
    restock_book_id = int(
        sql(
            f"INSERT INTO book(isbn,title,price,stock_count,language_code) VALUES('v2-restock-{unique}','{out_of_stock_title}',3900,0,'zh-CN'); "
            "SET @book=LAST_INSERT_ID(); INSERT INTO inventory(book_id,available_stock) VALUES(@book,0); SELECT @book;"
        )
    )

    headers = {"Origin": args.base_url}
    with httpx.Client(base_url=args.base_url, timeout=125, headers=headers) as client:
        # Guest chat is request-local and must not create a durable Trace.
        _, guest_events = chat(client, f"查看书籍 {book_id} 当前库存")
        guest_result = completed(guest_events)
        assert guest_result.get("trace_persisted") is False and "trace_id" not in guest_result, "GUEST_TRACE_PERSISTED"
        print("PASS guest chat without durable Trace", flush=True)

        # Registration, Mailpit verification, and HttpOnly-cookie login.
        registration = json_payload(
            client.post(
                "/api/auth/register",
                json={"email": email, "password": password, "display_name": "V2 Fixture"},
            ),
            202,
        )
        assert registration.get("verification_email_sent") is True, "VERIFICATION_EMAIL_NOT_SENT"
        unverified = json_payload(client.post("/api/auth/login", json={"email": email, "password": password}))
        assert unverified.get("user", {}).get("email_verified") is False, "UNVERIFIED_LOGIN_BOUNDARY"
        with httpx.Client(base_url="http://127.0.0.1:8025", timeout=15) as mail:
            messages = json_payload(mail.get("/api/v1/messages")).get("messages", [])
            message = next(
                item
                for item in messages
                if any(recipient.get("Address") == email for recipient in item.get("To", []))
            )
            letter = json_payload(mail.get("/api/v1/message/" + message["ID"]))
            match = re.search(r"token=([A-Za-z0-9_-]+)", str(letter.get("Text", "")))
            if match is None:
                raise AssertionError("VERIFICATION_TOKEN_MISSING")
            verification_token = match.group(1)
        json_payload(client.post("/api/auth/verify-email", json={"token": verification_token}))
        login = json_payload(client.post("/api/auth/login", json={"email": email, "password": password}))
        assert "access_token" not in login and client.cookies.get("bookstore_access"), "HTTPONLY_COOKIE_BOUNDARY"
        assert json_payload(client.get("/api/session")).get("authenticated") is True, "SESSION_NOT_AUTHENTICATED"
        print("PASS register -> Mailpit verify -> HttpOnly login", flush=True)

        # Public Catalog query must work independently of the Agent.
        search = json_payload(client.get("/api/books/search", params={"q": title, "in_stock": "true"}))
        assert any(int(book.get("book_id", 0)) == book_id for book in search.get("books", [])), "CATALOG_QUERY_MISSING"
        print("PASS Catalog query", flush=True)

        # Authenticated chat must persist a Trace and return fixture-backed facts.
        conversation, recommendation_events = chat(client, f"推荐一本书，书名包含「{title}」")
        recommendation = completed(recommendation_events)
        assert recommendation.get("trace_persisted") is True and recommendation.get("trace_id"), "AUTH_TRACE_MISSING"
        assert any(int(item.get("book_id", 0)) == book_id for item in recommendation.get("recommendations", [])), "RECOMMENDATION_MISSING"
        source_run_id = str(recommendation.get("run_id"))
        print("PASS authenticated single-agent chat + recommendation", flush=True)

        # Explicit preference is a user action, not model-inferred episode memory.
        preference = json_payload(
            client.post("/api/memories/preferences", json={"key": "PREFERRED_LANGUAGE", "value": "zh-CN"})
        )
        assert preference.get("key") == "PREFERRED_LANGUAGE" and preference.get("value") == "zh-CN", "PREFERENCE_WRITE_FAILED"
        memories = json_payload(client.get("/api/memories")).get("memories", [])
        language_memory = next(
            (item for item in memories if item.get("memory_key") == "PREFERRED_LANGUAGE"),
            None,
        )
        assert language_memory is not None, "PREFERENCE_READ_FAILED"
        deleted = json_payload(client.delete(f"/api/memories/{language_memory['memory_id']}"))
        assert deleted.get("deleted_count") == 1, "PREFERENCE_DELETE_FAILED"
        after_delete = json_payload(client.get("/api/memories")).get("memories", [])
        assert not any(item.get("memory_key") == "PREFERRED_LANGUAGE" for item in after_delete), "DELETED_PREFERENCE_STILL_ACTIVE"
        recreated = json_payload(
            client.post("/api/memories/preferences", json={"key": "PREFERRED_LANGUAGE", "value": "中文"})
        )
        assert recreated.get("value") == "zh-CN" and recreated.get("created") is True, "PREFERENCE_RECREATE_FAILED"
        print("PASS explicit structured preference -> delete -> normalized recreate", flush=True)

        # Prepare is a preview only.  API restart must not lose the MySQL-bound
        # confirmation; a repeated decision is an idempotent replay.
        before_orders = int(sql("SELECT COUNT(*) FROM customer_order;"))
        _, purchase_events = chat(client, f"购买书籍 {book_id}", conversation)
        confirmation = next((data for event, data in purchase_events if event == "confirmation.required"), None)
        if not confirmation or not confirmation.get("confirmation_token"):
            raise AssertionError("CONFIRMATION_CARD_MISSING")
        assert int(sql("SELECT COUNT(*) FROM customer_order;")) == before_orders, "PREVIEW_CREATED_ORDER"
        command(["restart", "agent-api"])
        wait_for(agent_api_ready, attempts=45)
        confirmation_path = f"/api/chat/conversations/{conversation}/confirmations"
        decision = {"confirmation_token": confirmation["confirmation_token"], "decision": "APPROVE"}
        first = json_payload(client.post(confirmation_path, json=decision))
        replay = json_payload(client.post(confirmation_path, json=decision))
        assert first.get("order", {}).get("order_id") == replay.get("order", {}).get("order_id") and replay.get("replayed") is True, "CONFIRMATION_REPLAY_FAILED"
        order_id = int(first["order"]["order_id"])
        assert int(sql("SELECT COUNT(*) FROM customer_order;")) == before_orders + 1, "ORDER_NOT_CREATED"
        assert int(sql(f"SELECT available_stock FROM inventory WHERE book_id={book_id};")) == 7, "STOCK_NOT_DEBITED"
        json_payload(client.get(f"/api/orders/{order_id}"))
        print("PASS preview -> API restart -> confirmation -> idempotent order replay", flush=True)

        # Restock watch is a deterministic user-confirmed API action.  V2
        # recovery is MySQL Outbox + Worker; Redis Streams is deliberately not
        # restarted or consulted here.
        json_payload(client.put(f"/api/books/{restock_book_id}", json={"stock_count": 0}))
        watch = json_payload(
            client.post(
                "/api/tasks/inventory-watches",
                headers={"Idempotency-Key": uuid.uuid4().hex},
                json={"conversation_id": conversation, "book_id": restock_book_id, "desired_quantity": 1},
            ),
            201,
        )
        task_id = watch.get("task", {}).get("task_id")
        assert task_id, "WATCH_NOT_CREATED"
        command(["restart", "agent-worker"])
        json_payload(client.put(f"/api/books/{restock_book_id}", json={"stock_count": 1}))

        def notification_received() -> bool:
            response = client.get("/api/notifications")
            if response.status_code != 200:
                return False
            items = response.json().get("notifications", [])
            return len([item for item in items if item.get("task_id") == task_id]) == 1

        wait_for(notification_received, attempts=90)
        notifications = json_payload(client.get("/api/notifications")).get("notifications", [])
        matches = [item for item in notifications if item.get("task_id") == task_id]
        assert len(matches) == 1, "DUPLICATE_RESTOCK_NOTIFICATION"
        print("PASS MySQL Outbox restock watch -> Worker restart -> one notification", flush=True)

        # V2 keeps Trace read-only and removes public Replay/Evals APIs.
        traces = json_payload(client.get("/api/traces"))
        runs = traces.get("runs", [])
        run_ids = {str(item.get("run_id")) for item in runs}
        assert source_run_id in run_ids or runs, "TRACE_LIST_MISSING"
        trace_id = source_run_id if source_run_id in run_ids else str(runs[0]["run_id"])
        detail = json_payload(client.get(f"/api/traces/{trace_id}"))
        run = detail.get("run", {})
        assert run.get("run_kind", "LIVE") == "LIVE"
        assert run.get("replayable") is False, "TRACE_REPLAYABLE"
        assert run.get("dag") is None and run.get("dag_json") is None, "TRACE_DAG_PERSISTED"
        assert detail.get("steps") is not None and detail.get("evidence") is not None, "TRACE_DETAIL_INCOMPLETE"
        for method, path in (("get", "/api/evals"), ("get", f"/api/traces/{trace_id}/replays"), ("post", f"/api/traces/{trace_id}/replays")):
            response = client.get(path) if method == "get" else client.post(path, json={})
            assert response.status_code == 404, f"REMOVED_ROUTE_{method.upper()}_{path}"
        print("PASS Trace timeline + Replay/Evals 404", flush=True)

        # Nginx must serve the Vite index for a direct SPA deep link.
        spa = client.get(f"/traces/{trace_id}")
        assert spa.status_code == 200 and "<div id=\"root\">" in spa.text and "_next/" not in spa.text, "SPA_DEEP_LINK_FAILED"
        print("PASS Vite SPA deep link", flush=True)

    print("SMOKE_PASS (V2 fixture provider; real infrastructure, no paid model calls)", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Only locally generated assertion codes/locations are safe to show.
        code = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
        frames = traceback.extract_tb(exc.__traceback__)
        location = next((frame.lineno for frame in reversed(frames) if frame.name == "main"), 0)
        raise SystemExit(f"SMOKE_FAILED main:{location} {code}") from None
