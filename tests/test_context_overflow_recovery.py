import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.agent.context import ContextStrategyConfig, ModelInputCoordinator
from app.agent.context._compaction import COMPRESSED_TOOL_RESULT
from app.agent.core.event_router import EventRouter, EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.tool_manager import ToolManager
from app.agent.models.llm_client import StreamChunk
from app.agent.state.messages import ContextSummaryMessage, ToolCall, ToolMessage
from app.agent.state import AgentState


class ScriptedModel:
    """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。"""
    def __init__(self, failures=1):
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        Args:
            failures (Any): 按用例顺序制造的失败类型或次数。
        """
        self.failures = failures
        self.requests = []
        self.summaries = []
        self.order = []

    async def astream(self, messages, tools=None):
        """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            tools (list[dict] | None): 当前向模型公开的工具定义。
        """
        self.requests.append(deepcopy(messages))
        self.order.append("model")
        if len(self.requests) <= self.failures:
            raise RuntimeError("context_length_exceeded")
        yield StreamChunk(content="完成")

    async def ainvoke_structured(self, messages, schema, **kwargs):
        """接收摘要请求并返回预设结构或异常，供检查候选校验及恢复次数。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            schema (Any): 当前要检查或转换的 JSON Schema。
            kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
        """
        self.summaries.append(deepcopy(messages))
        self.order.append("summary")
        return {"active_tasks": [], "summary": f"历史摘要{len(self.summaries)}"}


def history():
    """构造上下文超限恢复所需的完整历史轮次。"""
    state = AgentState.create_new("overflow-test")
    for index in range(12):
        state.add_user_message(f"历史问题{index}：" + "文" * 100)
        call = ToolCall(id=f"call-{index}", name="observe", args={})
        state.record_assistant_step("", (call,))
        state.record_tool_result(call, "旧观察" * 100, compressible=True)
        state.add_assistant_message("历史回答" + "文" * 100)
    state.add_user_message("继续")
    return state


async def run(model, state, *, checkpoint_error=False):
    """由外层测试调用，完成“用例”的异步步骤。

    Args:
        model (Any): 模型配置或模型类型。
        state (Any): 当前会话的内存 AgentState；保存由调用方负责。
        checkpoint_error (Any): 是否在状态保存阶段制造异常。
    """
    config = ContextStrategyConfig(compression_trigger_tokens_k=100)
    model_input = ModelInputCoordinator(
        config,
        model,
        accounting_key="test",
        chat_settings=SimpleNamespace(
            name="助手",
            address="用户",
            feature="",
            character="",
            characteristic="",
        ),
    )
    pipeline = ExecutionPipeline(
        prompt_source=SimpleNamespace(system_prompt="固定提示"), llm_client=model,
        tool_manager=ToolManager(), plugin_manager=PluginManager(),
        model_input=model_input,
        emit_event=EventRouter().emit,
    )
    checkpoints = []

    async def save(state, *, checkpoint_type):
        """记录当前候选保存，按用例配置可主动制造失败。

        Args:
            state (Any): 当前会话的内存 AgentState；保存由调用方负责。
            checkpoint_type (Any): 本次保存中间执行进度或完整轮次的类型。
        """
        model.order.append("checkpoint")
        if checkpoint_error:
            raise RuntimeError("checkpoint 写入失败")
        checkpoints.append(deepcopy(state.messages))

    events = [event async for event in pipeline.execute(state, checkpoint=save)]
    return events, checkpoints, model_input


def test_rejected_input_forces_both_compaction_steps_below_budget():
    """验证模型拒绝上下文后强制执行两层压缩。"""
    model = ScriptedModel()
    events, checkpoints, strategy = asyncio.run(run(model, history()))
    assert len(model.summaries) == 1
    assert any(message.get("content") == COMPRESSED_TOOL_RESULT for message in model.summaries[0])
    assert model.order == ["model", "summary", "checkpoint", "model"]
    assert any(isinstance(message, ContextSummaryMessage) for message in checkpoints[0])
    assert not any(event.type == EventType.ERROR for event in events)


@pytest.mark.parametrize("failures", [2, 3])
def test_second_recovery_uses_frozen_history_and_stops_after_two_candidates(failures):
    """验证第二次恢复使用同一原历史且最多两次候选。

    Args:
        failures (Any): 按用例顺序制造的失败类型或次数。
    """
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
    assert "历史摘要2" in checkpoints[-1][0].text
    assert any(event.type == EventType.ERROR for event in events) == (failures == 3)


@pytest.mark.parametrize("chunk", [
    StreamChunk(content="已输出"),
    StreamChunk(tool_call=ToolCall(id="partial", name="observe", args={})),
])
def test_overflow_after_output_does_not_compact_or_retry(chunk):
    """验证模型已输出文字后的超限不压缩重试。

    Args:
        chunk (Any): 本次需要产出的模型片段。
    """
    class PartialModel(ScriptedModel):
        """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。

        该定义仅用于外层用例：验证模型已输出文字后的超限不压缩重试。
        """
        async def astream(self, messages, tools=None):
            """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

            该定义仅用于外层用例：验证模型已输出文字后的超限不压缩重试。

            Args:
                messages (list[dict]): 按对话顺序排列的消息集合。
                tools (list[dict] | None): 当前向模型公开的工具定义。
            """
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
    """验证其他模型失败不触发上下文压缩。"""
    class FailedModel(ScriptedModel):
        """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。

        该定义仅用于外层用例：验证其他模型失败不触发上下文压缩。
        """
        async def astream(self, messages, tools=None):
            """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

            该定义仅用于外层用例：验证其他模型失败不触发上下文压缩。

            Args:
                messages (list[dict]): 按对话顺序排列的消息集合。
                tools (list[dict] | None): 当前向模型公开的工具定义。
            """
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
    """验证没有完整旧轮次时失败且不重复发送。"""
    model = ScriptedModel()
    state = AgentState.create_new("no-history")
    state.add_user_message("只有本轮输入")
    events, checkpoints, _ = asyncio.run(run(model, state))
    assert len(model.requests) == 1
    assert model.summaries == []
    assert checkpoints == []
    assert any(event.type == EventType.ERROR for event in events)


def test_invalid_second_summary_keeps_first_committed_candidate():
    """验证第二次摘要无效时保留第一次已保存候选。"""
    class InvalidSummaryModel(ScriptedModel):
        """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。

        该定义仅用于外层用例：验证第二次摘要无效时保留第一次已保存候选。
        """
        async def ainvoke_structured(self, messages, schema, **kwargs):
            """接收摘要请求并返回预设结构或异常，供检查候选校验及恢复次数。

            该定义仅用于外层用例：验证第二次摘要无效时保留第一次已保存候选。

            Args:
                messages (list[dict]): 按对话顺序排列的消息集合。
                schema (Any): 当前要检查或转换的 JSON Schema。
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
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
    """验证checkpoint 保存失败阻止模型重试。"""
    model = ScriptedModel()
    events, checkpoints, _ = asyncio.run(run(model, history(), checkpoint_error=True))
    assert model.order == ["model", "summary", "checkpoint"]
    assert len(model.requests) == 1
    assert checkpoints == []
    assert any(event.type == EventType.ERROR for event in events)
