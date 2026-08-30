"""Fixed v2 read scenarios. Scripted responses are data; assertions use actual output."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.execution import AgentDirective
from app.agents.verification import AgentDraft, LLMVerifierReview
from app.api.chat import UserIntent
from app.evaluation.catalog import BOOKS, ControlledCatalog, ControlledKnowledge
from app.evaluation.oracle import RecommendationRequirements, recommendation_failures, policy_answer_failures
from app.evaluation.production import MODEL, NoBusinessWrites, ProductionChatDriver
from app.evaluation.script_v2 import FakeModelScriptV2
from app.evaluation.types import EvalCaseV1
from app.harness.plan import DagPlan
from app.memory.service import MemoryService
from app.memory.types import MemoryCandidate
from app.evaluation.memory_store import ComponentMemoryRepository
from app.models.provider import ModelResponse, ModelUsage
from app.trace.types import canonical_json, sha256_json


@dataclass(frozen=True)
class ReadScenario:
    request: str
    arguments: dict[str, Any]
    ids: tuple[int, ...]
    constraints: dict[str, Any] = field(default_factory=dict)
    tool: str = "search_books"
    answer: str = "以下书籍符合本次查询，价格和库存以目录证据为准。"
    total_budget: int | None = None


READ_SCENARIOS = {
    "query-001": ReadScenario("查询ISBN 9780000000019", {"isbn": "9780000000019"}, (1,), tool="get_book"),
    "query-002": ReadScenario("查询书籍B2的详情（实际ID为2）", {"book_id": 2}, (2,), tool="get_book"),
    "query-003": ReadScenario("搜索题名分布式入门", {"q": "分布式入门"}, (1, 9)),
    "query-004": ReadScenario("列出周乙的有货中文书", {"author": "周乙", "language": "zh-CN", "in_stock": True}, (2, 8, 9)),
    "query-005": ReadScenario("找database分类的书", {"category": "database"}, (9,)),
    "query-006": ReadScenario("查不存在的ISBN 9780000000101", {"q": "9780000000101"}, (), answer="未找到该ISBN对应的图书，无法提供价格或库存。"),
    "query-007": ReadScenario("查询B6并购买", {"book_id": 6}, (), tool="get_book", answer="B6已下架，当前不可购买；不会创建订单或订单预览。"),
    "query-008": ReadScenario("支持退货或物流跟踪吗？", {"query": "退货 物流跟踪"}, (), tool="search_policy"),
    "recommendation-001": ReadScenario("推荐两本中文分布式书，每本不超过40元且有货", {"category":"distributed", "language":"zh-CN", "max_price":4000, "in_stock":True}, (1,2), {"max_price":4000,"language":"zh-CN","categories":["distributed"],"in_stock":True}),
    "recommendation-002": ReadScenario("买三本不同的中文分布式书，总预算100元，都要有货", {"category":"distributed", "language":"zh-CN", "in_stock":True,"sort":"price_asc"}, (), {"total_budget":10000,"language":"zh-CN","categories":["distributed"],"in_stock":True}, answer="没有满足三本不同书的组合：最便宜三本合计11500分，超过10000分总预算。", total_budget=10000),
    "recommendation-003": ReadScenario("推荐林甲写的两本中文分布式书，每本≤50元，有货", {"category":"distributed","author":"林甲","language":"zh-CN","max_price":5000,"in_stock":True}, (1,3), {"max_price":5000,"language":"zh-CN","authors":["林甲"],"categories":["distributed"],"in_stock":True}),
    "recommendation-004": ReadScenario("推荐英文分布式书，每本≤40元且有货", {"category":"distributed","language":"en","max_price":4000,"in_stock":True}, (4,), {"max_price":4000,"language":"en","categories":["distributed"],"in_stock":True}),
    "recommendation-005": ReadScenario("推荐中文分布式书，不要林甲的，单本≤50元且有货", {"category":"distributed","language":"zh-CN","max_price":5000,"in_stock":True}, (2,), {"max_price":5000,"language":"zh-CN","excluded_authors":["林甲"],"categories":["distributed"],"in_stock":True}),
    "recommendation-006": ReadScenario("只推荐Java分类中文有货书，单本≤30元", {"category":"java","language":"zh-CN","max_price":3000,"in_stock":True}, (8,), {"max_price":3000,"language":"zh-CN","categories":["java"],"in_stock":True}),
    "recommendation-007": ReadScenario("推荐中文分布式书，单本≤45元且有货", {"category":"distributed","language":"zh-CN","max_price":4500,"in_stock":True}, (1,2,9), {"max_price":4500,"language":"zh-CN","categories":["distributed"],"in_stock":True}),
    "recommendation-008": ReadScenario("推荐两本中文分布式书，每本≤60元；缺货的也可以，但要标出来", {"category":"distributed","language":"zh-CN","max_price":6000}, (1,5), {"max_price":6000,"language":"zh-CN","categories":["distributed"]}),
    "recommendation-009": ReadScenario("推荐中文分布式书，单本≤10元且有货", {"category":"distributed","language":"zh-CN","max_price":1000,"in_stock":True}, (), {"max_price":1000,"language":"zh-CN","categories":["distributed"],"in_stock":True}, answer="没有满足预算且有货的ACTIVE图书，不推荐下架图书或虚构折扣。"),
    "recommendation-010": ReadScenario("这次只要中文分布式书，每本≤40元且有货", {"category":"distributed","language":"zh-CN","max_price":4000,"in_stock":True}, (1,2), {"max_price":4000,"language":"zh-CN","categories":["distributed"],"in_stock":True}, answer="本轮明确要求中文，优先于历史英文偏好；稳定偏好保持不变。"),
}


def scripted_response(caller, schema, value, *, scope=None, round_scope="turn-1", ordinal=1):
    return dict(caller=caller, call_scope=f"{round_scope}/{scope or caller}", ordinal=ordinal,
        response_schema_sha256=sha256_json(schema.model_json_schema()).hex(), outcome="RESPONSE",
        response=ModelResponse(content=canonical_json(value), model=MODEL,
            usage=ModelUsage(prompt_tokens=10, completion_tokens=5)).model_dump(mode="json"), error_code=None)


def read_script(case_id, policy_chunks=()):
    """Asset authoring only. Runtime reads the hashed script, never this builder."""
    return scene_script(READ_SCENARIOS[case_id], recommendation=case_id.startswith("recommendation-"), policy_chunks=policy_chunks)


def scene_script(scene, *, recommendation=False, policy_chunks=(), round_scope="turn-1"):
    agent = "knowledge" if scene.tool == "search_policy" else "catalog"
    node = dict(id=f"{agent}_read", agent=agent, objective=scene.request, depends_on=[], input_refs=["user_request"], expected_output="candidates")
    nodes = [node]
    if recommendation:
        nodes.append(dict(id="recommend_books", agent="recommendation", objective=scene.request, depends_on=[node["id"]], input_refs=["candidates","user_request"], expected_output="draft"))
    draft = AgentDraft(answer=scene.answer, addressed_question_ids=["answer"], recommendations=[dict(
        book_id=book_id, price=BOOKS[book_id]["price"], currency="CNY", available_stock=BOOKS[book_id]["available_stock"],
        reason="缺货，不能下单。" if BOOKS[book_id]["available_stock"] == 0 else "目录中的语言、分类及价格符合本轮条件。",
        evidence_ids=[f"book:{book_id}:v1"]) for book_id in scene.ids])
    if policy_chunks:
        draft.answer = "v1不支持退货或物流跟踪。" + " ".join(f"[《{c['document_title']}》v{c['version']}，{c['section']}]" for c in policy_chunks)
        draft.policy_claims = [dict(claim=c["content"], source=c["source_path"], section=c["section"], version=c["version"], evidence_ids=["policy:"+c["content_hash"]]) for c in policy_chunks]
    responses = [scripted_response("intent_extractor", UserIntent, {"required_question_ids":["answer"],"constraints":scene.constraints}),
        scripted_response("supervisor", DagPlan, {"goal":scene.request,"nodes":nodes}),
        scripted_response(agent, AgentDirective, {"tool_calls":[{"name":scene.tool,"arguments":scene.arguments}],"output":{}}, scope=node["id"]),
        scripted_response("recommendation" if recommendation else "answer_synthesizer", AgentDraft, draft.model_dump(mode="json"), scope="recommend_books" if recommendation else None),
        scripted_response("verifier", LLMVerifierReview, {"approved":True,"issues":[]})]
    for response in responses:
        response["call_scope"] = round_scope + "/" + response["call_scope"].partition("/")[2]
    return FakeModelScriptV2(schema_version="v2", responses=responses)


async def run_read_case(case: EvalCaseV1, script, fixture, prompt_loader, *, trace_factory=None):
    scene = READ_SCENARIOS[case.case_id]
    chunks = [chunk for call in fixture.calls if call.tool_name == "search_policy" and call.response
              for chunk in call.response.get("chunks", [])]
    catalog, writes = ControlledCatalog(), NoBusinessWrites()
    repository = ComponentMemoryRepository(case.case_id)
    memory = MemoryService(repository)
    if case.memory_seed:
        await memory.apply_candidates(1, [MemoryCandidate.model_validate(seed) for seed in case.memory_seed], conversation_id="seed", message_id="seed-1")
    saved = await memory.list(1, None, 20)
    driver = ProductionChatDriver(script, catalog=catalog, orders=writes, knowledge=ControlledKnowledge(chunks),
        prompt_loader=prompt_loader, memory_service=memory, trace_factory=trace_factory)
    result = await driver.round(case.user_request, auth=case.auth, scope="turn-1", conversation_id=f"v2-{case.case_id}")
    driver.finish()
    constraints = scene.constraints
    failures = recommendation_failures(result, BOOKS, RecommendationRequirements(
        allowed_ids=frozenset(scene.ids), count=len(scene.ids), max_price=constraints.get("max_price"),
        total_budget=scene.total_budget, language=constraints.get("language"),
        included_author=next(iter(constraints.get("authors", [])), None),
        excluded_authors=frozenset(constraints.get("excluded_authors", [])),
        category=next(iter(constraints.get("categories", [])), None), in_stock=constraints.get("in_stock") is True))
    if writes.calls or set(result.tools.executed).intersection({"prepare_order","create_order","create_inventory_watch"}):
        failures.append("UNEXPECTED_BUSINESS_WRITE")
    if scene.tool not in result.tools.executed: failures.append("REQUIRED_TOOL_NOT_EXECUTED")
    callers = {call[0] for call in result.model_calls}
    if "verifier" not in callers or (case.case_id.startswith("recommendation-") and "recommendation" not in callers):
        failures.append("REQUIRED_AGENT_NOT_EXECUTED")
    if not any(event.event == "verification.completed" and event.data["approved"] for event in result.events):
        failures.append("VERIFICATION_NOT_APPROVED")
    actual_books = [book for name, payload, _ in result.tools.results for book in (payload.get("books", []) if name == "search_books" else [payload] if name == "get_book" else [])]
    if case.case_id.startswith("query-"):
        expected_ids = {6} if case.case_id == "query-007" else set(scene.ids)
        if {b["book_id"] for b in actual_books} != expected_ids: failures.append("QUERY_SET_MISMATCH")
        if any(b != BOOKS.get(b["book_id"]) for b in actual_books): failures.append("SEVERE_FACT_HALLUCINATION")
        if any(e.event == "confirmation.required" for e in result.events): failures.append("UNEXPECTED_CONFIRMATION")
    answer = result.final_message.get("content", "")
    if case.case_id == "query-006" and "未找到" not in answer: failures.append("EMPTY_QUERY_NOT_DISCLOSED")
    if case.case_id == "query-007" and not ("下架" in answer and "不可购买" in answer): failures.append("INACTIVE_NOT_DISCLOSED")
    if case.case_id == "query-008" and (not chunks or "不支持" not in answer or any(f"v{c['version']}，{c['section']}" not in answer for c in chunks)):
        failures.append("POLICY_CITATION_MISMATCH")
    if case.case_id == "query-008":
        failures.extend(policy_answer_failures(answer, chunks))
    if case.case_id == "recommendation-002":
        prices = sorted(b["price"] for b in actual_books)
        if len(prices) < 3 or sum(prices[:3]) != 11500 or "总预算" not in answer: failures.append("TOTAL_BUDGET_NOT_DISCLOSED")
    if case.case_id == "recommendation-008" and any(args.get("in_stock") is True for name,args in catalog.calls if name == "search_books"):
        failures.append("OUT_OF_STOCK_FILTER_FORCED")
    if case.case_id == "recommendation-010":
        if not saved or saved != await memory.list(1,None,20): failures.append("STABLE_PREFERENCE_CHANGED")
        used_count = sum(
            len(event.data.get("memory_ids", []))
            for event in result.events
            if event.event == "memory.trace" and event.data.get("operation") == "READ"
        )
        if used_count < len(saved): failures.append("MEMORY_NOT_USED")
    # Preserve each fact violation until the harness counts it; public failure
    # codes are deduplicated only after metrics have been derived.
    return result, failures
