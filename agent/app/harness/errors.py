from __future__ import annotations


class PlanValidationError(ValueError):
    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = issues


class PlanGenerationError(RuntimeError):
    pass


class ToolBudgetExceeded(RuntimeError):
    pass


class NodeExecutionError(RuntimeError):
    def __init__(self, node_id: str, cause: Exception) -> None:
        super().__init__(f"node {node_id} failed: {cause}")
        self.node_id = node_id
        self.__cause__ = cause
