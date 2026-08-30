from __future__ import annotations

from collections import deque

from pydantic import BaseModel, ConfigDict, Field

from app.agents.registry import AgentRegistry
from app.harness.errors import PlanValidationError


class DagPlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    agent: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    objective: str = Field(min_length=1, max_length=1000)
    depends_on: list[str] = Field(default_factory=list)
    input_refs: list[str] = Field(default_factory=list)
    expected_output: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class DagPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=2000)
    nodes: list[DagPlanNode] = Field(min_length=1)


class PlanLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_nodes: int = Field(default=12, ge=1, le=12)
    max_depth: int = Field(default=4, ge=1, le=4)
    max_parallel: int = Field(default=3, ge=1, le=3)
    max_tool_calls: int = Field(default=20, ge=1, le=20)


class ValidatedPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    plan: DagPlan
    depths: dict[str, int]
    topological_order: tuple[str, ...]


class PlanValidator:
    def __init__(self, agents: AgentRegistry, limits: PlanLimits | None = None) -> None:
        self.agents = agents
        self.limits = limits or PlanLimits()

    def validate(
        self,
        plan: DagPlan,
        *,
        allowed_agents: frozenset[str] | None = None,
    ) -> ValidatedPlan:
        issues: list[str] = []
        if len(plan.nodes) > self.limits.max_nodes:
            issues.append(f"node count {len(plan.nodes)} exceeds {self.limits.max_nodes}")

        by_id: dict[str, DagPlanNode] = {}
        output_owner: dict[str, str] = {}
        for node in plan.nodes:
            if node.id in by_id:
                issues.append(f"duplicate node id: {node.id}")
            else:
                by_id[node.id] = node
            if node.expected_output in output_owner:
                issues.append(f"duplicate output ref: {node.expected_output}")
            else:
                output_owner[node.expected_output] = node.id
            if node.agent not in self.agents.names:
                issues.append(f"unknown agent: {node.agent}")
            elif allowed_agents is not None and node.agent not in allowed_agents:
                issues.append(f"agent is not allowed for this execution context: {node.agent}")
            if len(node.depends_on) != len(set(node.depends_on)):
                issues.append(f"duplicate dependency in node: {node.id}")
            if node.id in node.depends_on:
                issues.append(f"node depends on itself: {node.id}")

        indegree = {node_id: 0 for node_id in by_id}
        children = {node_id: [] for node_id in by_id}
        for node in plan.nodes:
            for dependency in node.depends_on:
                if dependency not in by_id:
                    issues.append(f"unknown dependency {dependency} in node {node.id}")
                    continue
                indegree[node.id] += 1
                children[dependency].append(node.id)

        queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
        order: list[str] = []
        depths: dict[str, int] = {node_id: 1 for node_id in queue}
        while queue:
            node_id = queue.popleft()
            order.append(node_id)
            for child in sorted(children[node_id]):
                depths[child] = max(depths.get(child, 1), depths[node_id] + 1)
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)

        if len(order) != len(by_id):
            issues.append("plan contains a cycle")
        elif depths and max(depths.values()) > self.limits.max_depth:
            issues.append(f"plan depth {max(depths.values())} exceeds {self.limits.max_depth}")

        ancestors: dict[str, set[str]] = {node_id: set() for node_id in by_id}
        if len(order) == len(by_id):
            for node_id in order:
                for dependency in by_id[node_id].depends_on:
                    if dependency not in by_id:
                        continue
                    ancestors[node_id].add(dependency)
                    ancestors[node_id].update(ancestors[dependency])

        for node in plan.nodes:
            if len(node.input_refs) != len(set(node.input_refs)):
                issues.append(f"duplicate input ref in node: {node.id}")
            for reference in node.input_refs:
                if reference == "user_request":
                    continue
                owner = output_owner.get(reference)
                if owner is None:
                    issues.append(f"unknown input ref {reference} in node {node.id}")
                elif owner not in ancestors[node.id]:
                    issues.append(f"input ref {reference} is not produced by a dependency of node {node.id}")

        if issues:
            raise PlanValidationError(issues)
        return ValidatedPlan(plan=plan, depths=depths, topological_order=tuple(order))
