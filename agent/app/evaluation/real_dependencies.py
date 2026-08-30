"""Small, explicit fixture boundary for the nine real-dependency v2 cases.

No reset/flush/DDL: a separately provisioned v10 database and dedicated Redis
instance must be identified before fixtures may write. Instance IDs are obtained
by the acceptance operator's read-only preflight, not inferred from a test name.
"""
from __future__ import annotations

import os
from uuid import UUID

import asyncmy
from asyncmy.cursors import DictCursor
from redis.asyncio import Redis

from app.evaluation.catalog import BOOKS
from app.memory.repository import _parse_mysql_url
from app.trace.errors import TraceError


DATABASE = "bookstore_eval_v2_test"


class RealEvalDependencies:
    def __init__(self, *, mysql_dsn, redis_url, mysql_server_uuid, redis_run_id):
        self.mysql_dsn, self.redis_url = mysql_dsn, redis_url
        self.mysql_server_uuid, self.redis_run_id = mysql_server_uuid, redis_run_id
        self.pool, self.redis, self.lock_connection = None, None, None
        self.identity = {}

    @classmethod
    def from_environment(cls):
        if not os.getenv("EVAL_MYSQL_DSN"):
            return None
        return cls(mysql_dsn=os.environ["EVAL_MYSQL_DSN"], redis_url=os.getenv("EVAL_REDIS_URL", ""),
            mysql_server_uuid=os.getenv("EVAL_MYSQL_SERVER_UUID", ""), redis_run_id=os.getenv("EVAL_REDIS_RUN_ID", ""))

    async def open(self):
        await self.connect_readonly()
        try:
            self.lock_connection = await self.pool.acquire()
            async with self.lock_connection.cursor() as cursor:
                await cursor.execute("SELECT GET_LOCK('bookstore_eval_v2',0)")
                if (await cursor.fetchone())[0] != 1:
                    raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "another v2 evaluation owns the isolated fixture")
        except BaseException:
            await self.aclose()
            raise

    async def connect_readonly(self):
        config = _parse_mysql_url(self.mysql_dsn)
        if config["db"] != DATABASE or not self.redis_url or len(self.redis_run_id) != 40:
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "explicit isolated v2 dependency identities are required")
        try:
            UUID(self.mysql_server_uuid)
            self.pool = await asyncmy.create_pool(minsize=1, maxsize=8, autocommit=False, **config)
            rows = await self.query("SELECT DATABASE() AS db,@@server_uuid AS server_uuid,VERSION() AS version")
            row = rows[0]
            if row["db"] != DATABASE or row["server_uuid"] != self.mysql_server_uuid or not row["version"].startswith("8."):
                raise ValueError("MySQL identity mismatch")
            versions = await self.query("SELECT version,dirty FROM schema_migrations")
            if len(versions) != 1 or versions[0]["dirty"] or int(versions[0]["version"]) < 10:
                raise ValueError("clean v10 schema required")
            self.redis = Redis.from_url(self.redis_url)
            info = await self.redis.info("server")
            if info["run_id"] != self.redis_run_id or (await self.redis.config_get("appendonly")).get("appendonly") != "yes":
                raise ValueError("dedicated AOF Redis identity mismatch")
            self.identity = dict(database=DATABASE, mysql_server_uuid=row["server_uuid"], mysql_version=row["version"], redis_run_id=info["run_id"])
        except BaseException:
            await self.aclose()
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "isolated v2 dependency preflight failed") from None

    async def query(self, sql, parameters=()):
        async with self.pool.acquire() as connection:
            try:
                async with connection.cursor(DictCursor) as cursor:
                    await cursor.execute(sql, parameters)
                    return list(await cursor.fetchall())
            finally:
                await connection.rollback()

    async def execute(self, sql, parameters=()):
        async with self.pool.acquire() as connection:
            try:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql, parameters)
                    count = cursor.rowcount
                await connection.commit()
                return count
            except BaseException:
                await connection.rollback()
                raise

    async def seed_book_and_user(self, book_id, *, stock):
        """Only insert/modify the exact synthetic fixture; refuse foreign rows."""
        if self.lock_connection is None:
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "fixture writes require the isolated suite lock")
        book = BOOKS[book_id]
        users = await self.query("SELECT user_id,email,email_verified_at,status FROM user_account WHERE user_id IN (1,2,3)")
        for user in users:
            if (user["email"] != f"v2-u{user['user_id']}@example.test" or user["status"] != "ACTIVE"
                    or (user["email_verified_at"] is not None) != (user["user_id"] != 3)):
                raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "fixture user ID is occupied by unrelated data")
        missing_users = {1,2,3} - {user["user_id"] for user in users}
        rows = await self.query("SELECT isbn,title FROM book WHERE book_id=%s", (book_id,))
        if rows and (rows[0]["isbn"], rows[0]["title"]) != (book["isbn"], book["title"]):
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "fixture book ID is occupied by unrelated data")
        for user_id in sorted(missing_users):
            await self.execute("INSERT INTO user_account (user_id,email,display_name,password_hash,email_verified_at) "
                "VALUES (%s,%s,'v2 synthetic fixture','not-a-login-password',IF(%s,UTC_TIMESTAMP(3),NULL))",
                (user_id,f"v2-u{user_id}@example.test",user_id != 3))
        if not rows:
            await self.execute("INSERT INTO book (book_id,isbn,title,author,description,language_code,price,stock_count,currency,status) "
                "VALUES (%s,%s,%s,%s,'Synthetic acceptance catalog',%s,%s,%s,'CNY',%s)",
                (book_id,book["isbn"],book["title"],book["authors"][0],book["language_code"],book["price"],stock,book["status"]))
            await self.execute("INSERT INTO inventory (book_id,available_stock,reserved_stock,version) VALUES (%s,%s,0,1)", (book_id,stock))
        else:
            await self.execute("UPDATE inventory SET available_stock=%s,reserved_stock=0,version=version+1 WHERE book_id=%s", (stock,book_id))
            await self.execute("UPDATE book SET stock_count=%s,price=%s,status=%s,version=version+1 WHERE book_id=%s",
                (stock,book["price"],book["status"],book_id))

    def child_configuration(self):
        # Sent only through the child's anonymous stdin pipe; never report this.
        return dict(mysql_dsn=self.mysql_dsn, redis_url=self.redis_url,
            mysql_server_uuid=self.mysql_server_uuid, redis_run_id=self.redis_run_id)

    async def run_case(self, case, script, fixture, prompt_loader, *, trace_factory=None):
        if case.case_id.startswith("recovery-"):
            if fixture.calls:
                raise TraceError("EVAL_DATASET_INVALID", "recovery cannot consume fake business tool returns")
            from app.evaluation.recovery_cases import run_recovery_case
            return await run_recovery_case(self, case, script, trace_factory=trace_factory)
        if case.case_id.startswith("order-"):
            from app.evaluation.order_cases import run_order_case
            return await run_order_case(self, case, script, fixture, prompt_loader, trace_factory=trace_factory)
        raise TraceError("EVAL_DATASET_INVALID", "unrecognized real-dependency case")

    async def aclose(self):
        if self.lock_connection is not None:
            try:
                async with self.lock_connection.cursor() as cursor:
                    await cursor.execute("DO RELEASE_LOCK('bookstore_eval_v2')")
            finally:
                self.pool.release(self.lock_connection)
                self.lock_connection = None
        if self.redis is not None:
            await self.redis.aclose()
            self.redis = None
        if self.pool is not None:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None
