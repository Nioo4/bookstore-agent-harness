from __future__ import annotations

import asyncio

from app.harness.errors import ToolBudgetExceeded


class ToolCallBudget:
    def __init__(self, maximum: int, *, used: int = 0) -> None:
        if maximum <= 0:
            raise ValueError("tool call budget must be positive")
        if used < 0 or used > maximum:
            raise ValueError("used tool calls must be within the budget")
        self.maximum = maximum
        self.used = used
        self._lock = asyncio.Lock()

    async def consume(self) -> None:
        async with self._lock:
            if self.used >= self.maximum:
                raise ToolBudgetExceeded(f"tool call budget exceeded ({self.maximum})")
            self.used += 1
