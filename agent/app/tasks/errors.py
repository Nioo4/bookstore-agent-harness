class TaskError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TaskDependencyUnavailable(TaskError):
    def __init__(self, message: str = "task dependency is unavailable") -> None:
        super().__init__("TASK_DEPENDENCY_UNAVAILABLE", message, retryable=True)
