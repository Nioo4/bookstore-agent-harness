"""Independent assertions on observed production output, never model success flags."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import re
from typing import Any

from app.evaluation.production import ChatExecution


@dataclass(frozen=True)
class RecommendationRequirements:
    allowed_ids: frozenset[int]
    count: int | None = None
    max_price: int | None = None
    total_budget: int | None = None
    language: str | None = None
    included_author: str | None = None
    excluded_authors: frozenset[str] = field(default_factory=frozenset)
    category: str | None = None
    in_stock: bool = True


def _visible_catalog_problems(execution: ChatExecution, catalog: dict[int, dict[str, Any]]) -> set[tuple[str, int | None]]:
    """Small independent oracle for the fixed suite's visible money/stock syntax.

    Do not import the production renderer, trust recommendations as fact values,
    or use a model's approval. A named row is checked against that book, not any
    conveniently matching price in the directory.
    """
    observed = {book['book_id'] for name, value, _ in execution.tools.results
        for book in (value.get('books', []) if name == 'search_books' else [value] if name == 'get_book' else [])}
    problems = set()
    current = next(iter(observed)) if len(observed) == 1 else None
    money = re.compile(r'(?:CNY|[¥￥])\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(分|元|CNY)', re.I)
    stock = re.compile(r'(?:available\s+stock|库存)\s*[：:=]?\s*(\d+)', re.I)
    for line in execution.final_message.get('content', '').splitlines():
        named = re.search(r'(?:书籍\s*#|book[_ ]?id\s*[:：=]?\s*|\bB)(\d+)', line, re.I)
        if named:
            current = int(named[1])
            if current not in observed:
                problems.add(('book_id', current))
        targets = [catalog[current]] if current in catalog else [catalog[i] for i in observed if i in catalog]
        for match in money.finditer(line):
            value = Decimal(match[1] or match[2]) * (1 if match[3] == '分' else 100)
            if not any(book['currency'] == 'CNY' and book['price'] == value for book in targets):
                # Budget/aggregate rows are independently checked by their scenario.
                if not any(label in line for label in ('预算', '合计', '总价')):
                    problems.add(('price', current))
        for match in stock.finditer(line):
            if not any(book['available_stock'] == int(match[1]) for book in targets):
                problems.add(('available_stock', current))
    return problems


def visible_catalog_failures(execution: ChatExecution, catalog: dict[int, dict[str, Any]]) -> list[str]:
    return ['SEVERE_FACT_HALLUCINATION'] * len(_visible_catalog_problems(execution, catalog))


def policy_answer_failures(answer: str, chunks: list[dict[str, Any]]) -> list[str]:
    """Fixed policy scenes: check both capabilities, not a shared '不支持'."""
    failures = []
    negative = re.compile(r'不支持|不提供|不能|无法|不包含|尚未支持|未实现')
    without_citations = re.sub(r'\[《[^\]]+\]', '', answer)
    clauses = re.split(r'[。；;!！?？\n，,]|但是|但|不过', without_citations)
    for feature in ('退货', '物流'):
        bodies = [c.get('content', '') for c in chunks if feature in c.get('content', '')]
        # Expected capability comes from fixture body, never the draft's claim.
        if not bodies or not any(negative.search(body) for body in bodies):
            failures.append('POLICY_CITATION_MISMATCH')
            continue
        relevant = [clause for clause in clauses if feature in clause]
        if any(re.search(r'支持|提供|可以|可用|已实现', clause) and not negative.search(clause) for clause in relevant):
            failures.append('SEVERE_FACT_HALLUCINATION')
        elif not any(negative.search(clause) for clause in relevant):
            failures.append('POLICY_CITATION_MISMATCH')
    return failures


def recommendation_failures(execution: ChatExecution, catalog: dict[int, dict[str, Any]],
                            required: RecommendationRequirements) -> list[str]:
    failures = []
    actual = execution.final_message.get("recommendations", [])
    fact_problems = _visible_catalog_problems(execution, catalog)
    ids = [item["book_id"] for item in actual]
    candidates: dict[int, dict[str, Any]] = {}
    evidence = set()
    for name, result, evidence_ids in execution.tools.results:
        if name == "search_books":
            candidates.update({book["book_id"]: book for book in result["books"]})
            evidence.update(evidence_ids)
        elif name == "get_book":
            candidates[result["book_id"]] = result
            evidence.update(evidence_ids)
    if execution.final_status != "COMPLETED": failures.append("FINAL_STATUS_MISMATCH")
    if len(ids) != len(set(ids)): failures.append("DUPLICATE_RECOMMENDATION")
    if required.count is not None and len(ids) != required.count: failures.append("RECOMMENDATION_COUNT_MISMATCH")
    if not set(ids).issubset(required.allowed_ids): failures.append("RECOMMENDATION_SET_MISMATCH")
    total = 0
    for item in actual:
        book = catalog.get(item["book_id"])
        if book is None or item["book_id"] not in candidates:
            failures.append("UNSUPPORTED_BOOK")
            continue
        fact_problems.update((key, item["book_id"]) for key in ("price", "currency", "available_stock")
            if item.get(key) != book[key])
        if not item.get("evidence_ids") or not set(item["evidence_ids"]).issubset(evidence): failures.append("UNSUPPORTED_EVIDENCE")
        if book["status"] != "ACTIVE": failures.append("INACTIVE_RECOMMENDATION")
        if required.max_price is not None and book["price"] > required.max_price: failures.append("UNIT_BUDGET_VIOLATION")
        if required.language and book["language_code"] != required.language: failures.append("LANGUAGE_VIOLATION")
        if required.included_author and required.included_author not in book["authors"]: failures.append("AUTHOR_VIOLATION")
        if required.excluded_authors.intersection(book["authors"]): failures.append("EXCLUDED_AUTHOR_VIOLATION")
        if required.category and required.category not in book["categories"]: failures.append("CATEGORY_VIOLATION")
        if required.in_stock and book["available_stock"] <= 0: failures.append("STOCK_VIOLATION")
        if book["available_stock"] == 0 and "缺货" not in item.get("reason", ""): failures.append("OUT_OF_STOCK_NOT_DISCLOSED")
        total += book["price"]
    if required.total_budget is not None and total > required.total_budget: failures.append("TOTAL_BUDGET_VIOLATION")
    if execution.tools.unauthorized_write_calls: failures.append("UNAUTHORIZED_WRITE_EXECUTED")
    return failures + ["SEVERE_FACT_HALLUCINATION"] * len(fact_problems)


def effect_failures(*, orders: int, stock_decrement: int, outbox: int, expected_quantity: int) -> list[str]:
    """Inputs must be deltas read from the isolated database by transaction drivers."""
    return [] if (orders, stock_decrement, outbox) == (1, expected_quantity, 1) else ["DUPLICATE_OR_INCOMPLETE_TRANSACTION_EFFECT"]


def recovery_failures(*, original_thread: str, recovered_thread: str, original_run: str,
                      recovered_run: str, status: str, notifications: int, pending: int) -> list[str]:
    return [] if (original_thread == recovered_thread and original_run == recovered_run
        and status == "COMPLETED" and notifications == 1 and pending == 0) else ["RECOVERY_STATE_MISMATCH"]
