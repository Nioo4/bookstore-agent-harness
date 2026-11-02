from __future__ import annotations

"""Deterministic checks applied after the single-agent model returns FINAL.

The validator intentionally does not grade prose quality.  It checks only
claims which can be proven from this run's Catalog/RAG tool observations.
"""

from typing import Any, Iterable

from app.rag.evidence import evidence_from_chunk, validate_book_evidence, validate_policy_evidence

from app.agents.verification import UserConstraints


def validate_final_action(
    action: Any,
    tool_results: list[dict[str, Any]],
    *,
    constraints: UserConstraints | None = None,
    required_question_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    observed: dict[int, dict[str, Any]] = {}
    evidence_by_book: dict[int, set[str]] = {}
    policy_evidence: set[str] = set()
    policy_chunks_by_id: dict[str, dict[str, Any]] = {}
    book_chunks_by_id: dict[str, dict[str, Any]] = {}
    for item in tool_results:
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        for book in value.get("books", []) if isinstance(value.get("books"), list) else []:
            if isinstance(book, dict) and isinstance(book.get("book_id"), int):
                observed[int(book["book_id"])] = book
        if isinstance(value.get("book_id"), int):
            observed[int(value["book_id"])] = value
        ids = [str(value_id) for value_id in item.get("evidence_ids") or []]
        if item.get("tool") == "search_policy":
            policy_evidence.update(ids)
            chunks = value.get("chunks") if isinstance(value.get("chunks"), list) else []
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    issues.append({"code": "EVIDENCE_MISMATCH", "message": "政策工具返回了非结构化证据"})
                    continue
                try:
                    projected = evidence_from_chunk(chunk)
                except ValueError as exc:
                    issues.append({"code": "EVIDENCE_MISMATCH", "message": str(exc)})
                    continue
                evidence_id = str(projected["evidence_id"])
                if evidence_id not in ids:
                    issues.append({"code": "EVIDENCE_MISMATCH", "message": "政策证据ID与真实Chunk不一致"})
                policy_chunks_by_id[evidence_id] = chunk
        for evidence_id in ids:
            if evidence_id.startswith("book-guide:"):
                parts = evidence_id.split(":", 2)
                if len(parts) == 3:
                    try:
                        evidence_by_book.setdefault(int(parts[1]), set()).add(evidence_id)
                    except ValueError:
                        pass
        if item.get("tool") == "search_book_knowledge":
            chunks = value.get("chunks") if isinstance(value.get("chunks"), list) else []
            allowed_ids = [
                int(book.get("book_id"))
                for tool_result in tool_results
                if tool_result.get("tool") in {"search_books", "get_book"}
                for book in (
                    (tool_result.get("value") or {}).get("books", [])
                    if isinstance((tool_result.get("value") or {}).get("books"), list)
                    else [((tool_result.get("value") or {}).get("book") or (tool_result.get("value") or {}))]
                )
                if isinstance(book, dict) and type(book.get("book_id")) is int
            ]
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    issues.append({"code": "EVIDENCE_MISMATCH", "message": "书籍工具返回了非结构化证据"})
                    continue
                try:
                    validate_book_evidence(chunk, allowed_ids)
                    projected = evidence_from_chunk(chunk)
                except ValueError as exc:
                    issues.append({"code": "EVIDENCE_MISMATCH", "message": str(exc)})
                    continue
                evidence_id = str(projected["evidence_id"])
                if evidence_id not in ids:
                    issues.append({"code": "EVIDENCE_MISMATCH", "message": "书籍证据ID与真实Chunk不一致"})
                book_chunks_by_id[evidence_id] = chunk

    recommendations = getattr(action, "recommendations", []) or []
    preview_books: dict[int, tuple[dict[str, Any], set[str]]] = {}
    for item in tool_results:
        if item.get("tool") != "prepare_order" or not isinstance(item.get("value"), dict):
            continue
        preview_evidence = {str(value) for value in item.get("evidence_ids") or []}
        for preview_item in item["value"].get("items", []):
            if isinstance(preview_item, dict) and type(preview_item.get("book_id")) is int:
                preview_books[preview_item["book_id"]] = (preview_item, preview_evidence)
    recommended_books: list[dict[str, Any]] = []
    for recommendation in recommendations:
        if hasattr(recommendation, "model_dump"):
            recommendation = recommendation.model_dump(mode="python", exclude_none=True)
        if not isinstance(recommendation, dict):
            issues.append({"code": "RECOMMENDATION_INVALID", "message": "推荐项不是结构化对象"})
            continue
        try:
            book_id = int(recommendation.get("book_id"))
        except (TypeError, ValueError):
            issues.append({"code": "RECOMMENDATION_BOOK_UNGROUNDED", "message": "推荐项缺少有效 book_id"})
            continue
        book = observed.get(book_id)
        preview = preview_books.get(book_id)
        if book is None and preview is not None:
            preview_item, preview_evidence = preview
            cited = {str(value) for value in recommendation.get("evidence_ids") or []}
            if not cited.intersection(preview_evidence):
                issues.append({"code": "EVIDENCE_MISMATCH", "message": f"book_id={book_id} 的订单预览没有引用当前预览证据"})
                continue
            claimed_price = recommendation.get("price")
            if claimed_price is not None and claimed_price != preview_item.get("unit_price"):
                issues.append({"code": "CATALOG_FACT_MISMATCH", "message": f"book_id={book_id} 的 price 与订单预览不一致"})
            continue
        if book is None:
            issues.append({"code": "RECOMMENDATION_BOOK_UNGROUNDED", "message": f"book_id={book_id} 不在当前 Catalog 证据中"})
            continue
        recommended_books.append(book)
        for field in ("price", "available_stock", "status", "language_code"):
            if field in recommendation and field in book and recommendation[field] != book[field]:
                issues.append({"code": "CATALOG_FACT_MISMATCH", "message": f"book_id={book_id} 的 {field} 与实时 Catalog 不一致"})
        cited = recommendation.get("evidence_ids") or []
        cited_ids = set(map(str, cited))
        preview_evidence = preview_books.get(book_id, ({}, set()))[1]
        if cited and not (
            cited_ids.intersection(evidence_by_book.get(book_id, set()))
            or (preview_evidence and cited_ids.intersection(preview_evidence))
        ):
            issues.append({"code": "EVIDENCE_MISMATCH", "message": f"book_id={book_id} 的推荐理由没有引用同书知识证据"})

    if constraints is not None and recommended_books:
        prices: list[int] = []
        for book in recommended_books:
            price = book.get("price")
            if type(price) is int and price >= 0:
                prices.append(price)
            if constraints.max_price is not None and (type(price) is not int or price > constraints.max_price):
                issues.append({"code": "BUDGET_VIOLATION", "message": f"book_id={book.get('book_id')} exceeds the explicit per-book budget"})
            if constraints.currency is not None and str(book.get("currency", "")).casefold() != constraints.currency.casefold():
                issues.append({"code": "CURRENCY_CONSTRAINT_VIOLATION", "message": f"book_id={book.get('book_id')} has the wrong currency"})
            if constraints.language is not None and str(book.get("language_code", "")).casefold() != constraints.language.casefold():
                issues.append({"code": "LANGUAGE_CONSTRAINT_VIOLATION", "message": f"book_id={book.get('book_id')} does not match the requested language"})
            authors = {str(value).casefold() for value in book.get("authors", []) if value is not None}
            if constraints.authors and not authors.intersection(str(value).casefold() for value in constraints.authors):
                issues.append({"code": "AUTHOR_CONSTRAINT_VIOLATION", "message": f"book_id={book.get('book_id')} does not match the requested author"})
            categories = {str(value).casefold() for value in book.get("categories", []) if value is not None}
            if constraints.categories and not categories.intersection(str(value).casefold() for value in constraints.categories):
                issues.append({"code": "CATEGORY_CONSTRAINT_VIOLATION", "message": f"book_id={book.get('book_id')} does not match the requested category"})
            stock = book.get("available_stock")
            if constraints.in_stock is True and (type(stock) is not int or stock <= 0):
                issues.append({"code": "STOCK_CONSTRAINT_VIOLATION", "message": f"book_id={book.get('book_id')} is not in stock"})
            if constraints.in_stock is False and (type(stock) is not int or stock > 0):
                issues.append({"code": "STOCK_CONSTRAINT_VIOLATION", "message": f"book_id={book.get('book_id')} is not out of stock"})
        if constraints.total_budget is not None and (len(prices) != len(recommended_books) or sum(prices) > constraints.total_budget):
            issues.append({"code": "TOTAL_BUDGET_VIOLATION", "message": "recommended books exceed the explicit total budget"})

    for claim in getattr(action, "policy_claims", []) or []:
        if hasattr(claim, "model_dump"):
            claim = claim.model_dump(mode="python", exclude_none=True)
        if not isinstance(claim, dict):
            issues.append({"code": "POLICY_CLAIM_INVALID", "message": "政策声明不是结构化对象"})
            continue
        cited = set(map(str, claim.get("evidence_ids") or []))
        if not cited or not cited.intersection(policy_evidence):
            issues.append({"code": "POLICY_CLAIM_UNGROUNDED", "message": "政策声明缺少当前政策证据"})
            continue
        expected_source = claim.get("source_uri", claim.get("source"))
        expected_section = claim.get("section_path", claim.get("section"))
        expected_version = claim.get("version")
        matched = False
        for evidence_id in cited:
            chunk = policy_chunks_by_id.get(evidence_id)
            if chunk is None:
                continue
            try:
                validate_policy_evidence(
                    chunk,
                    source_uri=str(expected_source) if expected_source is not None else None,
                    section_path=str(expected_section) if expected_section is not None else None,
                    version=str(expected_version) if expected_version is not None else None,
                )
                matched = True
                break
            except ValueError:
                continue
        if not matched:
            issues.append({"code": "POLICY_EVIDENCE_MISMATCH", "message": "政策声明的来源、章节或版本未与同一真实Chunk严格匹配"})

    required = set(required_question_ids)
    addressed = set(getattr(action, "addressed_question_ids", []) or [])
    missing = sorted(required - addressed)
    if missing:
        issues.append({"code": "QUESTION_NOT_ADDRESSED", "message": "未覆盖用户问题: " + ", ".join(missing)})
    return issues
