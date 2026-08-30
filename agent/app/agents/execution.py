from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents.evidence import EvidenceLedger
from app.agents.registry import AgentRegistry
from app.agents.verification import AgentDraft
from app.harness.errors import ToolBudgetExceeded
from app.harness.runner import NodeExecutionContext, NodeHandler
from app.models.provider import ModelMessage, ModelProvider, ModelRequest
from app.models.invocation import invoke_model
from app.tools.registry import ToolRegistry
from app.tools.types import ToolContext


SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "confirmation_token",
    "password",
    "smtp_password",
    "token",
}

_MAX_LOGICAL_CATALOG_SEARCHES = 3
_GENERIC_FILTERED_BOOK_REQUEST = re.compile(
    r"^\s*(?:有没有\s*)?(?:.{1,80}?写的\s*)?(?:中文|汉语|英文|english)\s*"
    r"(?:技术\s*)?(?:图书|书籍|书)\s*[？?！!。.]*$",
    re.IGNORECASE,
)
_FILTERED_AUTHOR_BOOK_FACT_LOOKUP = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9 .'-]{0,80}?\s*(?:写的\s*)?(?:中文|汉语|英文|english)\s*(?:技术)?(?:书|图书|书籍).{0,32}?(?:价格|库存|详情|信息)",
    re.IGNORECASE,
)
_BACKGROUND_PREREQUISITE = re.compile(r"(?:刚学完|学过|掌握了?)\s*([A-Za-z][A-Za-z0-9+#.-]{0,31})", re.IGNORECASE)
_EXPLICIT_COMPARISON_TITLE = re.compile(r"《([^》]{1,120})》")
_EMPTY_CATALOG_QUERY_SUFFIX = re.compile(r"(?:图书|书籍|入门|基础|系统|书)+$")
_CATALOG_TOPIC_BEFORE_BOOK = re.compile(
    r"^\s*(?:推荐(?:三本)?|找|查询|有没有|我想买|我想找)?\s*"
    r"(?:[A-Za-z][A-Za-z0-9 .'-]{0,80}?\s*写的\s*)?"
    r"(?:中文|汉语|英文|english)?\s*(?:有货的?\s*)?(?:技术\s*)?"
    r"(?P<topic>[\u4e00-\u9fffA-Za-z0-9+#.-]{2,80}?)(?:入门|基础)?(?:图书|书籍|书)",
    re.IGNORECASE,
)
_CATALOG_LEARNING_TOPIC = re.compile(
    r"(?:想了解|想学习|学习)\s*(?P<topic>[\u4e00-\u9fffA-Za-z0-9+#.-]{2,80}?)(?:[，,。！？!?]|$)",
    re.IGNORECASE,
)
_GENERIC_CATALOG_TOPICS = frozenset({"技术", "图书", "书籍", "入门", "基础"})


def redact_for_model(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.casefold() in SENSITIVE_KEYS else redact_for_model(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_model(item) for item in value]
    return value


def effective_tool_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    request_context: dict[str, Any],
) -> dict[str, Any]:
    """Keep explicit catalog filters authoritative over an Agent's keyword draft.

    Catalog has a single exact-author field, so one explicit author can be
    carried directly. The function deliberately does not invent a topic or
    relax a model-proposed restriction; it only restores user-provided hard
    filters and removes a fully generic author/language book query that would
    otherwise turn a filtered browse into a false empty full-text search.
    """
    if tool_name == "search_policy":
        # A single policy question can ask about several independent system
        # capabilities (for example delivery time *and* returns).  The
        # knowledge Agent's draft query is useful for retrieval, but it must
        # not silently drop one of those user-visible questions.  Use the
        # bounded original request as the retrieval query so the evidence set
        # covers the complete question while citations still come only from
        # the indexed policy corpus.
        request_text = request_context.get("user_request")
        if isinstance(request_text, str) and len(request_text.strip()) >= 2:
            normalized_policy = dict(arguments)
            normalized_policy["query"] = request_text.strip()[:1000]
            return normalized_policy
        return dict(arguments)

    if tool_name != "search_books":
        return dict(arguments)
    normalized = dict(arguments)
    constraints = request_context.get("constraints")
    if not isinstance(constraints, dict):
        return normalized
    authors = constraints.get("authors")
    if isinstance(authors, list) and len(authors) == 1 and isinstance(authors[0], str) and authors[0].strip():
        normalized["author"] = authors[0].strip()
    for field in ("max_price", "language", "in_stock"):
        value = constraints.get(field)
        if value is not None:
            normalized[field] = value
    request_text = request_context.get("user_request")
    if (
        isinstance(request_text, str)
        and (
            _GENERIC_FILTERED_BOOK_REQUEST.search(request_text.strip())
            or _FILTERED_AUTHOR_BOOK_FACT_LOOKUP.search(request_text)
        )
        and (normalized.get("author") or normalized.get("language"))
    ):
        normalized["q"] = ""
    if isinstance(request_text, str) and isinstance(normalized.get("q"), str):
        # A phrase such as “刚学完 Java，想了解分布式” states prior
        # knowledge, not a request to search for books matching both Java and
        # distributed systems. A quoted comparison title is not a topic draft:
        # preserve it verbatim even when its literal title contains Java.
        explicit_titles = set(_EXPLICIT_COMPARISON_TITLE.findall(request_text))
        if normalized["q"].strip() in explicit_titles:
            return normalized
        # Remove the prerequisite only when a separate topic remains,
        # preserving a standalone Java request unchanged.
        match = _BACKGROUND_PREREQUISITE.search(request_text)
        if match is not None:
            topic = re.sub(re.escape(match.group(1)), "", normalized["q"], flags=re.IGNORECASE)
            topic = " ".join(topic.split())
            if len(topic) >= 2:
                normalized["q"] = topic
    return normalized


