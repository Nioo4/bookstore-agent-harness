from __future__ import annotations

import json

from pydantic import ValidationError

from app.harness.errors import PlanGenerationError, PlanValidationError
from app.harness.plan import DagPlan, PlanValidator, ValidatedPlan
from app.harness.routing import required_agents_for_request
from app.models.provider import ModelMessage, ModelProvider, ModelRequest
from app.models.invocation import invoke_model
from app.models.deepseek import ModelOutputError


class Supervisor:
    def __init__(
        self,
        provider: ModelProvider,
        router_model: str,
        validator: PlanValidator,
        *,
        prompt_prefix: str = "",
    ) -> None:
        self.provider = provider
        self.router_model = router_model
        self.validator = validator
        self.prompt_prefix = prompt_prefix.strip()

    async def plan(
        self,
        user_request: str,
        *,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        allowed_agents: frozenset[str] | None = None,
    ) -> ValidatedPlan:
        available = self.validator.agents.names
        if allowed_agents is not None:
            available = available & allowed_agents
        registry_context = json.dumps({
            "registered_agents": [
                {"name": name, "allowed_tools": sorted(self.validator.agents.get(name).allowed_tools)}
                for name in sorted(available)
            ],
            "limits": self.validator.limits.model_dump(),
        }, sort_keys=True, separators=(",", ":"))
        execution_constraint = (
            " Use only these agents for this execution context: "
            + ", ".join(sorted(allowed_agents))
            + ". Requests for authenticated or write capabilities must be answered with a login requirement using only allowed agents."
            if allowed_agents is not None
            else ""
        )
        messages = [
            ModelMessage(
                role="system",
                content=(
                    (self.prompt_prefix + "\n" if self.prompt_prefix else "")
                    + "Create a minimal task DAG using only registered agents. Return only JSON matching the schema. "
                    "Each input_refs entry must be user_request or an ancestor's exact expected_output string. "
                    "Example: node id=catalog_lookup, expected_output=book_info => input_refs=[book_info], "
                    "never catalog_lookup.book_info or nodes.catalog_lookup.output. "
                    " Agents without tools consume dependency results; obtain authoritative catalog facts before recommendation. "
                    "Use knowledge for questions about system rules or capabilities, not ordinary catalog filters. "
                    "Do not schedule order operations for a preference statement or a recommendation-only request. "
                    "Durable preference extraction is handled separately after verification. "
                    "A stock alert is not an order: use catalog to look up the target book. Use knowledge only when the "
                    "user asks about an existing alert rule or capability. "
                    "The chat service generates a stock-alert confirmation card from catalog facts and extracted intent; "
                    "there is no create_inventory_watch tool. Do not schedule the order agent to create an alert. "
                    "Use the exact registry names, never invent agent names. Registry and execution limits: "
                    + registry_context
                    + execution_constraint
                ),
            ),
            ModelMessage(
                role="user",
                content=json.dumps(
                    {
                        "request": user_request,
                        "memory_context": memory_context or [],
                        "conversation_context": conversation_context or {"used": False},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ]
        last_error = ""
        for attempt in range(2):
            response = None
            try:
                response = await invoke_model(self.provider, caller="supervisor",
                    request=ModelRequest(model=self.router_model, messages=messages, temperature=0,
                        response_schema=DagPlan.model_json_schema())
                )
                candidate = DagPlan.model_validate(json.loads(response.content))
                validated = self.validator.validate(candidate, allowed_agents=allowed_agents)
                self._validate_required_routes(user_request, validated, conversation_context)
                return validated
            except (json.JSONDecodeError, ValidationError, PlanValidationError, ModelOutputError) as exc:
                last_error = str(exc)
                if attempt == 0:
                    messages.extend(
                        [
                            ModelMessage(role="assistant", content=response.content if response else "Invalid structured output omitted."),
                            ModelMessage(
                                role="user",
                                content=f"The plan was rejected: {last_error}. Repair it once and return only valid JSON.",
                            ),
                        ]
                    )
        raise PlanGenerationError(f"supervisor plan rejected after one repair: {last_error}")

    @staticmethod
    def _validate_required_routes(
        user_request: str,
        plan: ValidatedPlan,
        conversation_context: dict[str, object] | None = None,
    ) -> None:
        planned_agents = {node.agent for node in plan.plan.nodes}
        required = set(required_agents_for_request(user_request))
        # A same-page catalog continuation can only resolve prior facts by
        # retrieving them again; history is never evidence. A policy-only
        # follow-up, however, needs its policy source rather than an unrelated
        # catalog route merely because the page has prior conversation state.
        if (
            conversation_context
            and conversation_context.get("used") is True
            and "knowledge" not in required
        ):
            required.add("catalog")
        missing = required - planned_agents
        errors = [f"request requires authoritative {agent} routing" for agent in sorted(missing)]
        if "knowledge" in planned_agents and "knowledge" not in required:
            errors.append("request does not ask for system rules; remove the knowledge route")
        if errors:
            raise PlanValidationError(errors)

    def fast_route(self, goal: str, agent: str, objective: str, expected_output: str) -> ValidatedPlan:
        return self.validator.validate(
            DagPlan(
                goal=goal,
                nodes=[
                    {
                        "id": "fast_route",
                        "agent": agent,
                        "objective": objective,
                        "depends_on": [],
                        "input_refs": ["user_request"],
                        "expected_output": expected_output,
                    }
                ],
            )
        )
