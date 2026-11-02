"""Initialize local service accounts and Ed25519 keys without printing credentials."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import asyncmy
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class BootstrapFailure(RuntimeError):
    """An error safe to show in a container log (no SQL or secret values)."""


def safe_failure(stage: str, error: BaseException) -> BootstrapFailure:
    error_class = type(error).__name__
    errno = next(
        (value for value in getattr(error, "args", ()) if isinstance(value, int)),
        None,
    )
    suffix = f" mysql_errno={errno}" if errno is not None else ""
    return BootstrapFailure(f"bootstrap failed stage={stage} error={error_class}{suffix}")


async def main() -> None:
    stage = "load-config"
    connection = None
    try:
        database = os.environ["MYSQL_DATABASE"]
        if not re.fullmatch(r"[A-Za-z0-9_]+", database):
            raise ValueError("invalid local database name")

        stage = "connect-mysql"
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
                ["user_memory", "memory_outbox", "agent_task", "notification",
                 "agent_run", "agent_step", "agent_evidence", "agent_tool_fixture"],
            ),
        }
        async with connection.cursor() as cursor:
            for username, (password_name, tables) in accounts.items():
                stage = f"read-password:{username}"
                password = os.environ[password_name]
                # asyncmy applies Python %-formatting when parameters are
                # supplied; %% is the literal MySQL host wildcard.
                stage = f"create-user:{username}"
                await cursor.execute(f"CREATE USER IF NOT EXISTS '{username}'@'%%' IDENTIFIED BY %s", (password,))
                stage = f"alter-user:{username}"
                await cursor.execute(f"ALTER USER '{username}'@'%%' IDENTIFIED BY %s", (password,))
                stage = f"revoke-user:{username}"
                await cursor.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM '{username}'@'%'")
                for table in tables:
                    privileges = "SELECT,INSERT,UPDATE,DELETE"
                    if username == "bookstore_agent":
                        privileges = {
                            "user_memory": "SELECT,INSERT,UPDATE",
                            "memory_outbox": "SELECT,INSERT,UPDATE",
                            "agent_task": "SELECT,INSERT,UPDATE",
                            "notification": "SELECT,UPDATE",
                            "agent_run": "SELECT,INSERT,UPDATE",
                            "agent_step": "SELECT,INSERT,UPDATE",
                            "agent_evidence": "SELECT,INSERT,UPDATE",
                            "agent_tool_fixture": "SELECT",
                        }[table]
                    stage = f"grant:{username}:{table}"
                    await cursor.execute(f"GRANT {privileges} ON `{database}`.`{table}` TO '{username}'@'%'")
            stage = "read-password:bookstore_worker"
            worker_password = os.environ["MYSQL_WORKER_PASSWORD"]
            stage = "create-user:bookstore_worker"
            await cursor.execute(
                "CREATE USER IF NOT EXISTS 'bookstore_worker'@'%%' IDENTIFIED BY %s", (worker_password,)
            )
            stage = "alter-user:bookstore_worker"
            await cursor.execute(
                "ALTER USER 'bookstore_worker'@'%%' IDENTIFIED BY %s", (worker_password,)
            )
            stage = "revoke-user:bookstore_worker"
            await cursor.execute("REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'bookstore_worker'@'%'")
            worker_grants = {
                "outbox_event": "SELECT,UPDATE",
                "inventory": "SELECT",
                "inventory_event_consumption": "SELECT,INSERT,UPDATE",
                "agent_task": "SELECT,UPDATE",
                "notification": "SELECT,INSERT,UPDATE",
                # Retention cleanup deletes only the parent; trace child rows
                # are removed by the existing ON DELETE CASCADE FKs.
                "agent_run": "SELECT,DELETE",
                # Legacy EPISODE cleanup only: the worker may claim and mark
                # delete events, but it cannot create/delete memory rows.
                "memory_outbox": "SELECT,UPDATE",
                "user_memory": "SELECT,UPDATE",
            }
            for table, privileges in worker_grants.items():
                stage = f"grant:bookstore_worker:{table}"
                await cursor.execute(f"GRANT {privileges} ON `{database}`.`{table}` TO 'bookstore_worker'@'%'")
            # Read-only migration metadata, not access to another service's business data.
            stage = "grant:bookstore_agent:schema_migrations"
            await cursor.execute(f"GRANT SELECT ON `{database}`.`schema_migrations` TO 'bookstore_agent'@'%'")
        stage = "commit-service-accounts"
        await connection.commit()

        stage = "prepare-jwt-directories"
        private_path = Path("/keys/private/jwt-private.pem")
        public_path = Path("/keys/public/jwt-public.pem")
        private_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.parent.mkdir(parents=True, exist_ok=True)
        if private_path.exists() != public_path.exists():
            raise RuntimeError("incomplete JWT key pair")
        if not private_path.exists():
            stage = "generate-jwt-key-pair"
            key = Ed25519PrivateKey.generate()
            private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            public_path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        for path in (private_path, public_path):
            stage = f"protect-jwt-key:{path.name}"
            path.chmod(0o600)
            if hasattr(os, "chown"):
                os.chown(path, 10001, 10001)
        print("service accounts and JWT keys are ready")
    except Exception as error:
        raise safe_failure(stage, error) from None
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BootstrapFailure as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        raise SystemExit(str(safe_failure("unclassified", error))) from None
