"""Explicit deterministic E2E fixture, never enabled by normal compose.yaml.

Implements the transport boundary only: real Harness, RPC, MySQL, SMTP, Redis and
Qdrant still execute. This is not evidence of a real model's recommendation quality.
"""
from __future__ import annotations

import hashlib
import json
import re

from fastapi import FastAPI, Header, HTTPException

app = FastAPI(docs_url=None, redoc_url=None)


def objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)
    elif isinstance(value, str) and value.startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except ValueError:
            return
        yield from objects(nested)


def system_json(messages: list[dict], prefix: str) -> dict:
    for message in messages:
        content = str(message.get("content", ""))
        if message.get("role") != "system" or not content.startswith(prefix):
            continue
        try:
            value = json.loads(content[len(prefix):])
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def output_for(schema: str, messages: list[dict]):
    text = next(message["content"] for message in messages if message["role"] == "user")
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    request = str(data.get("request", data.get("objective", text)))
    if schema in {"SingleAgentAction", "AgentAction"}:
        # V2 sends the same strict action schema for every round.  Keep this
        # provider deterministic and evidence-driven; it is a transport
        # fixture, not a recommendation-quality oracle.
        effective_request = system_json(messages, "EFFECTIVE_REQUEST_JSON=")
        required_question_ids = [
            value
            for value in effective_request.get("required_question_ids", [])
            if isinstance(value, str)
        ]
        tool_names = set()
        for message in messages:
            content = str(message.get("content", ""))
            if message.get("role") != "system" or not content.startswith("ALLOWED_TOOLS_JSON="):
                continue
            for item in objects(content.split("=", 1)[1]):
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    tool_names.add(item["name"])
        # Current providers receive local results as bounded system JSON, not
        # role=tool messages without a provider-native tool_calls predecessor.
        # Normalize both shapes here so the transport fixture can still read
        # archived protocol tests while exercising the current runtime path.
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        for message in messages:
            content = str(message.get("content", ""))
            if message.get("role") != "system" or not content.startswith("LOCAL_TOOL_RESULT_JSON="):
                continue
            try:
                envelope = json.loads(content.split("=", 1)[1].split(" IDENTICAL_READ_REUSED=", 1)[0])
            except (TypeError, ValueError):
                continue
            tool_messages.append(
                {
                    "role": "tool",
                    "name": envelope.get("tool"),
                    "content": json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
                }
            )
        if ("购买" in request or "下单" in request) and "prepare_order" in tool_names:
            if any(message.get("name") == "prepare_order" for message in tool_messages):
                # The runtime turns the structured prepare result into the
                # confirmation card; the model only needs to finish safely.
                return {"action": "FINAL", "answer": "已生成订单预览，请确认。", "recommendations": [], "policy_claims": [], "addressed_question_ids": required_question_ids}
            books = [
                item
                for message in tool_messages
                for item in objects(message.get("content", ""))
                if isinstance(item, dict) and type(item.get("book_id")) is int and "available_stock" in item
            ]
            if books:
                return {
                    "action": "CALL_TOOLS",
                    "tool_calls": [{"call_id": "prepare-1", "name": "prepare_order", "arguments": {"items": [{"book_id": books[0]["book_id"], "quantity": 1}]}}],
                }
        if tool_messages:
            books = {
                int(item["book_id"]): item
                for message in tool_messages
                for item in objects(message.get("content", ""))
                if isinstance(item, dict)
                and type(item.get("book_id")) is int
                and "available_stock" in item
                and "price" in item
            }
            recommendations = [
                {
                    "book_id": book["book_id"],
                    "price": book["price"],
                    "currency": book.get("currency", "CNY"),
                    "available_stock": book["available_stock"],
                    "reason": "来自本轮实时商品查询",
                    # This transport fixture does not claim recommendation
                    # quality.  Real same-book BOOK_GUIDE citations are
                    # exercised by the dedicated RAG evaluation/browser run.
                    "evidence_ids": [],
                }
                for book in books.values()
            ]
            return {
                "action": "FINAL",
                "answer": "；".join(str(item.get("title", "")) for item in books.values()) or "已根据本轮工具结果处理。",
                "recommendations": recommendations,
                "policy_claims": [],
                "addressed_question_ids": required_question_ids,
            }
        match = re.search(r"(?:book_id[=: ]+|书籍\s*)(\d+)", request)
        if ("购买" in request or "下单" in request) and "prepare_order" in tool_names:
            # The order tool may only use a book returned by the current run.
            # If no result exists yet, fetch that book first.
            if match and "get_book" in tool_names:
                return {"action": "CALL_TOOLS", "tool_calls": [{"call_id": "catalog-1", "name": "get_book", "arguments": {"book_id": int(match[1])}}]}
        if "政策" in request or "规则" in request:
            if "search_policy" in tool_names:
                return {"action": "CALL_TOOLS", "tool_calls": [{"call_id": "policy-1", "name": "search_policy", "arguments": {"query": request}}]}
        if match and "get_book" in tool_names:
            return {"action": "CALL_TOOLS", "tool_calls": [{"call_id": "book-1", "name": "get_book", "arguments": {"book_id": int(match[1])}}]}
        args = {"limit": 3}
        keyword = re.search(r"书名包含「([^」]+)」", request)
        if keyword:
            args["q"] = keyword[1]
        budget = re.search(r"预算(\d+)元", request)
        if budget:
            args["max_price"] = int(budget[1]) * 100
        if "一本" in request:
            args["limit"] = 1
        if "英文" in request:
            args["language"] = "en"
        if "有货" in request:
            args["in_stock"] = True
        if "search_books" in tool_names:
            return {"action": "CALL_TOOLS", "tool_calls": [{"call_id": "search-1", "name": "search_books", "arguments": args}]}
        return {"action": "FINAL", "answer": "无法从当前测试工具确定该问题。", "recommendations": [], "policy_claims": [], "addressed_question_ids": required_question_ids}
    # The remaining schema branches are retained for isolated historical unit
    # tests only.  The V2 Compose smoke reaches the SingleAgentAction branch
    # above and never asks the fixture for a plan, checkpoint, or replay.
    if schema == "UserIntent":
        constraints = {}
        budget = re.search(r"预算(\d+)元", request)
        if budget:
            constraints.update(max_price=int(budget[1])*100, currency="CNY")
        if "英文" in text:
            constraints["language"] = "en"
        if "有货" in text:
            constraints["in_stock"] = True
        return {"constraints": constraints}
    if schema == "DagPlan":
        from app.harness.routing import required_agents_for_request
        required = set(required_agents_for_request(request))
        required.add("catalog")
        order = "购买" in request or "下单" in request or "订单查询" in request
        nodes = [{"id": name, "agent": name, "objective": request, "depends_on": [], "input_refs": ["user_request"], "expected_output": name + "_result"} for name in sorted(required)]
        if order:
            nodes.append({"id": "order", "agent": "order", "objective": request, "depends_on": ["catalog"], "input_refs": ["user_request", "catalog_result"], "expected_output": "order_result"})
        elif "knowledge" not in required:
            nodes.append({"id": "recommendation", "agent": "recommendation", "objective": request, "depends_on": ["catalog"], "input_refs": ["user_request", "catalog_result"], "expected_output": "recommendation_result"})
        return {"goal": request, "nodes": nodes}
    if schema == "AgentDirective":
        names = {item["name"] for item in data["allowed_tools"]}
        if "search_books" in names:
            match = re.search(r"(?:book_id[=: ]+|书籍\s*)(\d+)", request)
            if match:
                call = {"name": "get_book", "arguments": {"book_id": int(match[1])}}
            else:
                args = {"limit": 3}
                keyword = re.search(r"书名包含「([^」]+)」", request)
                if keyword:
                    args["q"] = keyword[1]
                budget = re.search(r"预算(\d+)元", request)
                if budget:
                    args["max_price"] = int(budget[1])*100
                if "一本" in request:
                    args["limit"] = 1
                for obj in objects(data.get("inputs", {})):
                    if obj.get("memory_key") == "preference.languages":
                        args["language"] = obj["value"]["items"][0]
                if "英文" in text:
                    args["language"] = "en"
                if "有货" in text:
                    args["in_stock"] = True
                call = {"name": "search_books", "arguments": args}
        elif "search_policy" in names:
            call = {"name": "search_policy", "arguments": {"query": request}}
        elif "prepare_order" in names:
            if "订单查询" in request:
                call = {"name": "list_my_orders", "arguments": {}}
            else:
                books = [obj for obj in objects(data) if "book_id" in obj and "available_stock" in obj]
                if not books:
                    raise ValueError("fixture requires real catalog evidence")
                call = {"name": "prepare_order", "arguments": {"items": [{"book_id": books[0]["book_id"], "quantity": 1}]}}
        else:
            return {"output": {}}
        return {"tool_calls": [call]}
    if schema == "AgentDraft":
        books = {obj["book_id"]: obj for obj in objects(data) if {"book_id", "available_stock", "price", "version"}.issubset(obj)}
        recommendations = [{"book_id": b["book_id"], "price": b["price"], "currency": b["currency"], "available_stock": b["available_stock"], "reason": "来自本轮实时商品查询", "evidence_ids": [f"book:{b['book_id']}:v{b['version']}"]} for b in books.values()]
        claims = []
        for obj in objects(data):
            if "chunks" in obj:
                for chunk in obj["chunks"]:
                    if not isinstance(chunk, dict):
                        continue
                    # Evidence identifiers must come from supplied results, never guessed.
                    ids = [value for container in objects(data) for value in container.get("evidence_ids", []) if isinstance(value, str) and value.startswith("policy:")]
                    if ids:
                        claims.append({"claim": chunk["content"], "source": chunk["document_title"], "section": chunk["section"], "version": chunk["version"], "evidence_ids": ids})
        answer = "；".join(b["title"] for b in books.values()) or "已根据本轮工具结果处理。"
        if claims:
            answer = "\n".join(f"{c['claim']}[《{c['source']}》v{c['version']}，{c['section']}]" for c in claims)
        return {"answer": answer, "recommendations": recommendations, "policy_claims": claims}
    if schema == "LLMVerifierReview":
        return {"approved": True, "issues": []}
    if schema == "MemoryExtraction":
        candidates = []
        if "我喜欢英文" in text or "我喜欢中文" in text:
            language = "en" if "我喜欢英文" in text else "zh-CN"
            candidates.append({"memory_type": "PREFERENCE", "memory_key": "preference.languages", "operation": "SET", "value": {"items": [language]}, "source": "EXPLICIT", "confidence": 1.0, "evidence_excerpt": "用户明确表达阅读语言偏好"})
        return {"candidates": candidates}
    raise ValueError("unsupported fixture schema")


