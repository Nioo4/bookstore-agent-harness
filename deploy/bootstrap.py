"""Initialize local service accounts and Ed25519 keys without printing credentials."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import asyncmy
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


async def main() -> None:
    database = os.environ["MYSQL_DATABASE"]
    if not re.fullmatch(r"[A-Za-z0-9_]+", database):
        raise ValueError("invalid local database name")
    connection = await asyncmy.connect(
        host="mysql", user="root", password=os.environ["MYSQL_ROOT_PASSWORD"], db=database,
    )
    accounts = {
        "bookstore_business": (
            "MYSQL_BUSINESS_PASSWORD",
            ["book", "stock_deduction", "author", "category", "book_author", "book_category", "inventory",
             "user_account", "email_verification_token", "pending_action", "customer_order", "order_item", "outbox_event"],
        ),
        "bookstore_agent": (
            "MYSQL_AGENT_PASSWORD",
            ["user_memory", "memory_outbox", "agent_task", "notification", "inventory_event_consumption",
             "agent_run", "agent_step", "agent_evidence", "agent_tool_fixture", "evaluation_run", "evaluation_case_result"],
        ),
        "bookstore_publisher": ("MYSQL_PUBLISHER_PASSWORD", ["outbox_event"]),
    }
    try:
        async with connection.cursor() as cursor:
            for username, (password_name, tables) in accounts.items():
                password = os.environ[password_name]
                await cursor.execute(f"CREATE USER IF NOT EXISTS '{username}'@'%%' IDENTIFIED BY %s", (password,))
                await cursor.execute(f"ALTER USER '{username}'@'%%' IDENTIFIED BY %s", (password,))
                privileges = "SELECT,UPDATE,DELETE" if username == "bookstore_publisher" else "SELECT,INSERT,UPDATE,DELETE"
                for table in tables:
                    await cursor.execute(f"GRANT {privileges} ON `{database}`.`{table}` TO '{username}'@'%'")
            # Read-only migration metadata, not access to another service's business data.
            await cursor.execute(f"GRANT SELECT ON `{database}`.`schema_migrations` TO 'bookstore_agent'@'%'")
        await connection.commit()
    finally:
        connection.close()

    private_path = Path("/keys/private/jwt-private.pem")
    public_path = Path("/keys/public/jwt-public.pem")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    if private_path.exists() != public_path.exists():
        raise RuntimeError("incomplete JWT key pair; restore both files")
    if not private_path.exists():
        key = Ed25519PrivateKey.generate()
        private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        public_path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    for path in (private_path, public_path):
        path.chmod(0o600)
        if hasattr(os, "chown"):
            os.chown(path, 10001, 10001)
    print("service accounts and JWT keys are ready")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit("bootstrap failed; check dependency health and configured secrets") from None
