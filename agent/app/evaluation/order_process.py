"""In-memory control channel to the isolated production Go RPC test adapter."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

from app.api.auth import AccessTokenVerifier
from app.memory.repository import _parse_mysql_url
from app.tools.clients import GrpcOrderClient
from app.trace.errors import TraceError


class FixtureTokenVerifier(AccessTokenVerifier):
    def __init__(self, public_key, issuer):
        self.public_key, self.issuer = public_key, issuer


class OrderProcess:
    def __init__(self, dependencies):
        self.dependencies = dependencies
        self.process = None
        self.clients = []
        self.tokens = {}
        self.identity = {}

    async def open(self):
        path = Path(os.getenv("EVAL_ORDER_HELPER", ""))
        if not path.is_absolute() or not path.is_file():
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "EVAL_ORDER_HELPER must identify the built v2 Go adapter")
        config = _parse_mysql_url(self.dependencies.mysql_dsn)
        data = dict(host=config["host"],port=config["port"],user=config["user"],password=config["password"],database=config["db"],
            server_uuid=self.dependencies.mysql_server_uuid,redis_url=self.dependencies.redis_url,redis_run_id=self.dependencies.redis_run_id)
        self.process = await asyncio.create_subprocess_exec(str(path), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=262144)
        try:
            self.process.stdin.write((json.dumps(data)+"\n").encode())
            await self.process.stdin.drain()
            raw = await asyncio.wait_for(self._line("V2_BOOT="), timeout=25)
            boot = json.loads(raw)
            if boot["server_uuid"] != self.dependencies.mysql_server_uuid or len(set(boot["connection_ids"])) != 2:
                raise ValueError("independent connection identity mismatch")
            if len(boot["addresses"]) != 2 or any(not address.startswith("127.0.0.1:") for address in boot["addresses"]):
                raise ValueError("test adapter must bind loopback")
            self.tokens = boot["tokens"]
            self.verifier = FixtureTokenVerifier(boot["public_key"], boot["issuer"])
            self.clients = [GrpcOrderClient(address) for address in boot["addresses"]]
            self.identity = dict(mysql_server_uuid=boot["server_uuid"], connection_ids=boot["connection_ids"],
                helper_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), pid=self.process.pid)
        except BaseException:
            await self.aclose()
            raise

    async def _line(self, prefix):
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "isolated order helper terminated")
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded.startswith(prefix):
                return decoded[len(prefix):]

    async def expiry_boundary(self, enabled):
        self.process.stdin.write((json.dumps(dict(command="expiry_boundary",enabled=enabled))+"\n").encode())
        await self.process.stdin.drain()
        if await asyncio.wait_for(self._line("V2_CONTROL="), timeout=5) != "ok":
            raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "clock control failed")

    async def aclose(self):
        for client in self.clients:
            await client.aclose()
        self.clients = []
        self.tokens = {}
        if self.process is not None:
            if self.process.returncode is None:
                try:
                    self.process.stdin.write(b'{"command":"stop"}\n')
                    await self.process.stdin.drain()
                    await asyncio.wait_for(self.process.wait(), timeout=3)
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    self.process.kill()
                    await self.process.wait()
            self.process = None
