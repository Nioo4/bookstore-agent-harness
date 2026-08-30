from __future__ import annotations


class TraceError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TraceDependencyUnavailable(TraceError):
    def __init__(self, message: str = "trace dependency is unavailable") -> None:
        super().__init__("TRACE_DEPENDENCY_UNAVAILABLE", message, retryable=True)
