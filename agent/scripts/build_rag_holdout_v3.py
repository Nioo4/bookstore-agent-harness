"""Build the independently reviewed RAG v3 holdout split.

The cases below use catalog refs verified against manual-v1.json and policy
section names verified against knowledge/policies.  Do not edit the generated
split after its first real-provider evaluation; publish a new dataset version.
"""
from __future__ import annotations

import json
from pathlib import Path


def book(case: int, query: str, ref: str, sections: list[str], *, filters: dict | None = None,
         forbidden: list[str] | None = None) -> dict:
    return {
        "case_id": f"rag-v3-holdout-book-{case:03d}", "corpus": "BOOK_GUIDE", "query": query,
        "catalog_filters": filters or {}, "expected_document_ids": [f"book-guide-{ref.lower()}"],
        "expected_sections": sections, "allowed_catalog_refs": [ref],
        "forbidden_catalog_refs": forbidden or [], "must_abstain": False,
    }


def policy(case: int, query: str, document: str | None, sections: list[str]) -> dict:
    return {
        "case_id": f"rag-v3-holdout-policy-{case:03d}", "corpus": "POLICY", "query": query,
        "catalog_filters": {}, "expected_document_ids": [document] if document else [],
        "expected_sections": sections, "allowed_catalog_refs": [], "forbidden_catalog_refs": [],
        "must_abstain": document is None,
    }


def main() -> None:
    rows = [
        book(1, "我想系统理解缓存更新、过期和一致性问题", "B29", ["核心主题"], filters={"language": "zh-CN"}),
        book(2, "注册中心怎样做服务注册、发现和健康检查", "B31", ["核心主题"]),
        book(3, "想深入学习共识协议以及状态机复制", "B32", ["核心主题"], forbidden=["B25"]),
        book(4, "跨服务链路追踪和故障定位有什么实践书", "B33", ["核心主题"]),
        book(5, "怎样估算分布式系统容量和资源上限", "B34", ["核心主题"]),
        book(6, "通过故障注入验证系统恢复能力", "B35", ["核心主题", "学习成果"]),
        book(7, "需要英文的分布式系统零基础读物", "B36", ["适读人群", "核心主题"], filters={"language": "en"}),
        book(8, "练习SQL连接、聚合以及子查询", "B38", ["核心主题"]),
        book(9, "数据库隔离级别和事务边界入门", "B40", ["核心主题", "适读人群"]),
        book(10, "排查慢查询和锁等待，需要进阶资料", "B41", ["核心主题", "前置知识"]),
        book(11, "学习数据库日志、备份与恢复演练", "B42", ["核心主题"]),
        book(12, "主键外键和业务关系应该怎样建模", "B43", ["核心主题"]),
        book(13, "Java列表映射和集合类型如何选择", "B46", ["核心主题"]),
        book(14, "想了解JVM内存布局和垃圾回收", "B48", ["核心主题"], forbidden=["B45"]),
        book(15, "用Java练习套接字与请求响应", "B49", ["核心主题"]),
        book(16, "Java后端服务边界的单元和集成测试", "B50", ["核心主题"]),
        book(17, "如何小步重构同时保持程序行为", "B51", ["核心主题"]),
        book(18, "Go类型、函数和接口的零基础中文资料", "B59", ["核心主题", "适读人群"], filters={"language": "zh-CN"}),
        book(19, "goroutine和channel适合初学者的书", "B60", ["核心主题", "适读人群"]),
        book(20, "Go服务接口和超时传播实践", "B61", ["核心主题"]),
        book(21, "Go程序CPU与内存开销怎么剖析", "B62", ["核心主题"]),
        book(22, "Go表驱动测试和依赖替代", "B63", ["核心主题"]),
        book(23, "TCP重传、拥塞和连接状态故障排查", "B67", ["核心主题"]),
        book(24, "TLS握手与证书验证实践", "B68", ["核心主题"]),
        book(25, "React组件状态和数据流实践", "B72", ["核心主题"]),
        book(26, "网页键盘操作与语义结构的可访问性", "B74", ["核心主题"]),
        book(27, "测试并发竞态和幂等性", "B79", ["核心主题"]),
        book(28, "Agent工具Schema和参数校验实践", "B96", ["核心主题"]),
        policy(1, "这是实际付款的订单吗", "policy-demo-scope", ["真实支付"]),
        policy(2, "能承诺几天内发货送到吗", "policy-demo-scope", ["真实发货与配送时间"]),
        policy(3, "下单后在哪里看快递单号和物流轨迹", "policy-demo-scope", ["物流单号与轨迹"]),
        policy(4, "这个演示订单支持退货换货和退款吗", "policy-demo-scope", ["退款退货与换货"]),
        policy(5, "已经创建的模拟订单能由用户取消吗", "policy-demo-scope", ["用户取消订单"]),
        policy(6, "书一到货可以不问我就自动购买吗", "policy-demo-scope", ["自动下单"]),
        policy(7, "书没库存时系统能怎样处理", "policy-demo-scope", ["缺货处理"]),
        policy(8, "Agent回答是否保证永远完全正确", "policy-demo-scope", ["Agent 准确性"]),
        policy(9, "确认按钮点下以前会创建订单或扣库存吗", "policy-order-inventory", ["模拟订单", "订单确认"]),
        policy(10, "预览订单时价格与库存是否会重新读取", "policy-order-inventory", ["订单预览", "商品事实来源"]),
        policy(11, "确认订单遇到价格变化会怎样", "policy-order-inventory", ["订单确认"]),
        policy(12, "补货监听会预留库存或者自动购买吗", "policy-order-inventory", ["补货监听"]),
        policy(13, "书籍价格和库存应该以知识卡还是业务系统为准", "policy-order-inventory", ["商品事实来源"]),
        policy(14, "Agent怎样引用政策规则", "policy-agent-memory", ["规则回答与引用"]),
        policy(15, "模型可以绕开服务端确认门直接写订单吗", "policy-agent-memory", ["工具权限与注册表"]),
        policy(16, "系统允许Agent临时创造一个新工具吗", "policy-agent-memory", ["工具权限与注册表"]),
        policy(17, "我可以明确保存哪些语言主题或作者偏好", "policy-agent-memory", ["可保存的阅读偏好"]),
        policy(18, "删除某条长期偏好后还会用于推荐吗", "policy-agent-memory", ["删除长期记忆"]),
        policy(19, "完整地址和实时订单状态会保存进长期记忆吗", "policy-agent-memory", ["禁止写入记忆的数据"]),
        policy(20, "书籍知识卡能证明实时售价吗", "policy-agent-memory", ["知识卡边界"]),
        policy(21, "Agent回答书籍库存时事实从哪里获取", "policy-agent-memory", ["Agent 回答与事实"]),
        policy(22, "会员生日礼券有什么规则", None, []),
        policy(23, "积分兑换比例是多少", None, []),
        policy(24, "是否提供线下门店自提", None, []),
        policy(25, "企业采购可以开增值税专票吗", None, []),
        policy(26, "有学生认证折扣吗", None, []),
        policy(27, "客服人工坐席工作时间是什么", None, []),
        policy(28, "是否支持礼品包装和贺卡", None, []),
    ]
    if len(rows) != 56 or len({row["case_id"] for row in rows}) != 56:
        raise RuntimeError("RAG v3 holdout must contain 56 unique cases")
    target = Path(__file__).resolve().parents[1] / "evals" / "rag-v3" / "holdout.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError(f"refusing to overwrite frozen holdout: {target}")
    target.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
