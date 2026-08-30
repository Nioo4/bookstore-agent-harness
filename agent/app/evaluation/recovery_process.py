"""Test-only subprocess: fixed worker boundaries, never a production startup mode."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from app.evaluation.build import runner_build_version
from app.evaluation.real_dependencies import RealEvalDependencies
from app.tasks.graph import InventoryWatchGraph
from app.tasks.mysql_repository import AsyncMySQLTaskRepository
from app.tasks.worker import InventoryEventWorker


CRASH_EXITS = {f"recovery-{number:03}":90 + number for number in range(1,5)}


def observation(event, **values):
    print("V2_OBSERVATION=" + json.dumps(dict(event=event, **values), ensure_ascii=True), flush=True)


def install_forbidden_call_probes():
    """Prohibit network model/mail/order calls and report attempts before crashing."""
    from app.models.gateway import GatewayHTTP
    from app.tools.clients import GrpcOrderClient
    import smtplib

    def deny(kind):
        async def blocked(*args, **kwargs):
            observation("forbidden_call", kind=kind)
            raise RuntimeError("recovery graph attempted a forbidden dependency")
        return blocked

    GatewayHTTP.post = deny("model_gateway")
    GrpcOrderClient.prepare_order = deny("order_preview")
    GrpcOrderClient.create_order = deny("order_commit")

    def no_mail(*args, **kwargs):
        observation("forbidden_call", kind="smtp")
        raise RuntimeError("recovery graph attempted email")
    smtplib.SMTP.sendmail = no_mail


async def run(configuration):
    case_id = configuration["case_id"]
    if case_id not in CRASH_EXITS or configuration["build"] != runner_build_version():
        raise ValueError("unrecognized recovery case or changed runtime build")
    dependencies = RealEvalDependencies(**configuration["dependencies"])
    await dependencies.connect_readonly()
    try:
        install_forbidden_call_probes()
        crash = bool(configuration["crash"])

        def exit_at(boundary):
            if crash and case_id == boundary:
                observation("crash_boundary", case_id=case_id, pid=os.getpid())
                os._exit(CRASH_EXITS[case_id])

        class CrashGraph(InventoryWatchGraph):
            async def finish(self, task, event):
                exit_at("recovery-001")
                await super().finish(task, event)
                exit_at("recovery-002")

        class CrashRepository(AsyncMySQLTaskRepository):
            async def finalize_triggered_task(self, task_id, now):
                task = await super().finalize_triggered_task(task_id, now)
                exit_at("recovery-003")
                return task

        redis = dependencies.redis
        original_ack = redis.xack

        async def ack(*args, **kwargs):
            exit_at("recovery-004")
            count = await original_ack(*args, **kwargs)
            observation("ack", count=count)
            return count

        redis.xack = ack
        saver = AsyncRedisSaver(redis_client=redis)
        await saver.asetup()
        worker = InventoryEventWorker(CrashRepository(dependencies.pool), CrashGraph(saver), redis,
            stream=configuration["stream"], group=configuration["group"], consumer=f"v2-{os.getpid()}", claim_idle_ms=0)
        observation("process_started", pid=os.getpid(), build=runner_build_version())
        if crash:
            messages = await redis.xreadgroup(worker.group, worker.consumer, {worker.stream:">"}, count=1)
            if not messages:
                raise ValueError("expected an actual stream delivery")
            entry_id, fields = messages[0][1][0]
            await worker.process_message(entry_id.decode(), fields)
            raise ValueError("required process crash boundary was not reached")
        # Exercise the production XAUTOCLAIM path, which then resumes triggered
        # tasks through process_message -> _recover_event_tasks -> graph.finish.
        await worker._consume_claimed()
        messages = await redis.xreadgroup(worker.group, worker.consumer, {worker.stream:">"}, count=20)
        for _, entries in messages:
            for entry_id, fields in entries:
                await worker.process_message(entry_id.decode(), fields)
        observation("process_completed", pid=os.getpid())
    finally:
        await dependencies.aclose()


def main():
    try:
        asyncio.run(run(json.loads(sys.stdin.buffer.read())))
    except Exception:
        # Exceptions/DSNs/model requests never enter subprocess evidence.
        observation("process_error")
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
