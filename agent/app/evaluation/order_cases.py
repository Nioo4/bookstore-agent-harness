"""Five fixed transactional cases: actual Chat/HTTP confirmation/Go RPC/MySQL."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import uuid
from copy import deepcopy

import httpx

from app.agents.execution import AgentDirective
from app.agents.verification import AgentDraft, LLMVerifierReview
from app.api.app import AppDependencies, create_app
from app.api.chat import ChatEvent, UserIntent
from app.api.confirmation import ConfirmationService
from app.evaluation.catalog import BOOKS, ControlledCatalog, ControlledKnowledge
from app.evaluation.order_process import OrderProcess
from app.evaluation.production import ChatExecution, ProductionChatDriver
from app.evaluation.read_cases import ReadScenario, scene_script, scripted_response
from app.evaluation.script_v2 import FakeModelScriptV2
from app.evaluation.types import CaseFixture, EvalCaseV1
from app.harness.plan import DagPlan
from app.trace.errors import TraceError
from app.trace.types import RunStatus as TraceRunStatus, canonical_json


ORDER_IDS = tuple(f"order-{index:03}" for index in range(1,6))
REQUESTS = {"order-001":"预览B1两本，暂不提交", "order-002":"预览B2一本，暂不提交",
    "order-003":"预览B3一本，暂不提交", "order-004":"预览B1一本，暂不提交", "order-005":"预览B1一本，暂不提交"}
connection_slot = contextvars.ContextVar("v2_order_connection", default=0)


def order_assets(case_id):
    if case_id not in ORDER_IDS: raise ValueError("unknown fixed order case")
    rounds = 4 if case_id == "order-004" else 2 if case_id == "order-005" else 1
    responses = []
    book_id = {"order-002":2,"order-003":3}.get(case_id,1)
    quantity = 2 if case_id == "order-001" else 1
    for index in range(1,rounds+1):
        scope = f"turn-{index}"
        node = dict(id="prepare",agent="order",objective=REQUESTS[case_id],depends_on=[],input_refs=["user_request"],expected_output="preview")
        responses.extend([
            scripted_response("intent_extractor",UserIntent,{"required_question_ids":["answer"],"constraints":{}},round_scope=scope),
            scripted_response("supervisor",DagPlan,{"goal":REQUESTS[case_id],"nodes":[node]},round_scope=scope),
            scripted_response("order",AgentDirective,{"tool_calls":[{"name":"prepare_order","arguments":{
                "items":[{"book_id":book_id,"quantity":quantity}],"idempotency_key":f"{case_id}-{index}"}}],"output":{}},scope="prepare",round_scope=scope),
            scripted_response("answer_synthesizer",AgentDraft,{"answer":"已生成订单预览，只有点击结构化确认后才会提交。","addressed_question_ids":["answer"]},round_scope=scope),
            scripted_response("verifier",LLMVerifierReview,{"approved":True,"issues":[]},round_scope=scope),
        ])
    if case_id == "order-001":
        responses.extend(scene_script(ReadScenario("我已确认，查询B1", {"book_id":1}, (),tool="get_book",
            answer="普通聊天不代表结构化确认；订单仍未提交。"),round_scope="chat-only").responses)
    case = EvalCaseV1(case_id=case_id,category="ORDER_CONFIRMATION_CONCURRENCY",tags=["task_expected","critical_transaction"],
        user_request=REQUESTS[case_id],auth={"authenticated":True,"email_verified":True},memory_seed=[],
        fixture_ref=f"fixtures/{case_id}.json",fake_model_ref=f"scripts/{case_id}.json",
        expected=dict(final_status="CANCELLED" if case_id == "order-003" else "COMPLETED",required_tools=["prepare_order"],
            forbidden_tools=[],required_facts=[],required_constraints=[],allowed_error_codes=[]))
    return case, CaseFixture(schema_version="v1",calls=[]), FakeModelScriptV2(schema_version="v2",responses=responses)


class ConnectedOrders:
    def __init__(self, process):
        self.process, self.namespace = process, uuid.uuid4().hex
        self.barrier = None
        self.decide_connections = []

    async def prepare_order(self, request, access_token, conversation_id, timeout):
        # Namespace only fixture idempotency keys, so separate evaluation runs do
        # not reuse old previews. Items/user/conversation pass through unchanged.
        key = hashlib.sha256((self.namespace+request.idempotency_key).encode()).hexdigest()
        return await self.process.clients[0].prepare_order(request.model_copy(update={"idempotency_key":key}),access_token,conversation_id,timeout)

    async def create_order(self, request, access_token, conversation_id, timeout):
        slot = connection_slot.get()
        if self.barrier is not None:
            await self.barrier.wait()
        self.decide_connections.append(self.process.identity["connection_ids"][slot])
        return await self.process.clients[slot].create_order(request,access_token,conversation_id,timeout)

    async def get_order(self, request, access_token, timeout):
        return await self.process.clients[0].get_order(request,access_token,timeout)

    async def list_my_orders(self, request, access_token, timeout):
        return await self.process.clients[0].list_my_orders(request,access_token,timeout)


async def snapshot(dependencies, book_id, conversations):
    orders = await dependencies.query("SELECT order_id,user_id,conversation_id,status,total_amount,currency,idempotency_key FROM customer_order")
    items = await dependencies.query("SELECT * FROM order_item")
    inventory = (await dependencies.query("SELECT available_stock,reserved_stock,version FROM inventory WHERE book_id=%s",(book_id,)))[0]
    outbox = await dependencies.query("SELECT event_id,event_type,aggregate_type,aggregate_id,payload_json FROM outbox_event")
    pending = await dependencies.query("SELECT pending_action_id,user_id,conversation_id,token_hash,items_json,quoted_amount,currency,status,decision,order_id,"
        "(consumed_at IS NOT NULL) AS consumed FROM pending_action WHERE user_id=1")
    pending = [row for row in pending if row["conversation_id"] in conversations]
    for row in pending:
        row["token_hash"] = bytes(row["token_hash"]).hex()
        if isinstance(row["items_json"], str): row["items_json"] = json.loads(row["items_json"])
    for row in outbox:
        if isinstance(row["payload_json"], str): row["payload_json"] = json.loads(row["payload_json"])
    return dict(orders=orders,items=items,inventory=inventory,outbox=outbox,pending=pending)


def new_rows(before, after, table, key):
    old = {row[key] for row in before[table]}
    return [row for row in after[table] if row[key] not in old]


def transaction_failures(before, after, *, book_id, quantity, placed, pending_statuses):
    failures = []
    for table, key in (("orders", "order_id"), ("items", "order_item_id"), ("outbox", "event_id")):
        current = {row[key]: row for row in after[table]}
        if any(current.get(row[key]) != row for row in before[table]):
            failures.append("EXISTING_TRANSACTION_ROWS_CHANGED")
    orders = new_rows(before,after,"orders","order_id")
    items = new_rows(before,after,"items","order_item_id")
    events = new_rows(before,after,"outbox","event_id")
    placed_events = [row for row in events if row["event_type"] == "order.placed.v1"]
    inventory_events = [row for row in events if row["event_type"] == "inventory.stock_changed.v1"]
    if len(orders) != placed or len(items) != placed or len(placed_events) != placed or len(inventory_events) != placed:
        failures.append("TRANSACTION_EFFECT_CARDINALITY_MISMATCH")
    if before["inventory"]["available_stock"]-after["inventory"]["available_stock"] != quantity*placed or after["inventory"]["available_stock"] < 0 or after["inventory"]["reserved_stock"] != before["inventory"]["reserved_stock"]:
        failures.append("INVENTORY_EFFECT_MISMATCH")
    if sorted(row["status"] for row in after["pending"]) != sorted(pending_statuses): failures.append("CONFIRMATION_STATE_MISMATCH")
    book = BOOKS[book_id]
    for order in orders:
        action = next((row for row in after["pending"] if row["order_id"] == order["order_id"]), None)
        if (order["status"] != "PLACED" or order["total_amount"] != book["price"]*quantity or order["currency"] != "CNY"
                or action is None or action["decision"] != "APPROVE" or not action["consumed"]
                or order["conversation_id"] != action["conversation_id"] or order["user_id"] != action["user_id"]
                or order["idempotency_key"] != "order-confirmation:" + action["pending_action_id"]):
            failures.append("ORDER_OR_IDEMPOTENCY_BINDING_MISMATCH")
    for item in items:
        expected = {"book_id":book_id,"isbn_snapshot":book["isbn"],"title_snapshot":book["title"],"unit_price":book["price"],"quantity":quantity,"line_total":book["price"]*quantity}
        if any(item.get(key) != value for key,value in expected.items()) or item["order_id"] not in {row["order_id"] for row in orders}:
            failures.append("ORDER_ITEM_SNAPSHOT_MISMATCH")
    for action in after["pending"]:
        if action["status"] in {"REJECTED","EXPIRED"} and (not action["consumed"] or action["order_id"] is not None): failures.append("FAILED_ACTION_HAS_TRANSACTION_RESULT")
        if action["status"] == "PENDING" and (action["consumed"] or action["decision"] is not None): failures.append("PREVIEW_CONSUMED_EARLY")
    for event in placed_events:
        if event["aggregate_type"] != "order" or event["aggregate_id"] not in {str(row["order_id"]) for row in orders}: failures.append("ORDER_OUTBOX_BINDING_MISMATCH")
    for event in inventory_events:
        payload = event["payload_json"]
        if (event["aggregate_type"] != "book" or event["aggregate_id"] != str(book_id)
                or payload.get("book_id") != book_id or payload.get("available_stock") != after["inventory"]["available_stock"]
                or payload.get("inventory_version") != after["inventory"]["version"]): failures.append("INVENTORY_OUTBOX_MISMATCH")
    return failures


def compact_checks(checks):
    """Retain every changed row, plus counts/digests of the full SQL snapshots.

    The oracle still compares full snapshots. Unrelated historical rows are not
    copied into every report and no test database cleanup is needed to fit it.
    """
    compact = []
    for check in checks:
        result = {"operation": check["operation"]}
        for side in ("before", "after"):
            source = check[side]
            result[side] = {"inventory": source["inventory"], "pending": source["pending"]}
            for table, key in (("orders", "order_id"), ("items", "order_item_id"), ("outbox", "event_id")):
                other = {row[key]: row for row in check["after" if side == "before" else "before"][table]}
                ordered = sorted(source[table], key=lambda row: str(row[key]))
                result[side][table] = {"count": len(ordered),
                    "snapshot_sha256": hashlib.sha256(canonical_json(ordered).encode("utf-8")).hexdigest(),
                    "affected_rows": [row for row in ordered if other.get(row[key]) != row]}
        compact.append(result)
    return compact


async def run_order_case(dependencies, case, script, fixture, prompt_loader, *, trace_factory=None):
    if (case.case_id not in ORDER_IDS or case.user_request != REQUESTS[case.case_id] or fixture.calls
            or case.memory_seed or not case.auth.authenticated or not case.auth.email_verified):
        raise TraceError("EVAL_DATASET_INVALID", "order input does not match fixed authenticated v2 scenario")
    book_id = {"order-002":2,"order-003":3}.get(case.case_id,1)
    quantity = 2 if case.case_id == "order-001" else 1
    await dependencies.seed_book_and_user(book_id,stock=1 if case.case_id == "order-005" else 5)
    process = OrderProcess(dependencies)
    await process.open()
    try:
        connected = ConnectedOrders(process)
        driver = ProductionChatDriver(script,catalog=ControlledCatalog(),orders=connected,knowledge=ControlledKnowledge([]),
            prompt_loader=prompt_loader,trace_factory=trace_factory)
        chat = driver.service
        # EVALUATION traces have no user owner by contract. Bind their Recorder
        # around the real HTTP invocation, not through the LIVE-owner attachment
        # path. JWT/conversation/action authorization is still fully production.
        confirmation = ConfirmationService(driver.tools,driver.conversations,chat.checkpoint_store,chat.langgraph_checkpointer,chat.limits)
        app = create_app(AppDependencies(model_provider=chat.provider,agents=chat.agents,tools=driver.tools,
            token_verifier=process.verifier,confirmation_service=confirmation,conversation_store=driver.conversations))
        failures, rounds, checks, responses, conversations = [], [], [], [], []
        tokens = []  # Response-delivered raw confirmation values; never enter evidence.
        preview_trace_ids = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://isolated-agent") as http:
            async def decide(index, decision="APPROVE", *, user=1, slot=0, conversation=None):
                binding = connection_slot.set(slot)
                recorder = await trace_factory.attach(preview_trace_ids[index]) if trace_factory else None
                recorder_token = recorder.bind() if recorder else None
                try:
                    response = await http.post(f"/api/chat/conversations/{conversation or conversations[index]}/confirmations",
                        headers={"Authorization":"Bearer "+process.tokens[str(user)]},json={"confirmation_token":tokens[index],"decision":decision})
                    value = dict(status=response.status_code,body=response.json())
                    responses.append(value)
                    if recorder and response.status_code == 200 and not value["body"].get("replayed"):
                        await recorder.record_confirmation_decision(decision, value["body"]["status"])
                    return value
                finally:
                    if recorder and recorder_token is not None:
                        recorder.reset(recorder_token)
                    connection_slot.reset(binding)

            def require(response, status, code=None):
                if response["status"] != status or (code and response["body"].get("error",{}).get("code") != code):
                    failures.append("CONFIRMATION_HTTP_RESULT_MISMATCH")

            async def preview(index):
                conversations.append("v2-"+str(uuid.uuid4()))
                before = await snapshot(dependencies,book_id,conversations)
                result = await driver.round(case.user_request,auth=case.auth,scope=f"turn-{index+1}",
                    conversation_id=conversations[-1],access_token=process.tokens["1"])
                rounds.append(result)
                if trace_factory:
                    preview_trace_ids.append(trace_factory.run_ids[-1])
                previews = [event.data for event in result.events if event.event == "confirmation.required" and event.data.get("action_type") == "CREATE_ORDER"]
                if len(previews) != 1:
                    raise TraceError("EVAL_DEPENDENCY_UNAVAILABLE", "production flow did not deliver one order preview")
                token = previews[0]["confirmation_token"]
                tokens.append(token)
                after = await snapshot(dependencies,book_id,conversations)
                failures.extend(transaction_failures(before,after,book_id=book_id,quantity=quantity,placed=0,
                    pending_statuses=[row["status"] for row in before["pending"]]+["PENDING"]))
                action = next((row for row in after["pending"] if row["conversation_id"] == conversations[-1]), None)
                if (action is None or action["token_hash"] != hashlib.sha256(token.encode()).hexdigest()
                        or action["quoted_amount"] != BOOKS[book_id]["price"]*quantity
                        or previews[0]["total_amount"] != action["quoted_amount"]): failures.append("PREVIEW_BINDING_OR_QUOTE_MISMATCH")
                checks.append(dict(before=before,after=after,operation="preview"))
                return after

            base = await preview(0)
            if case.case_id == "order-001":
                rounds.append(await driver.round("我已确认，查询B1",auth=case.auth,scope="chat-only",conversation_id=conversations[0],access_token=process.tokens["1"]))
                before_click = await snapshot(dependencies,book_id,conversations)
                failures.extend(transaction_failures(base,before_click,book_id=book_id,quantity=quantity,placed=0,pending_statuses=["PENDING"]))
                checks.append(dict(before=base,after=before_click,operation="ordinary_chat"))
                require(await decide(0),200)
            elif case.case_id == "order-002":
                connected.barrier = asyncio.Barrier(2)
                pair = await asyncio.wait_for(asyncio.gather(decide(0,slot=0),decide(0,slot=1)),timeout=20)
                connected.barrier = None
                for response in pair: require(response,200)
                replay = await decide(0)
                require(replay,200)
                if (sorted(response["body"].get("replayed") for response in pair) != [False,True]
                        or not replay["body"].get("replayed") or len({response["body"].get("order",{}).get("order_id") for response in [*pair,replay]}) != 1
                        or set(connected.decide_connections[:2]) != set(process.identity["connection_ids"])): failures.append("CONCURRENT_REPLAY_MISMATCH")
                require(await decide(0,"REJECT"),409,"CONFIRMATION_ALREADY_DECIDED")
            elif case.case_id == "order-003":
                other = "v2-"+str(uuid.uuid4())
                await driver.conversations.bind(other,2)
                require(await decide(0,user=2,conversation=other),404,"CONFIRMATION_NOT_FOUND")
                require(await decide(0,user=3),403,"EMAIL_NOT_VERIFIED")
                require(await decide(0,"REJECT"),200)
                replay = await decide(0,"REJECT")
                require(replay,200)
                if not replay["body"].get("replayed") or replay["body"].get("status") != "CANCELLED": failures.append("REJECT_REPLAY_MISMATCH")
                require(await decide(0),409,"CONFIRMATION_ALREADY_DECIDED")
            elif case.case_id == "order-004":
                for index, code in enumerate(("PRICE_CHANGED","BOOK_INACTIVE","INVENTORY_INSUFFICIENT","CONFIRMATION_EXPIRED")):
                    if index:
                        await dependencies.seed_book_and_user(book_id,stock=5)
                        await preview(index)
                    if index == 0: await dependencies.execute("UPDATE book SET price=price+100 WHERE book_id=1")
                    if index == 1: await dependencies.execute("UPDATE book SET status='INACTIVE' WHERE book_id=1")
                    if index == 2: await dependencies.execute("UPDATE inventory SET available_stock=0 WHERE book_id=1")
                    if index == 3: await process.expiry_boundary(True)
                    before = await snapshot(dependencies,book_id,conversations)
                    require(await decide(index),410 if index == 3 else 409,code)
                    if index == 3: await process.expiry_boundary(False)
                    after = await snapshot(dependencies,book_id,conversations)
                    failures.extend(transaction_failures(before,after,book_id=book_id,quantity=quantity,placed=0,pending_statuses=["EXPIRED"]*(index+1)))
                    checks.append(dict(before=before,after=after,operation=code))
                    require(await decide(index),410,"CONFIRMATION_EXPIRED")
            else:
                await preview(1)
                base = await snapshot(dependencies,book_id,conversations)
                connected.barrier = asyncio.Barrier(2)
                pair = await asyncio.wait_for(asyncio.gather(decide(0,slot=0),decide(1,slot=1)),timeout=20)
                connected.barrier = None
                if sorted(response["status"] for response in pair) != [200,409]: failures.append("OVERSOLD_OR_BOTH_FAILED")
                loser = next((response for response in pair if response["status"] == 409), None)
                if loser: require(loser,409,"INVENTORY_INSUFFICIENT")
                if set(connected.decide_connections) != set(process.identity["connection_ids"]): failures.append("INDEPENDENT_CONNECTIONS_NOT_EXECUTED")
            final = await snapshot(dependencies,book_id,conversations)
            if case.case_id != "order-004":
                failures.extend(transaction_failures(base,final,book_id=book_id,quantity=quantity,
                    placed=0 if case.case_id == "order-003" else 1,
                    pending_statuses=["REJECTED"] if case.case_id == "order-003" else ["APPROVED","EXPIRED"] if case.case_id == "order-005" else ["APPROVED"]))
                checks.append(dict(before=base,after=final,operation="final"))
            driver.finish()
            if trace_factory:
                for conversation_id, trace_id in zip(conversations, preview_trace_ids, strict=True):
                    action = next(row for row in final["pending"] if row["conversation_id"] == conversation_id)
                    recorder = await trace_factory.attach(trace_id)
                    terminal = {"APPROVED": TraceRunStatus.COMPLETED, "REJECTED": TraceRunStatus.CANCELLED,
                        "EXPIRED": TraceRunStatus.FAILED}.get(action["status"])
                    if terminal is not None:
                        await recorder.finish_run(status=terminal, final_answer=recorder.run.final_answer,
                            dag=recorder.run.dag, replayable=False, replay_block_reason="EVALUATION_RUN",
                            error_code="CONFIRMATION_EXPIRED" if action["status"] == "EXPIRED" else None)
            states = {row["status"] for row in final["pending"]}
            status = "CANCELLED" if states == {"REJECTED"} else "COMPLETED" if states and states <= {"APPROVED","EXPIRED"} else "FAILED"
            # All per-attempt HTTP terminal states and DB states remain in evidence;
            # the last envelope denotes completion of the fixed scenario sequence.
            result = ChatExecution(events=[ChatEvent(event="message.completed",data={"content":"实际交易场景已执行","scenario_status":status})],
                tools=deepcopy(driver.observations),model_calls=list(driver.queue.requests),tool_registry_version=driver.tools.version,
                prior_rounds=rounds,dependency_evidence=dict(identity=dependencies.identity,order_process=process.identity,
                    checks=compact_checks(checks),http_responses=responses,decision_connections=connected.decide_connections,
                    assertion_source="actual SQL rows/unique keys/Outbox and production HTTP responses"),terminal_status=status)
            return result, sorted(set(failures))
    finally:
        await process.aclose()
