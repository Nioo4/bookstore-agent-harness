import pytest
from app.harness.catalog_fact import project_catalog_fact
from app.harness.single_agent import SingleAgentAction
from app.harness.validator import validate_final_action

from app.api.conversation_context import deterministic_intent
from app.api.chat import ChatService


@pytest.mark.parametrize("question", [
    "请查询ISBN为9780000000019的书，告诉我价格和当前库存。",
    "ISBN 9780000000019 多少钱，有货吗？",
])
def test_exact_catalog_query_is_not_a_recommendation(question):
    assert deterministic_intent(question).required_question_ids == ["catalog_fact"]
    assert ChatService._allowed_tools_for_request(["catalog_fact"], authenticated=True) == {"search_books", "get_book"}


@pytest.mark.parametrize("question", [
    "推荐两本林甲写的中文分布式书，有货，每本50元以内。",
    "ISBN为9780000000019的书价格多少，适合初学者吗？",
    "查询ISBN 9780000000019价格，并推荐类似的书。",
])
def test_recommendation_and_semantic_questions_keep_knowledge_validation(question):
    labels = deterministic_intent(question).required_question_ids
    assert "recommendation" in labels
    assert "catalog_fact" not in labels


def test_unsolicited_recommendation_cannot_hide_verified_price_and_stock():
    action = SingleAgentAction(action="FINAL", answer="错误价格999元，适合所有人", recommendations=[
        {"book_id": 1, "reason": "适合所有人", "evidence_ids": ["book:1:v1"]}
    ])
    results = [{"tool": "get_book", "value": {"book_id": 1, "isbn": "9780000000019", "price": 3000,
               "currency": "CNY", "available_stock": 5}, "evidence_ids": ["book:1:v1"]}]
    assert validate_final_action(action, results)  # actual recommendations still need knowledge
    project_catalog_fact(action, results, "ISBN 9780000000019价格和库存", ["recommendation"])
    assert action.recommendations
    project_catalog_fact(action, results, "ISBN 9780000000019价格和库存", ["catalog_fact"])
    assert not action.recommendations
    assert "30.00 元" in action.answer and "5 本" in action.answer
    assert "999" not in action.answer and "适合" not in action.answer
    assert not validate_final_action(action, results, required_question_ids=["catalog_fact"])


@pytest.mark.parametrize("value", [{}, {"isbn": "other"}, {"isbn": "9780000000019", "price": "3000"}])
def test_no_valid_catalog_evidence_cannot_create_facts(value):
    action = SingleAgentAction(action="FINAL", answer="没有依据")
    project_catalog_fact(action, [{"tool": "get_book", "value": value}], "ISBN 9780000000019价格库存", ["catalog_fact"])
    assert action.answer == "没有依据"
