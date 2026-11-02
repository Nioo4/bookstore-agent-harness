import pytest

from app.agents.intent import UserIntent
from app.agents.verification import UserConstraints
from app.api.conversation_context import catalog_query_hints, deterministic_intent, resolve_request, updated_context
from app.api.conversations import ConversationContext, DisplayedBook, InMemoryConversationStore


def _book(book_id: int, title: str = "分布式系统零基础导读") -> dict:
    return {"book_id": book_id, "isbn": f"9780000000{book_id:03d}", "title": title, "available_stock": 0}


def test_follow_up_retains_explicit_conditions_but_a_new_topic_does_not():
    context = ConversationContext(
        active_request="推荐三本有货的分布式系统入门书，每本50元以内",
        constraints=UserConstraints(max_price=5000, in_stock=True).model_dump(mode="json"),
    )
    follow_up = resolve_request(
        "只要中文，其他要求不变",
        UserIntent(constraints=UserConstraints(language="zh-CN")),
        context,
    )
    assert follow_up.uses_context
    assert follow_up.constraints == UserConstraints(max_price=5000, in_stock=True, language="zh-CN")

    new_topic = resolve_request(
        "换个话题，不沿用刚才的条件：我想找有货的英文 Python 入门书",
        UserIntent(constraints=UserConstraints(language="en", in_stock=True)),
        context,
    )
    assert not new_topic.uses_context
    assert new_topic.constraints.max_price is None
    assert new_topic.constraints.language == "en"


def test_preference_language_is_not_a_hard_catalog_filter():
    effective = resolve_request("我长期偏好英文书，查询B1", UserIntent(), ConversationContext())
    assert effective.constraints.language is None


def test_model_inferred_scalar_filters_are_ignored_without_matching_user_text():
    effective = resolve_request(
        "我刚学完Java，想了解分布式，主要想看懂基础概念",
        UserIntent(constraints=UserConstraints(language="zh-CN", max_price=5000, in_stock=True)),
        ConversationContext(),
    )

    assert effective.constraints.language is None
    assert effective.constraints.max_price is None
    assert effective.constraints.in_stock is None


def test_explicit_latin_author_is_preserved_when_intent_omits_it():
    effective = resolve_request("有没有 Alice 写的中文技术书？", UserIntent(), ConversationContext())

    assert effective.constraints.authors == ["Alice"]
    assert effective.constraints.language == "zh-CN"


def test_explicit_latin_author_adjacent_to_a_language_book_phrase_is_preserved():
    effective = resolve_request("查询Alice中文书的价格和库存", UserIntent(), ConversationContext())

    assert effective.constraints.authors == ["Alice"]
    assert effective.constraints.language == "zh-CN"


def test_deterministic_intent_extracts_hard_conditions_and_stable_question_ids():
    intent = deterministic_intent("推荐三本每本不超过100元且有货的中文分布式系统书")

    assert intent.constraints.max_price == 10_000
    assert intent.constraints.currency == "CNY"
    assert intent.constraints.language == "zh-CN"
    assert intent.constraints.in_stock is True
    assert intent.required_question_ids == ["recommendation"]


def test_learning_how_to_handle_an_engineering_problem_is_not_policy_intent():
    intent = deterministic_intent("我有Java基础，想学如何处理服务雪崩，请推荐适合的书")
    assert intent.required_question_ids == ["recommendation"]
    assert catalog_query_hints("我有Java基础，想学如何处理服务雪崩") == ["分布式系统", "Java"]
    assert catalog_query_hints("想理解消息幂等和重复消费") == ["分布式系统"]
    assert catalog_query_hints("数据库索引优化") == ["数据库"]


def test_pure_inventory_watch_does_not_require_a_recommendation_answer():
    intent = deterministic_intent("书籍 #11 现在缺货，请在库存达到 2 本时提醒我，监听 24 小时。")
    assert intent.required_question_ids == ["inventory_watch"]


def test_out_of_stock_is_not_forced_when_the_user_allows_both_states():
    effective = resolve_request(
        "推荐两本中文分布式书，每本≤60元；缺货的也可以，但要标出来",
        UserIntent(constraints=UserConstraints(language="zh-CN", max_price=6000)),
        ConversationContext(),
    )

    assert effective.constraints.in_stock is None


def test_follow_up_resolves_only_actual_display_order_and_watch_target():
    first = DisplayedBook(book_id=25, isbn="9780000010254", title="分布式系统零基础导读")
    second = DisplayedBook(book_id=26, isbn="9780000010261", title="从Java走进分布式")
    context = ConversationContext(active_request="推荐两本书", displayed_books=[first, second])
    value = resolve_request(
        "刚才推荐的第二本适合没有分布式基础的人吗？",
        UserIntent(),
        context,
    )
    assert value.selected_book == second

    watch = resolve_request(
        "这本补到两本时提醒我，监听24小时",
        UserIntent(),
        ConversationContext(active_request="查询库存", selected_book=second),
    )
    assert watch.inventory_watch is not None
    assert watch.inventory_watch.book_identifier == "26"
    assert watch.inventory_watch.desired_quantity == 2


def test_completed_response_updates_display_only_after_verified_result():
    context = ConversationContext(active_request="旧问题")
    effective = resolve_request(
        "推荐三本",
        UserIntent(constraints=UserConstraints(in_stock=True)),
        context,
    )
    updated = updated_context(
        context,
        "推荐三本",
        "已找到三本。",
        effective,
        {25: _book(25), 26: _book(26, "从Java走进分布式")},
        [25, 26],
    )
    assert [book.book_id for book in updated.displayed_books] == [25, 26]
    assert updated.selected_book is None
    assert updated.turns[-1].request == "推荐三本"


@pytest.mark.asyncio
async def test_older_context_generation_cannot_overwrite_newer_completed_run():
    store = InMemoryConversationStore()
    await store.bind("page-1", 42)
    older = await store.begin_context("page-1", 42)
    newer = await store.begin_context("page-1", 42)
    current = ConversationContext(active_request="newer")
    assert await store.commit_context("page-1", 42, newer.generation, current)
    assert not await store.commit_context("page-1", 42, older.generation, ConversationContext(active_request="older"))
    final = await store.begin_context("page-1", 42)
    assert final.context.active_request == "newer"
