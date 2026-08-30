from __future__ import annotations

from typing import Any


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class UnknownToolError(ToolError):
    def __init__(self, name: str) -> None:
        super().__init__("UNKNOWN_TOOL", f"tool is not registered: {name}")


class ToolPermissionError(ToolError):
    def __init__(self, name: str) -> None:
        super().__init__("TOOL_PERMISSION_DENIED", f"tool is not permitted: {name}")
