"""Real test-stack auth edge cases; only synthetic rows, no printed credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import traceback
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="bookstore-stage11")
    args = parser.parse_args()
    if not re.fullmatch(r"bookstore-(stage11|test)[a-z0-9-]*", args.project):
        raise SystemExit("TEST_PROJECT_REQUIRED")
    compose = ["docker", "compose", "-p", args.project, "-f", "compose.yaml", "-f", "compose.e2e.yaml"]

    def command(parts, body=None):
        result = subprocess.run(compose + parts, input=body, cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        if result.returncode:
            raise RuntimeError("TEST_COMPOSE_COMMAND_FAILED")
        return result.stdout.strip()

    def sql(statement):
        return command(["exec", "-T", "mysql", "sh", "-c",
                        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -N -B -uroot -D "$MYSQL_DATABASE"'], statement)

    def response_data(response, status=200, code=None):
        if response.status_code != status:
            raise AssertionError(f"HTTP_{response.status_code}_EXPECTED_{status}")
        data = response.json()
        if code is not None and data.get("error", {}).get("code") != code:
            raise AssertionError("UNEXPECTED_PUBLIC_ERROR_CODE")
        return data

    # Verify the named project owns the tested ingress before doing any writes.
    if command(["port", "nginx", "80"]) != "127.0.0.1:8088":
        raise SystemExit("TEST_INGRESS_MISMATCH")
    command(["exec", "-T", "agent-api", "python", "-c",
        "import os; assert os.environ['MODEL_BASE_URL']=='http://model-fixture:8010/v1'; assert os.environ['ENVIRONMENT']=='test'"])
    unique = uuid.uuid4().hex[:12].translate(str.maketrans("0123456789", "ghijklmnop"))
    email, password = f"auth-edge.{unique}@example.test", secrets.token_urlsafe(24)
    book_id = int(sql(f"INSERT INTO book(isbn,title,price,stock_count) VALUES('auth-{unique}','Auth Boundary Book',4200,2); SET @book=LAST_INSERT_ID(); INSERT INTO inventory(book_id,available_stock) VALUES(@book,2); SELECT @book;"))
    with httpx.Client(base_url="http://127.0.0.1:8088", timeout=120, headers={"Origin": "http://127.0.0.1:8088"}) as client:
        body = {"email": email, "password": password, "display_name": "Auth Boundary Test"}
        registered = response_data(client.post("/api/auth/register", json=body), 202)
        assert registered["verification_email_sent"] is True, "INITIAL_SMTP_FAILED"
        user_id = int(registered["user_id"])
        response_data(client.post("/api/auth/register", json=body), 409, "AUTH_EMAIL_EXISTS")
        response_data(client.post("/api/auth/login", json={"email": email, "password": "wrong-test-password"}), 401, "AUTH_INVALID_CREDENTIALS")
        unverified = response_data(client.post("/api/auth/login", json={"email": email, "password": password}))
        assert unverified["user"]["email_verified"] is False
        assert "access_token" not in unverified and client.cookies.get("bookstore_access"), "COOKIE_BOUNDARY_FAILED"
        counts = f"SELECT COUNT(*) FROM pending_action WHERE user_id={user_id}; SELECT COUNT(*) FROM customer_order WHERE user_id={user_id}; SELECT COUNT(*) FROM agent_task WHERE user_id={user_id}; SELECT available_stock FROM inventory WHERE book_id={book_id};"
        before = sql(counts)
        conversation = str(uuid.uuid4())
        response_data(client.post("/api/tasks/inventory-watches", headers={"Idempotency-Key": str(uuid.uuid4())},
                                  json={"conversation_id": conversation, "book_id": book_id, "desired_quantity": 1}),
                      403, "EMAIL_NOT_VERIFIED")
        chat = client.post(f"/api/chat/conversations/{conversation}/messages", json={"content": f"购买书籍 {book_id}"})
        assert chat.status_code == 200 and "event: confirmation.required" not in chat.text, "UNVERIFIED_CONFIRMATION"
        events = []
        for block in chat.text.replace("\r\n", "\n").split("\n\n"):
            event = next((line[6:].strip() for line in block.splitlines() if line.startswith("event:")), "")
            data = next((line[5:].strip() for line in block.splitlines() if line.startswith("data:")), "{}")
            events.append((event, json.loads(data)))
        assert any(event == "run.failed" and data.get("code") == "EMAIL_NOT_VERIFIED" for event, data in events), "UNVERIFIED_ORDER_NOT_REJECTED"
        assert before == sql(counts), "UNVERIFIED_WRITE_SIDE_EFFECT"
        print("PASS duplicate email/wrong password/unverified login and write rejection", flush=True)

        expired = secrets.token_urlsafe(32)
        digest = hashlib.sha256(expired.encode()).hexdigest()
        # Fixed expired timestamp, independent of the execution day's clock.
        sql(f"INSERT INTO email_verification_token(token_digest,user_id,expires_at) VALUES(UNHEX('{digest}'),{user_id},'2000-01-01 00:00:00');")
        response_data(client.post("/api/auth/verify-email", json={"token": expired}), 409, "EMAIL_VERIFICATION_EXPIRED")
        assert sql(f"SELECT email_verified_at IS NULL FROM user_account WHERE user_id={user_id};") == "1"
        assert sql(f"SELECT used_at IS NULL FROM email_verification_token WHERE token_digest=UNHEX('{digest}');") == "1"
        with httpx.Client(base_url="http://127.0.0.1:8025") as mail:
            messages = response_data(mail.get("/api/v1/messages"))["messages"]
            message = next(item for item in messages if any(recipient["Address"] == email for recipient in item["To"]))
            letter = response_data(mail.get("/api/v1/message/" + message["ID"]))
            token = re.search(r"token=([A-Za-z0-9_-]+)", letter["Text"])[1]
        response_data(client.post("/api/auth/verify-email", json={"token": token}))
        response_data(client.post("/api/auth/verify-email", json={"token": token}), 409, "EMAIL_VERIFICATION_ALREADY_USED")
        verified = response_data(client.post("/api/auth/login", json={"email": email, "password": password}))
        assert verified["user"]["email_verified"] is True
        print("PASS expired token is not consumed; valid token is one-use; fresh login reflects verification", flush=True)

        # The SMTP outage affects this explicitly named test stack only.
        try:
            command(["stop", "mailpit"])
            recovery_email = f"smtp-edge.{unique}@example.test"
            failed = response_data(client.post("/api/auth/register", json={**body, "email": recovery_email}), 202)
            assert failed["verification_email_sent"] is False, "SMTP_FAILURE_REPORTED_AS_SENT"
            failed_id = int(failed["user_id"])
            assert sql(f"SELECT email_verified_at IS NULL FROM user_account WHERE user_id={failed_id};") == "1"
        finally:
            command(["start", "mailpit"])
        recovered_login = response_data(client.post("/api/auth/login", json={"email": recovery_email, "password": password}))
        assert recovered_login["user"]["email_verified"] is False
        old_cookie = client.cookies.get("bookstore_access")
        resent = response_data(client.post("/api/auth/resend-verification", json={}), 202)
        assert resent["status"] == "SMTP_ACCEPTED" and resent["verification_email_sent"] is True
        assert resent["retry_after_seconds"] > 0
        limited = client.post("/api/auth/resend-verification", json={})
        response_data(limited, 429, "AUTH_RATE_LIMITED")
        assert int(limited.headers["retry-after"]) > 0 and limited.headers["cache-control"] == "no-store"
        with httpx.Client(base_url="http://127.0.0.1:8025") as mail:
            messages = response_data(mail.get("/api/v1/messages"))["messages"]
            matches = [item for item in messages if any(recipient["Address"] == recovery_email for recipient in item["To"])]
            assert len(matches) == 1, "RESEND_DUPLICATE_DELIVERY"
            letter = response_data(mail.get("/api/v1/message/" + matches[0]["ID"]))
            recovered_token = re.search(r"token=([A-Za-z0-9_-]+)", letter["Text"])[1]
        response_data(client.post("/api/auth/verify-email", json={"token": recovered_token}))
        protected_body = {"conversation_id": str(uuid.uuid4()), "book_id": book_id, "desired_quantity": 3}
        before_tasks = sql(f"SELECT COUNT(*) FROM agent_task WHERE user_id={failed_id};")
        response_data(client.post("/api/tasks/inventory-watches", json=protected_body,
            headers={"Idempotency-Key": str(uuid.uuid4())}), 403, "EMAIL_NOT_VERIFIED")
        assert sql(f"SELECT COUNT(*) FROM agent_task WHERE user_id={failed_id};") == before_tasks
        refreshed = response_data(client.post("/api/auth/login", json={"email": recovery_email, "password": password}))
        assert refreshed["user"]["email_verified"] and client.cookies.get("bookstore_access") != old_cookie
        owned_chat = client.post(f"/api/chat/conversations/{protected_body['conversation_id']}/messages",
            json={"content": f"查看书籍 {book_id} 当前库存"})
        assert owned_chat.status_code == 200 and "event: message.completed" in owned_chat.text, "OWNED_CONVERSATION_NOT_CREATED"
        created = response_data(client.post("/api/tasks/inventory-watches", json=protected_body,
            headers={"Idempotency-Key": str(uuid.uuid4())}), 201)
        assert created["task"]["status"] == "ACTIVE"
        task_id = created["task"]["task_id"]
        uuid.UUID(task_id)
        assert sql(f"SELECT COUNT(*) FROM agent_task WHERE task_id='{task_id}' AND user_id={failed_id};") == "1"
        print("PASS SMTP failure -> login -> explicit resend -> Mailpit verification -> fresh JWT protected write; old JWT denied", flush=True)
    print("AUTH_BOUNDARY_PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        location = next((frame.lineno for frame in reversed(frames) if frame.name == "main"), 0)
        raise SystemExit(f"AUTH_BOUNDARY_FAILED at main:{location}; {type(exc).__name__}; secrets omitted") from None