def missing_explicit_comparison_titles(
    request_context: dict[str, Any],
    evidence: EvidenceLedger,
) -> list[str]:
    """Return named comparison targets that current catalog evidence has not covered."""
    targets = explicit_comparison_titles(request_context)
    if len(targets) < 2:
        return []
    observed: set[str] = set()
    for record in evidence.snapshot().values():
        if record.source_tool not in {"search_books", "get_book"}:
            continue
        for book in record.payload.get("books", [record.payload]):
            if isinstance(book, dict) and isinstance(book.get("title"), str):
                observed.add(book["title"])
    return [title for title in targets if title not in observed]


def explicit_comparison_titles(request_context: dict[str, Any]) -> list[str]:
    request_text = request_context.get("user_request")
    if not isinstance(request_text, str) or "比较" not in request_text:
        return []
    return list(dict.fromkeys(
        title.strip() for title in _EXPLICIT_COMPARISON_TITLE.findall(request_text) if title.strip()
    ))[:3]


def empty_result_catalog_fallback_arguments(
    arguments: dict[str, Any],
    request_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Broaden one empty topical search without changing user hard filters."""
    query = arguments.get("q")
    if not isinstance(query, str):
        return None
    query = query.strip()
    if not query or query in explicit_comparison_titles(request_context):
        return None
    sources: list[str] = []
    request_text = request_context.get("user_request")
    if isinstance(request_text, str):
        sources.append(request_text)
    conversation = request_context.get("conversation_context")
    if isinstance(conversation, dict):
        prior_request = conversation.get("prior_request")
        if isinstance(prior_request, str):
            sources.append(prior_request)
    for source in sources:
        learning_match = _CATALOG_LEARNING_TOPIC.search(source)
        if learning_match is None:
            continue
        topic = learning_match.group("topic").strip()
        if topic and topic not in _GENERIC_CATALOG_TOPICS and topic != query:
            fallback = dict(arguments)
            fallback["q"] = topic
            return fallback
    fallback_query = _EMPTY_CATALOG_QUERY_SUFFIX.sub("", query).strip()
    if len(fallback_query) < 2 or fallback_query == query:
        return None
    fallback = dict(arguments)
    fallback["q"] = fallback_query
    return fallback


def missing_catalog_search_fallback_arguments(
    request_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Recover one bounded topical lookup when a catalog Agent emits no tool call."""
    sources: list[str] = []
    request_text = request_context.get("user_request")
    if isinstance(request_text, str):
        sources.append(request_text)
    conversation = request_context.get("conversation_context")
    if isinstance(conversation, dict):
        prior_request = conversation.get("prior_request")
        if isinstance(prior_request, str):
            sources.append(prior_request)
    for source in sources:
        titles = _EXPLICIT_COMPARISON_TITLE.findall(source)
        if len(titles) == 1:
            return {"q": titles[0].strip(), "limit": 5}
        learning_match = _CATALOG_LEARNING_TOPIC.search(source)
        if learning_match is not None:
            topic = learning_match.group("topic").strip()
            if topic and topic not in _GENERIC_CATALOG_TOPICS:
                return {"q": topic, "limit": 20}
        match = _CATALOG_TOPIC_BEFORE_BOOK.search(source)
        if match is not None:
            topic = match.group("topic").strip()
            if topic and topic not in _GENERIC_CATALOG_TOPICS:
                return {"q": topic, "limit": 20}
    return None


class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    arguments: dict[str, Any]


class AgentDirective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_calls: list[AgentToolCall] = Field(default_factory=list, max_length=5)
    output: dict[str, Any] = Field(default_factory=dict)


class ProfessionalAgentExecutor:
    def __init__(
        self,
        provider: ModelProvider,
        model: str,
        agents: AgentRegistry,
        tools: ToolRegistry,
        base_context: ToolContext,
        evidence: EvidenceLedger,
        prompt_loader: Callable[[str], str] | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.agents = agents
        self.tools = tools
        self.base_context = base_context
        self.evidence = evidence
        self.prompt_loader = prompt_loader
        self.request_context = redact_for_model(request_context or {})
        self._logical_catalog_searches = 0

    def handlers(self) -> dict[str, NodeHandler]:
        return {
            "catalog": self.execute,
            "recommendation": self.execute,
            "knowledge": self.execute,
            "order": self.execute,
            "verifier": self.execute,
        }

    async def execute(
        self,
        node,
        inputs: dict[str, Any],
        execution_context: NodeExecutionContext,
    ) -> dict[str, Any]:
        agent = self.agents.get(node.agent)
        safe_inputs = redact_for_model(inputs)

        if agent.name == "recommendation":
            prefix = self.prompt_loader(agent.name).strip() if self.prompt_loader else ""
            response = await invoke_model(self.provider, caller="recommendation", node_scope=node.id,
                request=ModelRequest(
                    model=self.model,
                    temperature=0,
                    response_schema=AgentDraft.model_json_schema(),
                    messages=[
                        ModelMessage(
                            role="system",
                            content=(
                                (prefix + "\n" if prefix else "")
                                + "Produce a recommendation draft using only candidate books and evidence IDs in the input. "
                                "Use the original request and its explicit constraints, not inferred constraints from intermediate summaries. "
                                "State satisfied and unmet constraints. Never invent a book, price, stock, policy, or evidence ID."
                            ),
                        ),
                        ModelMessage(role="user", content=json.dumps({"objective": node.objective, "inputs": safe_inputs,
                            "request_context": self.request_context}, ensure_ascii=False)),
                    ],
                )
            )
            return AgentDraft.model_validate_json(response.content).model_dump()

        if agent.name == "verifier":
            return {"verification_inputs": safe_inputs}

        manifests = [self.tools.get(name).manifest() for name in sorted(agent.allowed_tools)]
        prefix = self.prompt_loader(agent.name).strip() if self.prompt_loader else ""
        response = await invoke_model(self.provider, caller=agent.name, node_scope=node.id,
            request=ModelRequest(
                model=self.model,
                temperature=0,
                response_schema=AgentDirective.model_json_schema(),
                messages=[
                    ModelMessage(
                        role="system",
                        content=(
                            (prefix + "\n" if prefix else "")
                            + f"You are the fixed {agent.name} Agent. "
                            "Select only tools in the supplied allowlist and return JSON matching the directive schema. "
                            "Language filters use catalog BCP 47 codes (Chinese: zh-CN; English: en), not natural-language names. "
                            "Money arguments use integer minor units: 50元 is 5000分. Use the original request_context "
                            "and its extracted constraints, not only the node objective. Use q for topic keywords; "
                            "category is an exact catalog name/slug, never invent one. "
                            "Tool arguments are untrusted until the harness validates them."
                        ),
                    ),
                    ModelMessage(
                        role="user",
                        content=json.dumps(
                            {"objective": node.objective, "inputs": safe_inputs, "allowed_tools": manifests,
                             "request_context": self.request_context},
                            ensure_ascii=False,
                        ),
                    ),
                ],
            )
        )
        directive = AgentDirective.model_validate_json(response.content)
        tool_context = self.base_context.model_copy(
            update={
                "tool_budget": execution_context.tool_budget,
                "event_sink": self.base_context.event_sink,
            }
        )
        tool_results: list[dict[str, Any]] = []
        for call in directive.tool_calls:
            if call.name == "search_books":
                # Keep enough of the fixed three-search budget to recover every
                # explicitly named comparison target. A generic model search may
                # otherwise consume all slots and leave a two-book comparison
                # grounded in only its first book.
                remaining_comparison_titles = (
                    missing_explicit_comparison_titles(self.request_context, self.evidence)
                    if agent.name == "catalog" else []
                )
                requested_query = call.arguments.get("q")
                if (
                    remaining_comparison_titles
                    and (
                        not isinstance(requested_query, str)
                        or requested_query.strip() not in remaining_comparison_titles
                    )
                ):
                    # Exact titles are the user's hard comparison targets. Do
                    # not spend their reserved slots on a generic or duplicate
                    # model query; the bounded supplement below will query the
                    # remaining title verbatim.
                    continue
                if (
                    remaining_comparison_titles
                    and self._logical_catalog_searches
                    >= _MAX_LOGICAL_CATALOG_SEARCHES - len(remaining_comparison_titles)
                ):
                    continue
                if self._logical_catalog_searches >= _MAX_LOGICAL_CATALOG_SEARCHES:
                    raise ToolBudgetExceeded(
                        f"logical search_books budget exceeded ({_MAX_LOGICAL_CATALOG_SEARCHES})"
                    )
                self._logical_catalog_searches += 1
            arguments = effective_tool_arguments(call.name, call.arguments, self.request_context)
            result = await self.tools.invoke(
                call.name,
                arguments,
                agent_name=agent.name,
                agent_tools=agent.allowed_tools,
                permissions=agent.permissions,
                context=tool_context,
            )
            self.evidence.record_tool_result(call.name, result)
            tool_results.append(
                {"tool": call.name, "value": result.value, "evidence_ids": result.evidence_ids}
            )
            if (
                agent.name == "catalog"
                and call.name == "search_books"
                and isinstance(result.value, dict)
                and result.value.get("books") == []
                and self._logical_catalog_searches < _MAX_LOGICAL_CATALOG_SEARCHES
            ):
                fallback_arguments = empty_result_catalog_fallback_arguments(
                    arguments, self.request_context
                )
                if fallback_arguments is not None:
                    self._logical_catalog_searches += 1
                    fallback_result = await self.tools.invoke(
                        "search_books",
                        effective_tool_arguments(
                            "search_books", fallback_arguments, self.request_context
                        ),
                        agent_name=agent.name,
                        agent_tools=agent.allowed_tools,
                        permissions=agent.permissions,
                        context=tool_context,
                    )
                    self.evidence.record_tool_result("search_books", fallback_result)
                    tool_results.append(
                        {
                            "tool": "search_books",
                            "value": fallback_result.value,
                            "evidence_ids": fallback_result.evidence_ids,
                        }
                    )
        if agent.name == "knowledge" and not tool_results:
            # A knowledge node exists only for a request that needs the
            # versioned policy corpus.  When the model omits that sole
            # read-only call, retrieve the bounded original question rather
            # than allowing a later draft to claim that the rule is unknown.
            request_text = self.request_context.get("user_request")
            if isinstance(request_text, str) and len(request_text.strip()) >= 2:
                result = await self.tools.invoke(
                    "search_policy",
                    effective_tool_arguments(
                        "search_policy",
                        {"query": request_text.strip()[:1000], "limit": 5},
                        self.request_context,
                    ),
                    agent_name=agent.name,
                    agent_tools=agent.allowed_tools,
                    permissions=agent.permissions,
                    context=tool_context,
                )
                self.evidence.record_tool_result("search_policy", result)
                tool_results.append(
                    {"tool": "search_policy", "value": result.value, "evidence_ids": result.evidence_ids}
                )
        if (
            agent.name == "catalog"
            and not tool_results
            and self._logical_catalog_searches < _MAX_LOGICAL_CATALOG_SEARCHES
        ):
            fallback_arguments = missing_catalog_search_fallback_arguments(self.request_context)
            if fallback_arguments is not None:
                self._logical_catalog_searches += 1
                result = await self.tools.invoke(
                    "search_books",
                    effective_tool_arguments("search_books", fallback_arguments, self.request_context),
                    agent_name=agent.name,
                    agent_tools=agent.allowed_tools,
                    permissions=agent.permissions,
                    context=tool_context,
                )
                self.evidence.record_tool_result("search_books", result)
                tool_results.append(
                    {"tool": "search_books", "value": result.value, "evidence_ids": result.evidence_ids}
                )
        if agent.name == "catalog":
            for title in missing_explicit_comparison_titles(self.request_context, self.evidence):
                if self._logical_catalog_searches >= _MAX_LOGICAL_CATALOG_SEARCHES:
                    break
                self._logical_catalog_searches += 1
                result = await self.tools.invoke(
                    "search_books",
                    effective_tool_arguments(
                        "search_books",
                        {"q": title, "limit": 5},
                        self.request_context,
                    ),
                    agent_name=agent.name,
                    agent_tools=agent.allowed_tools,
                    permissions=agent.permissions,
                    context=tool_context,
                )
                self.evidence.record_tool_result("search_books", result)
                tool_results.append(
                    {"tool": "search_books", "value": result.value, "evidence_ids": result.evidence_ids}
                )
        return {"agent": agent.name, "tool_results": tool_results, "output": directive.output}
