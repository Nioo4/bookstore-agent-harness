from app.api.conversation_context import deterministic_intent


def test_buying_an_ordinal_is_not_misclassified_as_comparison() -> None:
    intent = deterministic_intent("就买第二本，请生成订单预览，但不要替我确认。")
    assert intent.required_question_ids == ["recommendation"]


def test_ordinal_comparison_remains_a_comparison() -> None:
    intent = deterministic_intent("第二本和第一本有什么区别？")
    assert "comparison" in intent.required_question_ids
