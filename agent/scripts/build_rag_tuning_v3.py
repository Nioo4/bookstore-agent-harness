"""Create the reviewed 24-case RAG v3 tuning split.

V3 keeps the original corpus balance while replacing redundant cases with
correctly labeled Python topics and policy boundaries exposed by the v2 audit.
"""
from __future__ import annotations

import json
from pathlib import Path


def case(case_id: str, corpus: str, query: str, docs: list[str], sections: list[str], refs: list[str] | None = None, abstain: bool = False) -> dict:
    return {"case_id": case_id, "corpus": corpus, "query": query, "catalog_filters": {}, "expected_document_ids": docs,
            "expected_sections": sections, "allowed_catalog_refs": list(refs or []), "forbidden_catalog_refs": [], "must_abstain": abstain}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    original = [
        json.loads(line)
        for line in (root / "evals/rag-v2/tuning.jsonl").read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    retained = [row for row in original if row["case_id"] not in {
        "rag-tune-book-004", "rag-tune-book-006", "rag-tune-book-007", "rag-tune-book-008",
        "rag-tune-policy-003", "rag-tune-policy-005", "rag-tune-policy-009", "rag-tune-policy-011",
    }]
    additions = [
        case("rag-v3-tune-book-001", "BOOK_GUIDE", "Python协程和异步HTTP请求实践", ["book-guide-b54"], ["核心主题"], ["B54"]),
        case("rag-v3-tune-book-002", "BOOK_GUIDE", "清理缺失值并转换表格数据", ["book-guide-b55"], ["核心主题"], ["B55"]),
        case("rag-v3-tune-book-003", "BOOK_GUIDE", "用断言fixture和隔离编写Python自动化测试", ["book-guide-b56"], ["核心主题"], ["B56"]),
        case("rag-v3-tune-book-004", "BOOK_GUIDE", "定位Python服务的CPU热点和性能瓶颈", ["book-guide-b57"], ["核心主题"], ["B57"]),
        case("rag-v3-tune-policy-001", "POLICY", "怎样一次删除我保存的全部阅读偏好", ["policy-agent-memory"], ["删除长期记忆"]),
        case("rag-v3-tune-policy-002", "POLICY", "模拟购买之后系统会自动退款吗", ["policy-demo-scope"], ["退款退货与换货"]),
        case("rag-v3-tune-policy-003", "POLICY", "Agent能否自己发明工具或扩大工具权限", ["policy-agent-memory"], ["工具权限与注册表"]),
        case("rag-v3-tune-policy-004", "POLICY", "请介绍本系统没有定义的会员等级权益", [], [], abstain=True),
    ]
    rows = retained + additions
    if len(rows) != 24 or sum(row["corpus"] == "BOOK_GUIDE" for row in rows) != 12:
        raise RuntimeError("v3 tuning must remain 12 BOOK_GUIDE + 12 POLICY cases")
    target = root / "evals/rag-v3/tuning.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
