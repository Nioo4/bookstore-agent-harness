from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _fixture_module():
    path = Path(__file__).resolve().parents[2] / "deploy" / "model_fixture.py"
    spec = importlib.util.spec_from_file_location("bookstore_model_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_consumes_current_local_tool_result_protocol() -> None:
    fixture = _fixture_module()
    messages = [
        {"role": "system", "content": "ALLOWED_TOOLS_JSON=" + json.dumps([
            {"name": "get_book"}, {"name": "search_books"}
        ])},
        {"role": "system", "content": "EFFECTIVE_REQUEST_JSON=" + json.dumps({
            "required_question_ids": ["recommendation"]
        })},
        {"role": "user", "content": "查看书籍 101 当前库存"},
        {"role": "assistant", "content": '{"action":"CALL_TOOLS"}'},
        {"role": "system", "content": "LOCAL_TOOL_RESULT_JSON=" + json.dumps({
            "tool": "get_book",
            "evidence_ids": ["book:101:v1"],
            "value": {"book_id": 101, "title": "Fixture Book", "price": 4200,
                      "currency": "CNY", "available_stock": 8},
        })},
    ]

    output = fixture.output_for("SingleAgentAction", messages)

    assert output["action"] == "FINAL"
    assert output["recommendations"][0]["book_id"] == 101
