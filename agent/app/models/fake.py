from __future__ import annotations

from collections import deque

from app.models.provider import ModelProvider, ModelRequest, ModelResponse


class FakeModelProvider(ModelProvider):
    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = deque(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("FakeModelProvider has no response left")
        return self._responses.popleft()
