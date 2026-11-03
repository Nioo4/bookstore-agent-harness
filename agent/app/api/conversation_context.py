"""Resolve a logged-in page's explicit follow-up request without treating history as facts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.agents.intent import InventoryWatchIntent, UserIntent, infer_inventory_watch
from app.agents.verification import UserConstraints
from app.api.conversations import ConversationContext, ConversationTurn, DisplayedBook
from app.trace.safety import TraceSafetyProjector


_CLEAR_CONTEXT = re.compile(r"(?:换个话题|不(?:再|要)?沿用|不要沿用|不沿用)")
_FOLLOW_UP = re.compile(
    r"(?:其他(?:要求|条件).*不变|只要|改成|取消(?:价格|预算)|保留|刚才|刚刚|这本|它|前两本|第[一二三四五12345]本|"
    r"^现在|^可以|^我刚|^主要|^把|^取消|^前)",
    re.IGNORECASE,
)
_PREFERENCE_ONLY = re.compile(r"(?:请)?记住|长期偏好|今后推荐|以后推荐", re.IGNORECASE)
_LANGUAGE_PREFERENCE = re.compile(r"(?:偏好|喜欢|长期).{0,12}?(?:中文|汉语|英文|english)", re.IGNORECASE)
_PRICE_CANCEL = re.compile(r"取消(?:价格|预算)(?:限制)?", re.IGNORECASE)
_PER_BOOK_BUDGET = re.compile(r"(?:每本|单本)[^。；，,]{0,20}?(\d+(?:\.\d+)?)\s*元")
_TOTAL_BUDGET = re.compile(r"(?:合计|总(?:预算|价)?)[^。；，,]{0,20}?(\d+(?:\.\d+)?)\s*元")
_LATIN_AUTHOR = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9 .'-]{0,80}?)\s*写的")
_LATIN_AUTHOR_ADJACENT_BOOK = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9 .'-]{0,80}?)\s*(?=(?:中文|汉语|英文|english)\s*(?:技术)?(?:书|图书|书籍))",
    re.IGNORECASE,
)
_EXPLICIT_OUT_OF_STOCK = re.compile(r"(?:只要|仅|必须|只找|要|查询|查找|找|有没有).{0,12}?缺货(?:的)?(?:书|本|图书)?")
_OUT_OF_STOCK_ALLOWED = re.compile(r"缺货.{0,8}?(?:也可以|也行|都可以|允许)")
_CATALOG_QUERY_HINTS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"服务雪崩|熔断|限流|容错|故障恢复"), ("分布式系统", "Java")),
    (re.compile(r"消息幂等|重复消费|消息队列"), ("分布式系统",)),
    (re.compile(r"数据库索引|索引优化|查询优化"), ("数据库",)),
)


@dataclass(frozen=True)
class EffectiveConversationRequest:
    intent: UserIntent
    inventory_watch: InventoryWatchIntent | None
    constraints: UserConstraints
    uses_context: bool
    context_for_agents: dict[str, Any]
    selected_book: DisplayedBook | None
    referenced_books: list[DisplayedBook]


def resolve_request(
    content: str,
    intent: UserIntent,
    context: ConversationContext,
) -> EffectiveConversationRequest:
    """Merge only an explicit follow-up with prior page state, never stale catalog facts."""
    clears_context = bool(_CLEAR_CONTEXT.search(content))
    uses_context = not clears_context and bool(context.active_request or context.displayed_books) and bool(_FOLLOW_UP.search(content))
    previous = _constraints_from_context(context)
    current = _normalized_explicit_constraints(intent.constraints, content)
    constraints = _merge_constraints(previous, current, content) if uses_context else current
    selected, referenced = _resolve_reference(content, context) if uses_context else (None, [])
    inventory_watch = infer_inventory_watch(content)
    if inventory_watch is not None:
        inventory_watch = _resolve_watch_identifier(
            inventory_watch,
            selected,
        )
    context_for_agents: dict[str, Any] = {"used": uses_context}
    if uses_context:
        context_for_agents.update(
            {
                "prior_request": context.active_request,
                "effective_constraints": constraints.model_dump(mode="json"),
                "displayed_books": [book.model_dump(mode="json") for book in context.displayed_books],
                "selected_book": selected.model_dump(mode="json") if selected is not None else None,
                "referenced_books": [book.model_dump(mode="json") for book in referenced],
            }
        )
    return EffectiveConversationRequest(
        intent=intent.model_copy(deep=True),
        inventory_watch=inventory_watch,
        constraints=constraints,
        uses_context=uses_context,
        context_for_agents=context_for_agents,
        selected_book=selected,
        referenced_books=referenced,
    )


def request_context(
    content: str,
    effective: EffectiveConversationRequest,
    memory_context: list[dict[str, Any]],
) -> dict[str, Any]:
    selected_book = effective.selected_book.model_dump(mode="json") if effective.selected_book is not None else None
    referenced_books = [book.model_dump(mode="json") for book in effective.referenced_books]
    return {
        "user_request": content,
        "required_question_ids": effective.intent.required_question_ids,
        "constraints": effective.constraints.model_dump(mode="json"),
        "inventory_watch": (
            effective.inventory_watch.model_dump(mode="json")
            if effective.inventory_watch is not None
            else None
        ),
        "conversation_context": effective.context_for_agents,
        "selected_book": selected_book,
        "referenced_books": referenced_books,
        "memory_context": memory_context,
        "catalog_query_hints": catalog_query_hints(content),
    }


def catalog_query_hints(content: str) -> list[str]:
    """Map a small reviewed concept vocabulary to Catalog-searchable topics.

    Hints do not claim book coverage and never weaken explicit filters. They
    only bridge natural problem wording to the deliberately small demo catalog;
    BOOK_GUIDE evidence must still support any final suitability statement.
    """
    values: list[str] = []
    for pattern, hints in _CATALOG_QUERY_HINTS:
        if pattern.search(content):
            values.extend(hints)
    return list(dict.fromkeys(values))


def deterministic_intent(content: str) -> UserIntent:
    """Build the small request contract without a second model call.

    The online v2 path has one model role only: the controlled answer/tool
    loop.  Hard filters are parsed from explicit text and normalized by the
    same code used to merge follow-ups; the question IDs are stable labels for
    final-answer coverage, not model-invented facts.
    """
    text = content.strip()
    question_ids: list[str] = []
    if re.search(r"(?:退换|退款|配送|隐私|规则|政策|能否退|可以退|是否支持退)", text, re.IGNORECASE):
        question_ids.append("policy")
    purchase_intent = re.search(r"(?:买|下单|购买|订单预览|生成.{0,6}预览)", text, re.IGNORECASE)
    comparison_intent = re.search(r"(?:比较|区别|差别|哪个更|更适合|前两本)", text, re.IGNORECASE)
    ordinal_reference = re.search(r"第[一二三四五12345]本", text, re.IGNORECASE)
    if comparison_intent or (ordinal_reference and not purchase_intent):
        question_ids.append("comparison")
    watch_intent = re.search(r"(?:补货|到货|库存).{0,24}(?:监听|提醒|通知)|(?:监听|提醒|通知).{0,24}(?:补货|到货|库存)", text, re.IGNORECASE)
    if watch_intent:
        question_ids.append("inventory_watch")
    catalog_fact = (
        bool(re.search(r"ISBN\s*(?:为|是|:|：)?\s*[0-9Xx-]{10,17}", text, re.IGNORECASE))
        and bool(re.search(r"价格|多少钱|库存|有货", text))
        and not re.search(r"推荐|适合|比较|区别|差别|哪个|入门|难度|前置|内容|主题|评价|买|下单|购买|预览|规则|政策|监听|提醒|通知", text)
    )
    if catalog_fact:
        question_ids.append("catalog_fact")
    elif re.search(r"(?:买|下单|购买|查(?:询|看).{0,8}订单|我的订单|价格|预算|有货|书|图书|推荐|作者|分类|语言|ISBN|isbn)", text, re.IGNORECASE) and not watch_intent:
        question_ids.append("recommendation")
    if not question_ids:
        question_ids.append("answer")
    # Preserve first-seen semantic order and avoid duplicate IDs when a
    # request combines (for example) a recommendation with a comparison.
    question_ids = list(dict.fromkeys(question_ids))
    constraints = _normalized_explicit_constraints(UserIntent().constraints, text)
    return UserIntent(required_question_ids=question_ids, constraints=constraints)


def updated_context(
    context: ConversationContext,
    content: str,
    answer: str,
    effective: EffectiveConversationRequest,
    catalog_books: dict[int, dict[str, Any]],
    recommendation_book_ids: list[int],
) -> ConversationContext:
    """Apply a completed response only; callers must not commit failed or partial runs."""
    updated = context.model_copy(deep=True)
    projector = TraceSafetyProjector()
    updated.turns.append(
        ConversationTurn(
            request=projector.text(content).value,
            answer=projector.text(answer).value,
        )
    )
    if not _PREFERENCE_ONLY.search(content):
        updated.active_request = _next_active_request(updated.active_request, content, effective.uses_context)
        updated.constraints = effective.constraints.model_dump(mode="json")
    if effective.selected_book is not None:
        updated.selected_book = effective.selected_book

    displayed = [_displayed_book(catalog_books.get(book_id)) for book_id in recommendation_book_ids]
    displayed = [book for book in displayed if book is not None]
    if displayed:
        updated.displayed_books = displayed
        updated.selected_book = displayed[0] if len(displayed) == 1 else None
    elif len(catalog_books) == 1:
        updated.selected_book = _displayed_book(next(iter(catalog_books.values())))
    return updated


def _constraints_from_context(context: ConversationContext) -> UserConstraints:
    try:
        return UserConstraints.model_validate(context.constraints)
    except Exception:
        return UserConstraints()


def _normalized_explicit_constraints(raw: UserConstraints, content: str) -> UserConstraints:
    """Keep only user-stated hard filters; a topic is not an invented catalog category."""
    values = raw.model_dump(mode="python")
    # Model extraction is a hint, not authority for a hard catalog filter.
    # Rebuild scalar filters from text below so a stated learning background or
    # preference cannot silently become a language, budget, or stock limit.
    for field in ("max_price", "total_budget", "currency", "language", "in_stock"):
        values[field] = None
    values["categories"] = values["categories"] if "分类" in content else []
    values["authors"] = [author for author in values["authors"] if author and author.casefold() in content.casefold()]
    values["excluded_authors"] = [author for author in values["excluded_authors"] if author and author.casefold() in content.casefold()]
    # Keep a plainly written Latin author even when the extraction model only
    # returns the surrounding generic phrase (for example “Alice 写的中文技术书”).
    # This is a filter, not an inferred preference or catalog category.
    if author_match := _LATIN_AUTHOR.search(content) or _LATIN_AUTHOR_ADJACENT_BOOK.search(content):
        values["authors"] = [author_match.group(1).strip()]
    # “偏好中文/英文” is a soft memory statement, not this round's hard
    # catalog filter. A direct request such as “只要中文” remains explicit.
    if not _LANGUAGE_PREFERENCE.search(content):
        if "中文" in content or "汉语" in content:
            values["language"] = "zh-CN"
        elif "英文" in content or re.search(r"\benglish\b", content, re.IGNORECASE):
            values["language"] = "en"
    if "有货" in content:
        values["in_stock"] = True
    elif _EXPLICIT_OUT_OF_STOCK.search(content) and not _OUT_OF_STOCK_ALLOWED.search(content):
        values["in_stock"] = False
    per_book = _PER_BOOK_BUDGET.search(content)
    total = _TOTAL_BUDGET.search(content)
    if per_book:
        values["max_price"] = _yuan_to_fen(per_book.group(1))
        values["currency"] = "CNY"
    if total:
        values["total_budget"] = _yuan_to_fen(total.group(1))
        values["currency"] = "CNY"
    return UserConstraints.model_validate(values)


def _merge_constraints(previous: UserConstraints, current: UserConstraints, content: str) -> UserConstraints:
    values = previous.model_dump(mode="python")
    for field in ("max_price", "total_budget", "currency", "language", "in_stock"):
        value = getattr(current, field)
        if value is not None:
            values[field] = value
    for field in ("authors", "excluded_authors", "categories"):
        value = getattr(current, field)
        if value:
            values[field] = value
    if _PRICE_CANCEL.search(content):
        values["max_price"] = None
        values["total_budget"] = None
    return UserConstraints.model_validate(values)


def _resolve_reference(content: str, context: ConversationContext) -> tuple[DisplayedBook | None, list[DisplayedBook]]:
    ordered = context.displayed_books
    match = re.search(r"第([一二三四五12345])本", content)
    if match:
        token = match.group(1)
        index = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4}.get(token)
        if index is None and token.isdigit():
            index = int(token) - 1
        if 0 <= index < len(ordered):
            return ordered[index], [ordered[index]]
    if "前两本" in content:
        return None, ordered[:2]
    if "这本" in content or "刚才" in content or "刚刚" in content:
        if context.selected_book is not None:
            return context.selected_book, [context.selected_book]
        if len(ordered) == 1:
            return ordered[0], [ordered[0]]
    return None, []


def _resolve_watch_identifier(
    watch: InventoryWatchIntent,
    selected: DisplayedBook | None,
) -> InventoryWatchIntent:
    identifier = (watch.book_identifier or "").strip().casefold()
    if selected is not None and identifier in {"", "这本", "它", "this book", "it"}:
        return watch.model_copy(update={"book_identifier": str(selected.book_id)})
    return watch


def _next_active_request(previous: str, content: str, uses_context: bool) -> str:
    if not uses_context or not previous:
        return content
    joined = f"{previous}\n当前补充：{content}"
    return joined[-4_000:]


def _displayed_book(value: dict[str, Any] | None) -> DisplayedBook | None:
    if value is None:
        return None
    try:
        return DisplayedBook(
            book_id=int(value["book_id"]),
            isbn=str(value["isbn"]),
            title=str(value["title"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _yuan_to_fen(value: str) -> int:
    whole, _, fraction = value.partition(".")
    return int(whole) * 100 + int((fraction + "00")[:2])
