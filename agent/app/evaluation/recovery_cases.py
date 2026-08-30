"""Actual process-death recovery, reusing the production integration-test boundary."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta

from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from app.api.chat import ChatEvent
from app.evaluation.build import runner_build_version
from app.evaluation.catalog import BOOKS
from app.evaluation.oracle import recovery_failures
from app.evaluation.production import ChatExecution, ToolObservations
from app.evaluation.recovery_process import CRASH_EXITS
from app.evaluation.script_v2 import FakeModelScriptV2
from app.evaluation.types import CaseFixture, EvalCaseV1
from app.models.configuration import AGENT_ROOT
from app.tasks.graph import InventoryWatchGraph
from app.tasks.mysql_repository import AsyncMySQLTaskRepository
from app.tasks.types import AgentTask, CheckpointStatus, TaskStatus
from app.tasks.worker import INVENTORY_EVENT_TYPE
from app.tools.business import build_business_tool_registry
from app.trace.errors import TraceError
from app.trace.types import RunStatus, StepStatus, StepType


def recovery_assets(case_id):
    if case_id not in CRASH_EXITS:
        raise ValueError("unknown recovery case")
    case = EvalCaseV1(case_id=case_id, category="PERSISTENT_TASK_RECOVERY", tags=["task_expected"],
        user_request="监听缺货B5，补货达到1本后发站内通知", auth={"authenticated":True,"email_verified":True},
        memory_seed=[], fixture_ref=f"fixtures/{case_id}.json", fake_model_ref=f"scripts/{case_id}.json",
        expected=dict(final_status="COMPLETED", required_tools=[], forbidden_tools=["prepare_order","create_order"],
            required_facts=[], required_constraints=[], allowed_error_codes=[]))
    return case, CaseFixture(schema_version="v1", calls=[]), FakeModelScriptV2(schema_version="v2", responses=[])


async def run_process(configuration):
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "app.evaluation.recovery_process",
        cwd=str(AGENT_ROOT), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, _ = await asyncio.wait_for(process.communicate(json.dumps(configuration).encode()), timeout=45)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    observations = [json.loads(line.partition("=")[2]) for line in output.decode("utf-8", errors="replace").splitlines()
        if line.startswith("V2_OBSERVATION=")]
    return dict(pid=process.pid, exit_code=process.returncode, observations=observations)


async def business_snapshot(dependencies):
    values = {}
    for key, sql in {
        "orders":"SELECT COUNT(*) AS n FROM customer_order WHERE user_id=1",
        "items":"SELECT COUNT(*) AS n FROM order_item i JOIN customer_order o ON i.order_id=o.order_id WHERE o.user_id=1",
        "previews":"SELECT COUNT(*) AS n FROM pending_action WHERE user_id=1",
        "outbox":"SELECT COUNT(*) AS n FROM outbox_event",
        "verification_tokens":"SELECT COUNT(*) AS n FROM email_verification_token WHERE user_id=1",
    }.items():
        values[key] = int((await dependencies.query(sql))[0]["n"])
    values["inventory"] = (await dependencies.query("SELECT available_stock,reserved_stock,version FROM inventory WHERE book_id=5"))[0]
    return values


async def recovery_snapshot(dependencies, graph, task, stream, group, event_id):
    rows = await dependencies.query("SELECT task_id,run_id,status,trigger_event_id,checkpoint_status,last_seen_available_stock,"
        "last_seen_inventory_version FROM agent_task WHERE task_id=%s", (task.task_id,))
    consumption = await dependencies.query("SELECT event_id,stream_entry_id,status,attempt_count,book_id,inventory_version FROM inventory_event_consumption WHERE event_id=%s", (event_id,))
    notifications = await dependencies.query("SELECT notification_id,task_id,user_id,notification_type,title,body,payload_json FROM notification WHERE task_id=%s", (task.task_id,))
    for notification in notifications:
        if isinstance(notification["payload_json"], str):
            notification["payload_json"] = json.loads(notification["payload_json"])
    checkpoint = await graph._compiled.aget_state(graph._config(task.task_id))
    return dict(task=rows[0], consumption=consumption, notifications=notifications,
        checkpoint=dict(values=dict(checkpoint.values), next=list(checkpoint.next), thread_id=checkpoint.config["configurable"]["thread_id"]),
        pending=int((await dependencies.redis.xpending(stream, group))["pending"]))


async def run_recovery_case(dependencies, case, script, *, trace_factory=None):
    if case.case_id not in CRASH_EXITS or script.responses or not case.auth.authenticated or not case.auth.email_verified or case.memory_seed:
        raise TraceError("EVAL_DATASET_INVALID", "recovery requires the fixed no-LLM authenticated scenario")
    if case.user_request != "监听缺货B5，补货达到1本后发站内通知":
        raise TraceError("EVAL_DATASET_INVALID", "recovery request does not match the fixed worker scenario")
    await dependencies.seed_book_and_user(5, stock=0)
    inventory = (await dependencies.query("SELECT version FROM inventory WHERE book_id=5"))[0]
    now = datetime.now(UTC)
    task = AgentTask(task_id=str(uuid.uuid4()), user_id=1, conversation_id="v2-" + str(uuid.uuid4()),
        run_id=str(uuid.uuid4()), status=TaskStatus.ACTIVE, book_id=5, book_title=BOOKS[5]["title"],
        desired_quantity=1, last_seen_inventory_version=inventory["version"], last_seen_available_stock=0,
        create_idempotency_key="v2-" + str(uuid.uuid4()), request_hash=hashlib.sha256(case.user_request.encode()).digest(),
        checkpoint_status=CheckpointStatus.PENDING, expires_at=now + timedelta(days=30), created_at=now, updated_at=now)
    repository = AsyncMySQLTaskRepository(dependencies.pool)
    task, replayed = await repository.create(task)
    if replayed:
        raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "fresh recovery fixture unexpectedly replayed")
    saver = AsyncRedisSaver(redis_client=dependencies.redis)
    await saver.asetup()
    graph = InventoryWatchGraph(saver)
    await graph.ensure_waiting(task)
    task = await repository.mark_checkpoint(task.task_id, CheckpointStatus.READY)
    event_id, stream, group = str(uuid.uuid4()), "v2.inventory." + task.task_id, "v2-inventory-workers"
    payload = dict(event_id=event_id, event_type=INVENTORY_EVENT_TYPE, occurred_at=now.isoformat(),
        book_id=5, available_stock=2, inventory_version=inventory["version"] + 1)
    await dependencies.execute("UPDATE inventory SET available_stock=2,version=version+1 WHERE book_id=5")
    await dependencies.execute("UPDATE book SET stock_count=2 WHERE book_id=5")
    await dependencies.redis.xgroup_create(stream, group, id="0-0", mkstream=True)
    fields = dict(event_id=event_id, event_type=INVENTORY_EVENT_TYPE, payload_json=json.dumps(payload))
    entry_id = (await dependencies.redis.xadd(stream, fields)).decode()
    before = await business_snapshot(dependencies)
    recorder = await trace_factory.start(user_id=1, conversation_id=task.conversation_id, user_request=case.user_request) if trace_factory else None
    step_id = await recorder.start_step(node_id="evaluation.real_recovery", step_type=StepType.SYSTEM,
        input_value={"case_id":case.case_id, "task_id":task.task_id, "worker_run_id":task.run_id}) if recorder else None
    configuration = dict(case_id=case.case_id, build=runner_build_version(), stream=stream, group=group,
        dependencies=dependencies.child_configuration())
    crash = await run_process({**configuration, "crash":True})
    interrupted = await recovery_snapshot(dependencies, graph, task, stream, group, event_id)
    # Duplicate the same actual event; the first delivery stays in the crashed PEL.
    await dependencies.redis.xadd(stream, fields)
    restarted = await run_process({**configuration, "crash":False})
    after = await recovery_snapshot(dependencies, graph, task, stream, group, event_id)
    unchanged = await business_snapshot(dependencies)
    failures = recovery_assertions(case.case_id, task, payload, entry_id, crash, restarted, interrupted, after, before, unchanged)
    evidence = dict(identity=dependencies.identity, before=before, interrupted=interrupted, after=after,
        business_after=unchanged, crash_process=crash, recovery_process=restarted,
        deliveries=int(await dependencies.redis.xlen(stream)), assertions="actual MySQL rows + Redis PEL/checkpoint + process exit/attempt hooks")
    if evidence["deliveries"] < 2:
        failures.append("DUPLICATE_DELIVERY_NOT_EXECUTED")
    if recorder:
        await recorder.finish_step(step_id, status=StepStatus.FAILED if failures else StepStatus.COMPLETED,
            output_value={"case_id":case.case_id,"failure_codes":failures,"worker_run_id":task.run_id,"task_id":task.task_id})
        await recorder.finish_run(status=RunStatus.FAILED if failures else RunStatus.COMPLETED,
            final_answer="实际进程恢复断言完成", dag=None, replayable=False, replay_block_reason="EVALUATION_RUN")
    result = ChatExecution(events=[ChatEvent(event="message.completed" if after["task"]["status"] == "COMPLETED" else "run.failed",
            data={"content":"实际Worker恢复流程已执行", "task_status":after["task"]["status"]})],
        tools=ToolObservations(), model_calls=[], tool_registry_version=build_business_tool_registry(object(),object(),object()).version,
        dependency_evidence=evidence)
    return result, sorted(set(failures))


def recovery_assertions(case_id, task, payload, entry_id, crash, restarted, interrupted, after, before, unchanged):
    failures = recovery_failures(original_thread=task.task_id, recovered_thread=after["checkpoint"]["thread_id"],
        original_run=task.run_id, recovered_run=after["task"]["run_id"], status=after["task"]["status"],
        notifications=len(after["notifications"]), pending=after["pending"])
    if crash["exit_code"] != CRASH_EXITS[case_id] or restarted["exit_code"] != 0 or crash["pid"] == restarted["pid"]:
        failures.append("ACTUAL_PROCESS_RESTART_NOT_PROVEN")
    observations = crash["observations"] + restarted["observations"]
    if any(item["event"] == "forbidden_call" for item in observations):
        failures.append("RECOVERY_FORBIDDEN_DEPENDENCY_CALLED")
    if not any(item["event"] == "crash_boundary" and item["case_id"] == case_id for item in crash["observations"]):
        failures.append("CRASH_BOUNDARY_NOT_REACHED")
    if before != unchanged or unchanged["inventory"]["available_stock"] != 2:
        failures.append("RECOVERY_UNEXPECTED_BUSINESS_EFFECT")
    expected_status = "TRIGGERED" if case_id in {"recovery-001","recovery-002"} else "COMPLETED"
    expected_notifications = 0 if expected_status == "TRIGGERED" else 1
    if (interrupted["task"]["status"] != expected_status or len(interrupted["notifications"]) != expected_notifications
            or interrupted["pending"] != 1 or bool(interrupted["checkpoint"]["next"]) != (case_id == "recovery-001")):
        failures.append("PERSISTENT_CRASH_BOUNDARY_MISMATCH")
    if (not after["consumption"] or after["consumption"][0]["status"] != "COMPLETED"
            or after["consumption"][0]["stream_entry_id"] != entry_id or after["task"]["trigger_event_id"] != payload["event_id"]
            or after["consumption"][0]["event_id"] != payload["event_id"] or after["consumption"][0]["book_id"] != 5
            or after["consumption"][0]["inventory_version"] != payload["inventory_version"]
            or after["task"]["last_seen_inventory_version"] != payload["inventory_version"]
            or after["task"]["last_seen_available_stock"] != 2):
        failures.append("RECOVERY_CONSUMPTION_MISMATCH")
    state = after["checkpoint"]["values"]
    event = state.get("terminal_event") or {}
    if (after["checkpoint"]["next"] or state.get("run_id") != task.run_id or state.get("task_id") != task.task_id
            or state.get("status") != "COMPLETED" or state.get("terminal_event", {}).get("event_id") != payload["event_id"]):
        failures.append("RECOVERY_CHECKPOINT_MISMATCH")
    if set(event) != {"kind","event_id","book_id","available_stock","inventory_version"} or event.get("kind") != "InventoryStockEvent" or event.get("book_id") != 5 or event.get("available_stock") != 2 or event.get("inventory_version") != payload["inventory_version"]:
        failures.append("RECOVERY_CHECKPOINT_PROJECTION_MISMATCH")
    for notification in after["notifications"]:
        value = notification["payload_json"]
        if (notification["user_id"] != 1 or notification["task_id"] != task.task_id
                or notification["notification_type"] != "INVENTORY_AVAILABLE"
                or set(value) != {"book_id","book_title_snapshot","desired_quantity","available_stock","inventory_version","triggered_at"}
                or value.get("book_id") != 5 or value.get("book_title_snapshot") != task.book_title
                or value.get("desired_quantity") != 1 or value.get("available_stock") != 2
                or value.get("inventory_version") != payload["inventory_version"]
                or notification["title"] != "补货提醒"
                or notification["body"] != f"《{task.book_title}》在触发时库存已达到你监听的数量（触发时库存快照：2）。"):
            failures.append("RECOVERY_NOTIFICATION_PROJECTION_MISMATCH")
    return failures
