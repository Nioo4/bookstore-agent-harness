class MemoryError(RuntimeError):
    code = "MEMORY_DEPENDENCY_UNAVAILABLE"


class MemoryNotFound(MemoryError):
    code = "MEMORY_NOT_FOUND"


class MemoryKeyUnsupported(MemoryError):
    code = "MEMORY_KEY_UNSUPPORTED"


class MemoryValueInvalid(MemoryError):
    code = "MEMORY_VALUE_INVALID"


class MemorySensitiveContent(MemoryError):
    code = "MEMORY_SENSITIVE_CONTENT"


class MemoryDependencyUnavailable(MemoryError):
    code = "MEMORY_DEPENDENCY_UNAVAILABLE"
