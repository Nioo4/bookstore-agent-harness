from __future__ import annotations

import re


_CATALOG_PATTERNS = (
    r"实时",
    r"当前",
    r"现在",
    r"价格",
    r"多少钱",
    r"库存",
    r"有货",
    r"缺货",
    r"\bprice\b",
    r"\bstock\b",
    r"\bavailability\b",
    r"\bisbn\b",
    r"(?:补货|到货).{0,24}?(?:监听|提醒|通知)",
)

# These are requests whose useful answer necessarily depends on the current
# catalog, even when the user did not explicitly say "price" or "stock".
# Keep this narrow so preference and policy-only messages retain their routes.
_BOOK_REQUEST_PATTERNS = (
    r"^推荐",
    r"^找",
    r"^查(?:询)?",
    r"^有没有",
    r"^我想买",
    r"比较",
    r"适合",
    r"章节",
    r"我想找",
    r"《[^》]+》",
    r"\brecommend\b",
    r"\bfind\b",
    r"\bcompare\b",
    r"\bbuy\b",
)

_POLICY_PATTERNS = (
    r"配送",
    r"物流",
    r"退款",
    r"退货",
    r"换货",
    r"取消订单",
    r"自动下单",
    r"补货(?:监听)?(?:规则|如何|怎么|吗|是否)",
    r"长期记忆",
    r"删除.*记忆",
    r"\bshipping\b",
    r"\bdelivery\b",
    r"\brefund",
    r"\breturn",
    r"\bautomatic(?:ally)? order",
    r"\blong[- ]term memory\b",
)

_DIRECT_WATCH_PATTERNS = (
    r"(?:创建|设置|开启|帮我|为我|请为).{0,40}?(?:补货|补到|到货|库存).{0,40}?(?:监听|提醒|通知)",
    r"(?:补货|补到|到货|库存).{0,40}?(?:提醒我|通知我|监听)",
    r"(?:这本|它).{0,16}?(?:提醒我|通知我|监听)",
)


def required_agents_for_request(user_request: str) -> frozenset[str]:
    normalized = user_request.casefold()
    required: set[str] = set()
    direct_watch = any(re.search(pattern, normalized) for pattern in _DIRECT_WATCH_PATTERNS)
    if any(re.search(pattern, normalized) for pattern in (*_CATALOG_PATTERNS, *_BOOK_REQUEST_PATTERNS)):
        required.add("catalog")
    # A creation request may say “do not order automatically” as an action
    # constraint. It still needs current catalog facts and a confirmation card,
    # not a policy lookup. Policy questions about the existing rule retain the
    # knowledge route.
    if not direct_watch and any(re.search(pattern, normalized) for pattern in _POLICY_PATTERNS):
        required.add("knowledge")
    return frozenset(required)
