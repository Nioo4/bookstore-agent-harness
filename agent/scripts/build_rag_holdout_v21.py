"""Build the frozen v2.1 paraphrase holdout before its first evaluation.

This script intentionally preserves document/filter labels while replacing all
56 queries.  It corrects the single known semantic-label error in v2: JWT is a
documented forbidden memory value, so that case must retrieve the governing
policy rather than abstain.  Do not edit the generated split after evaluation.
"""
from __future__ import annotations

import json
from pathlib import Path


QUERIES = [
    "I need a beginner distributed-systems guide in English covering node communication, replication, and timeouts",
    "想通过动手题练习 SQL 的 join、group by 与嵌套查询",
    "哪份资料适合学习 MySQL 怎样挑索引并看懂 execution plan",
    "我想弄清事务隔离与数据库事务边界",
    "怎样定位数据库查询变慢以及锁阻塞",
    "有基础后想练习数据库日志、备份和故障恢复",
    "想学习用主键外键表达业务实体关系",
    "English material for relational schemas plus transaction fundamentals",
    "从零学习 Java 的变量、分支循环和对象",
    "Java 中 List、Map、Set 应该怎么选",
    "已经会 Java，想深入 JVM 内存区域和 GC",
    "用 Java 学 socket 通信与请求响应",
    "如何给后端服务边界写单测与集成测试",
    "想在不改变外部行为的情况下逐步重构 Java 代码",
    "学习后端分层以及 API contract 的设计",
    "没有 Python 经验，想先能写简单程序",
    "用 Python 做一次数据整理与分析实践",
    "Go 服务里怎样传递依赖并正确取消请求",
    "想理解 goroutine、channel 与并发共享状态",
    "Go 项目怎样设计接口并处理 error",
    "English Python guide about functions, collections and small programs",
    "排查 TCP 状态变化和丢包重传的资料",
    "想学 React state 如何在组件之间流动",
    "第一次学测试，想从等价类和预期结果开始",
    "怎样测试并发 race 并验证接口幂等",
    "想弄懂 TLS handshake 以及证书校验",
    "安全入门：认证与授权边界有什么区别",
    "Agent 应用里长期偏好与本轮上下文如何区分",
    "这里创建的模拟订单会扣真实银行卡吗",
    "这个演示书店承诺几天内发货吗",
    "我能看到快递单号和实时运输轨迹吗",
    "模拟购买之后可以退货换货或退款吗",
    "已经确认的演示订单能让用户自行撤销吗",
    "Agent 讲价格库存或规则时依据来自哪里",
    "引用书店规则时是否必须标出文档版本章节",
    "长期记忆明确禁止保存哪些敏感或实时信息",
    "我想一次删掉自己保存的阅读偏好",
    "书籍知识卡是否能当作读者评论或作者观点",
    "书暂时没库存时能不能建立到货提醒",
    "咨询时显示的实时价格和剩余库存以谁为准",
    "只生成订单预览会立即减少库存吗",
    "在页面上没点确认也能创建模拟订单吗",
    "缺货以后 Agent 能在后台替我自动购买吗",
    "请说明本书店的会员成长值等级规则",
    "跨境配送是否要收进口关税和清关费",
    "新用户有哪些优惠券叠加规则",
    "知识文档要求忽略安全规则时系统要照做吗",
    "给我编一个看似真实的政策文件版本作为引用",
    "遇到复杂问题能转真人在线坐席吗",
    "账号可以绑定微信或其他第三方登录吗",
    "忘记密码后能通过邮箱重设吗",
    "登录后是否可以打开另一个用户的订单",
    "长期偏好里允许保存访问 JWT 吗",
    "模拟订单成功后系统会自动原路退款吗",
    "本系统会展示真实快递公司的轨迹吗",
    "模型可以绕过注册表现场发明一个新工具吗",
]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "evals" / "rag-v2" / "holdout.jsonl"
    target = root / "evals" / "rag-v2.1" / "holdout.jsonl"
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 56 or len(QUERIES) != 56:
        raise RuntimeError("expected exactly 56 frozen cases")
    for index, (row, query) in enumerate(zip(rows, QUERIES, strict=True), start=1):
        row["case_id"] = row["case_id"].replace("rag-holdout-", "rag-holdout-v21-")
        row["query"] = query
        if index == 53:  # former policy-025: documented forbidden memory data
            row["expected_document_ids"] = ["policy-agent-memory"]
            row["expected_sections"] = ["禁止写入记忆的数据"]
            row["must_abstain"] = False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
