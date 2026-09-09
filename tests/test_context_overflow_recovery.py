import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.agent.context_strategy import COMPRESSED_TOOL_RESULT, ContextStrategyConfig, ContextStrategyManager
from app.agent.core.event_router import EventRouter, EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.tool_manager import ToolManager
from app.agent.models.llm_client import StreamChunk
from app.agent.message import ToolCall
from app.agent.state import AgentState


class ScriptedModel:
    def __init__(self, failures=1):
        self.failures = failures
        self.requests = []
        self.summaries = []
        self.order = []

    async def astream(self, messages, tools=None):
        self.requests.append(deepcopy(messages))
        self.order.append("model")
        if len(self.requests) <= self.failures:
            raise RuntimeError("context_length_exceeded")
        yield StreamChunk(content="完成")

    async def ainvoke_structured(self, messages, schema, **kwargs):
        self.summaries.append(deepcopy(messages))
        self.order.append("summary")
        return {"active_tasks": [], "summary": f"历史摘要{len(self.summaries)}"}


def history():
    state = AgentState.create_new("overflow-test")
    for index in range(12):
        state.add_user_message(f"历史问题{index}：" + "文" * 100)
        state.messages.append({
            "role": "assistant", "content": "", "tool_calls": [{
                "id": f"call-{index}", "type": "function",
                "function": {"name": "observe", "arguments": "{}"},
            }],
        })
        state.add_tool_message("旧观察" * 100, "observe", f"call-{index}", compressible=True)
        state.add_assistant_message("历史回答" + "文" * 100)
    state.add_user_message("继续")
    return state


async def run(model, state, *, checkpoint_error=False):
    strategy = ContextStrategyManager(ContextStrategyConfig(compression_trigger_tokens_k=100))
    pipeline = ExecutionPipeline(
        prompt_source=SimpleNamespace(system_prompt="固定提示"), llm_client=model,
        tool_manager=ToolManager(), plugin_manager=PluginManager(),
        context_strategy=strategy, emit_event=EventRouter().emit,
        chat_settings=SimpleNamespace(name="助手", address="用户", feature="", character="", characteristic=""),
    )
    checkpoints = []

    async def save(state, *, checkpoint_type):
        model.order.append("checkpoint")
        if checkpoint_error:
            raise RuntimeError("checkpoint 写入失败")
        checkpoints.append(deepcopy(state.messages))

    events = [event async for event in pipeline.execute(state, checkpoint=save)]
    return events, checkpoints, strategy


def test_rejected_input_forces_both_compaction_steps_below_budget():
    model = ScriptedModel()
    events, checkpoints, strategy = asyncio.run(run(model, history()))
    assert len(model.summaries) == 1
    assert any(message.get("content") == COMPRESSED_TOOL_RESULT for message in model.summaries[0])
    assert model.order == ["model", "summary", "checkpoint", "model"]
    assert any(message.get("name") == "context_summary" for message in checkpoints[0])
    assert not any(event.type == EventType.ERROR for event in events)
    assert strategy.budget.calibration_factor == 1.10


@pytest.mark.parametrize("failures", [2, 3])
def test_second_recovery_uses_frozen_history_and_stops_after_two_candidates(failures):
    model = ScriptedModel(failures)
    state = history()
    events, checkpoints, _ = asyncio.run(run(model, state))
    assert len(model.requests) == 3
    assert len(model.summaries) == 2
    assert model.order == ["model", "summary", "checkpoint", "model", "summary", "checkpoint", "model"]
    sources = [request[1:-1] for request in model.summaries]
    assert len(sources[1]) > len(sources[0])
    assert sources[1][:len(sources[0])] == sources[0]
    assert not any(message.get("name") == "context_summary" for message in sources[1])
    assert len(checkpoints) == 2
    assert "历史摘要2" in checkpoints[-1][0]["content"]
    assert any(event.type == EventType.ERROR for event in events) == (failures == 3)


@pytest.mark.parametrize("chunk", [
    StreamChunk(content="已输出"),
    StreamChunk(tool_call=ToolCall(id="partial", name="observe", args={})),
])
def test_overflow_after_output_does_not_compact_or_retry(chunk):
    class PartialModel(ScriptedModel):
        async def astream(self, messages, tools=None):
            self.requests.append(deepcopy(messages))
            yield chunk
            raise RuntimeError("context_length_exceeded")

    model = PartialModel()
    events, checkpoints, _ = asyncio.run(run(model, history()))
    assert len(model.requests) == 1
    assert model.summaries == []
    assert checkpoints == []
    assert any(event.type == EventType.ERROR for event in events)


def test_unrelated_failure_does_not_start_compaction():
    class FailedModel(ScriptedModel):
        async def astream(self, messages, tools=None):
            self.requests.append(deepcopy(messages))
            raise RuntimeError("connection reset")
            yield  # 保持流式协议。

    model = FailedModel()
    events, checkpoints, _ = asyncio.run(run(model, history()))
    assert len(model.requests) == 1
    assert model.summaries == []
    assert checkpoints == []
    assert any(event.type == EventType.ERROR for event in events)


def test_overflow_without_completed_history_fails_without_resending():
    model = ScriptedModel()
    state = AgentState.create_new("no-history")
    state.add_user_message("只有本轮输入")
    events, checkpoints, _ = asyncio.run(run(model, state))
    assert len(model.requests) == 1
    assert model.summaries == []
    assert checkpoints == []
    assert any(event.type == EventType.ERROR for event in events)


def test_invalid_second_summary_keeps_first_committed_candidate():
    class InvalidSummaryModel(ScriptedModel):
        async def ainvoke_structured(self, messages, schema, **kwargs):
            result = await super().ainvoke_structured(messages, schema, **kwargs)
            return result if len(self.summaries) == 1 else {"invalid": True}

    model = InvalidSummaryModel(failures=2)
    state = history()
    events, checkpoints, _ = asyncio.run(run(model, state))
    assert len(model.requests) == 2
    assert len(checkpoints) == 1
    assert state.messages == checkpoints[0]
    assert any(event.type == EventType.ERROR for event in events)


def test_checkpoint_failure_prevents_model_retry():
    model = ScriptedModel()
    events, checkpoints, _ = asyncio.run(run(model, history(), checkpoint_error=True))
    assert model.order == ["model", "summary", "checkpoint"]
    assert len(model.requests) == 1
    assert checkpoints == []
    assert any(event.type == EventType.ERROR for event in events)
