import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.chat import ChatMessageRequest, ChatService, QueueEventSink
from app.harness.single_agent import SingleAgentAction
from app.models.provider import ModelResponse
from app.trace.types import StepStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "invalid", "error", "cancel"])
async def test_production_repair_callback_closes_step(monkeypatch, outcome):
    final = SingleAgentAction(action="FINAL", answer="事实摘要")
    provider = SimpleNamespace(complete=AsyncMock())
    if outcome == "error":
        provider.complete.side_effect = NameError("internal detail")
    elif outcome == "cancel":
        provider.complete.side_effect = asyncio.CancelledError()
    else:
        provider.complete.return_value = ModelResponse(
            model="test", content=final.model_dump_json() if outcome == "success" else "invalid"
        )
    recorder = SimpleNamespace(
        run=SimpleNamespace(run_id="repair-regression", model_profile="test", prompt_set_version="test"),
        bind=lambda: None,
        start_step=AsyncMock(return_value="repair-step"),
        finish_step=AsyncMock(),
        finish_run=AsyncMock(return_value=SimpleNamespace(trace_status=SimpleNamespace(value="COMPLETE"))),
    )
    captured = []

    class Runtime:
        def __init__(self, *args, **kwargs):
            self.repair = kwargs["repair_provider"]

        async def run(self, *args, **kwargs):
            captured.append(await self.repair(final, [{"code": "EVIDENCE_MISMATCH"}], []))
            # Stop after the production callback, before unrelated UI preparation.
            raise RuntimeError("test callback complete")

    monkeypatch.setattr("app.harness.single_agent.SingleAgentRuntime", Runtime)
    base = SimpleNamespace(provider=provider, model="test", tools=None, limits=None, system_prompt="")
    service = ChatService(provider, "test", None, None, single_agent_runtime=base)
    sink = QueueEventSink(asyncio.Queue(), False, guest=True, trace_persisted=True)
    invocation = service._execute("conversation", ChatMessageRequest(content="推荐书籍"),
                                  access_token=None, user=None, sink=sink, recorder=recorder)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await invocation
    else:
        await invocation
    provider.complete.assert_awaited_once()
    recorder.finish_step.assert_awaited_once()
    result = recorder.finish_step.await_args.kwargs
    assert result["status"] == (StepStatus.COMPLETED if outcome == "success" else StepStatus.FAILED)
    if outcome == "success":
        assert captured == [final]
    if outcome == "error":
        assert result["error_code"] == "INTERNAL_ERROR"
        assert result["error_message"] == "answer repair failed"
