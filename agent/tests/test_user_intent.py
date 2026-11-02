import pytest
from pydantic import ValidationError

from app.agents.intent import InventoryWatchIntent, UserIntent, infer_inventory_watch, watch_books


BOOK = {"book_id": 11, "isbn": "9780000000217", "title": "到货通知练习册", "available_stock": 0, "status": "ACTIVE"}


@pytest.mark.parametrize("identifier", ["11", "9780000000217", "到货通知练习册", None])
def test_watch_uses_authoritative_unique_book_and_requested_threshold(identifier):
    intent = InventoryWatchIntent(book_identifier=identifier, desired_quantity=2, duration_seconds=86400)
    assert watch_books({11: BOOK}, intent) == [BOOK]
    assert watch_books({11: {**BOOK, "available_stock": 1}}, intent)
    assert watch_books({11: {**BOOK, "available_stock": 2}}, intent) == []
    assert watch_books({11: {**BOOK, "status": "INACTIVE"}}, intent) == []


def test_watch_does_not_guess_ambiguous_or_unavailable_target():
    assert watch_books({11: BOOK}, InventoryWatchIntent(book_identifier="missing")) == []
    assert watch_books({11: BOOK, 12: {**BOOK, "book_id": 12}}, InventoryWatchIntent()) == []
    assert UserIntent().constraints.max_price is None


def test_explicit_watch_parser_keeps_identifier_threshold_and_duration():
    intent = infer_inventory_watch("请为 ISBN 9780000000217 创建补货监听：库存达到2本时通知我，监听24小时。只监听，不自动下单。")
    assert intent == InventoryWatchIntent(book_identifier="9780000000217", desired_quantity=2, duration_seconds=86400)
    assert infer_inventory_watch("不要创建补货监听，只问规则。") is None
    assert infer_inventory_watch("查询《到货通知练习册》的库存") is None


def test_explicit_watch_parser_accepts_hash_book_id():
    intent = infer_inventory_watch("书籍 #11 现在缺货，请在库存达到 2 本时提醒我，监听 24 小时。")
    assert intent == InventoryWatchIntent(book_identifier="11", desired_quantity=2, duration_seconds=86400)


@pytest.mark.parametrize("value", [{"desired_quantity": 0}, {"desired_quantity": 100}, {"duration_seconds": 3599}, {"duration_seconds": 7776001}])
def test_watch_intent_bounds(value):
    with pytest.raises(ValidationError):
        InventoryWatchIntent(**value)
