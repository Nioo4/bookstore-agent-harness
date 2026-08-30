from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    prompt_id: str = Field(min_length=1)
    allowed_tools: frozenset[str]
    permissions: frozenset[str]


class AgentRegistry:
    def __init__(self, agents: Iterable[AgentSpec]) -> None:
        self._agents: dict[str, AgentSpec] = {}
        for agent in agents:
            if agent.name in self._agents:
                raise ValueError(f"duplicate agent: {agent.name}")
            self._agents[agent.name] = agent

    def get(self, name: str) -> AgentSpec:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise ValueError(f"unknown agent: {name}") from exc

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._agents)


def build_agent_registry() -> AgentRegistry:
    return AgentRegistry(
        [
            AgentSpec(
                name="catalog",
                prompt_id="catalog.v1",
                allowed_tools=frozenset({"search_books", "get_book"}),
                permissions=frozenset({"catalog:read"}),
            ),
            AgentSpec(
                name="recommendation",
                prompt_id="recommendation.v1",
                allowed_tools=frozenset(),
                permissions=frozenset(),
            ),
            AgentSpec(
                name="knowledge",
                prompt_id="knowledge.v1",
                allowed_tools=frozenset({"search_policy"}),
                permissions=frozenset({"policy:read"}),
            ),
            AgentSpec(
                name="order",
                prompt_id="order.v1",
                allowed_tools=frozenset({"prepare_order", "create_order", "get_order", "list_my_orders"}),
                permissions=frozenset({"order:read", "order:prepare", "order:commit"}),
            ),
            AgentSpec(
                name="verifier",
                prompt_id="verifier.v1",
                allowed_tools=frozenset(),
                permissions=frozenset(),
            ),
        ]
    )
