from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation.types import CaseFixture, EvalCaseV1, FakeModelScript
from app.models.provider import ModelMessage, ModelProvider, ModelRequest
from app.trace.safety import TraceSafetyProjector


class JudgeScores(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relevance: int = Field(ge=1, le=5)
    constraint_coverage: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    rationale_quality: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=512)

    @property
    def informational_passed(self) -> bool:
        values = [
            self.relevance,
            self.constraint_coverage,
            self.clarity,
            self.rationale_quality,
        ]
        return min(values) >= 3 and sum(values) / len(values) >= 4


class EvaluationJudge:
    def __init__(self, provider: ModelProvider, model: str, prompt: str, *, model_profile: str = "judge-default", prompt_version: str = "recommendation-v1") -> None:
        self.provider = provider
        self.model = model
        self.prompt = prompt
        self.model_profile = model_profile
        self.prompt_version = prompt_version

    async def score(
        self, case: EvalCaseV1, script: FakeModelScript, fixture: CaseFixture
    ) -> JudgeScores | None:
        evidence = [fact for call in fixture.calls for fact in call.evidence_facts]
        return await self._score(case, script.final_answer, evidence)

    async def score_observed(self, case, execution) -> JudgeScores | None:
        # v2 Judge sees the actual production answer/Evidence, never scripted claims.
        evidence = [dict(tool=name, result=result, evidence_ids=ids) for name,result,ids in execution.tools.results]
        safe = TraceSafetyProjector().json({"answer":execution.final_message.get("content", ""), "evidence":evidence})
        if safe.truncated:
            return None
        return await self._score(case, safe.value["answer"], safe.value["evidence"])

    async def _score(self, case, answer, evidence) -> JudgeScores | None:
        request = ModelRequest(
            model=self.model,
            temperature=0,
            response_schema=JudgeScores.model_json_schema(),
            messages=[
                ModelMessage(
                    role="system",
                    content=self.prompt
                    + "\nScore only relevance, constraint coverage, clarity, and rationale quality. "
                    "Do not score authorization, inventory, amount, or transaction safety. Return JSON only.",
                ),
                ModelMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "user_request": case.user_request,
                            "required_constraints": case.expected.required_constraints,
                            "final_answer": answer,
                            "evidence": evidence,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            ],
        )
        for _ in range(2):
            try:
                response = await self.provider.complete(request)
                return JudgeScores.model_validate_json(response.content)
            except Exception:
                continue
        return None