@app.get("/healthz")
async def health():
    return {"status": "ok", "mode": "deterministic-e2e-fixture"}


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def complete(body: dict, authorization: str | None = Header(default=None)):
    if authorization != "Bearer local-e2e-fixture-not-a-provider-secret":
        raise HTTPException(401, "fixture chat credential mismatch")
    try:
        result = output_for(body["response_format"]["json_schema"]["schema"]["title"], body["messages"])
    except Exception:
        raise HTTPException(422, "unsupported fixture request") from None
    return {"model": body["model"], "choices": [{"message": {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 20, "completion_tokens": 20}}


@app.post("/v1/embeddings")
@app.post("/embeddings")
async def embed(body: dict, authorization: str | None = Header(default=None)):
    if authorization != "Bearer local-e2e-embedding-not-a-provider-secret":
        raise HTTPException(401, "fixture embedding credential mismatch")
    texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
    dimensions = body.get("dimensions", 32)
    if dimensions != 32 or len(texts) > 20:
        raise HTTPException(422, "unsupported fixture vector request")
    return {"data": [{"index": i, "embedding": [(b - 127.5) / 127.5 for b in hashlib.sha256(text.encode()).digest()] * (dimensions // 32)} for i, text in enumerate(texts)], "usage": {"prompt_tokens": len(texts)}}
