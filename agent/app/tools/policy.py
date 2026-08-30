from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.tools.errors import ToolError
from app.tools.types import RiskLevel, ToolContext


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    code: str
    reason: str


class DeterministicPolicyEngine:
    async def evaluate(
        self,
        tool_name: str,
        risk: RiskLevel,
        arguments: BaseModel,
        context: ToolContext,
    ) -> PolicyDecision:
        if risk is RiskLevel.READ_ONLY:
            return PolicyDecision(allowed=True, code="POLICY_ALLOWED", reason="read-only tool")

        if context.access_token is None or context.authenticated_user_id is None:
            return PolicyDecision(allowed=False, code="AUTH_REQUIRED", reason="verified JWT context is required")

        if risk in {RiskLevel.PREPARE, RiskLevel.CONFIRM_REQUIRED, RiskLevel.COMMIT} and not context.email_verified:
            return PolicyDecision(allowed=False, code="EMAIL_NOT_VERIFIED", reason="verified email is required")

        if risk in {RiskLevel.CONFIRM_REQUIRED, RiskLevel.COMMIT} and not context.user_confirmed:
            return PolicyDecision(
                allowed=False,
                code="CONFIRMATION_REQUIRED",
                reason="an explicit user confirmation event is required",
            )

        if risk is RiskLevel.COMMIT:
            confirmation_token = getattr(arguments, "confirmation_token", "")
            if not confirmation_token:
                return PolicyDecision(
                    allowed=False,
                    code="CONFIRMATION_REQUIRED",
                    reason="a one-time confirmation token is required",
                )

        return PolicyDecision(allowed=True, code="POLICY_ALLOWED", reason=f"{tool_name} passed deterministic policy")

    async def enforce(
        self,
        tool_name: str,
        risk: RiskLevel,
        arguments: BaseModel,
        context: ToolContext,
    ) -> None:
        decision = await self.evaluate(tool_name, risk, arguments, context)
        if not decision.allowed:
            raise ToolError(decision.code, decision.reason)
