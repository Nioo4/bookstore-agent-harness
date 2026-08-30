from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Category = Literal[
    "SIMPLE_QUERY",
    "MULTI_CONSTRAINT_RECOMMENDATION",
    "LONG_TERM_MEMORY",
    "ORDER_CONFIRMATION_CONCURRENCY",
    "PROMPT_INJECTION",
    "PERSISTENT_TASK_RECOVERY",
    "DEPENDENCY_DEGRADATION",
]


class EvalAuth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authenticated: bool
    email_verified: bool


class EvalExpected(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    final_status: Literal["COMPLETED", "FAILED", "CANCELLED"]
    required_tools: list[str]
    forbidden_tools: list[str]
    required_facts: list[dict[str, Any]]
    required_constraints: list[str]
    allowed_error_codes: list[str]


class EvalCaseV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,127}$")
    category: Category
    tags: list[str]
    user_request: str = Field(min_length=1, max_length=8000)
    auth: EvalAuth
    memory_seed: list[dict[str, Any]]
    fixture_ref: str
    fake_model_ref: str
    expected: EvalExpected


class FileDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvalSuiteManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["v1"]
    suite_version: Literal["v1", "v2"]
    isolated: Literal[True]
    network_access: Literal[False]
    case_count: Literal[40]
    case_ids: list[str]
    category_counts: dict[Category, int]
    files: list[FileDigest]

    @model_validator(mode="after")
    def validate_distribution(self) -> "EvalSuiteManifest":
        expected = {
            "SIMPLE_QUERY": 8,
            "MULTI_CONSTRAINT_RECOMMENDATION": 10,
            "LONG_TERM_MEMORY": 5,
            "ORDER_CONFIRMATION_CONCURRENCY": 5,
            "PROMPT_INJECTION": 5,
            "PERSISTENT_TASK_RECOVERY": 4,
            "DEPENDENCY_DEGRADATION": 3,
        }
        if self.category_counts != expected:
            raise ValueError("evaluation category distribution is not the required 8/10/5/5/5/4/3")
        if len(self.case_ids) != 40 or len(set(self.case_ids)) != 40:
            raise ValueError("evaluation suite must contain 40 unique case IDs")
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("evaluation manifest contains duplicate file paths")
        return self


class FakeToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    arguments: dict[str, Any]


class FakeModelScript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["v1"]
    tool_calls: list[FakeToolCall]
    final_status: Literal["COMPLETED", "FAILED", "CANCELLED"]
    final_answer: str
    facts: list[dict[str, Any]]
    satisfied_constraints: list[str]
    error_code: str | None = None
    confirmation_decision: Literal["APPROVE", "REJECT"] | None = None


class FixtureCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    arguments: dict[str, Any]
    response: dict[str, Any] | None
    outcome: Literal["SUCCESS", "BUSINESS_ERROR", "TECHNICAL_ERROR"]
    error_code: str | None = None
    evidence_facts: list[dict[str, Any]]


class RecoveryFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    crash_point: Literal["BEFORE_CHECKPOINT", "AFTER_CHECKPOINT", "AFTER_NOTIFICATION", "BEFORE_ACK"]
    deliveries: int = Field(ge=2, le=10)
    expected_unique_notifications: Literal[1]


class CaseFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["v1"]
    calls: list[FixtureCall]
    recovery: RecoveryFixture | None = None
    transaction_assertions: dict[str, bool] = Field(default_factory=dict)


class CaseExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: Category
    status: Literal["PASSED", "FAILED", "ERROR", "SKIPPED"]
    final_status: str
    failure_codes: list[str]
    deterministic_metrics: dict[str, int | bool]
    duration_ms: int = Field(ge=0)


class EvalMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    critical_transaction_passed: int
    critical_transaction_total: int
    severe_hallucinations: int
    tool_schema_valid: int
    tool_schema_attempted: int
    task_completed: int
    task_total: int
    recovery_passed: int
    recovery_total: int
    unauthorized_write_calls: int
    deterministic_gate_passed: bool
