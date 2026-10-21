from tests.runtime_support import checkpoint_probe
from app.agent.models.openai_message_names import SCREENSHOT_COMPRESSED_NAME, SCREENSHOT_MESSAGE_NAME
from tests.model_capture import result_parts
from app.agent.state.messages import ProviderData
"""agent 引擎回归测试（离线，无需网络）。

覆盖以下核心行为：
- 带工具调用的助手消息可正确序列化
- 流式工具调用在流末仅产出一次
- 中断恢复可重入同一工具并注入截屏图片
"""
from tests.model_capture import capture_input
from app.agent.state.messages import text_content

import asyncio
import types
from copy import deepcopy

import pytest

from app.agent.agent import AgentConfig
from app.agent.context import ContextStrategyConfig, ModelInputCoordinator
from app.agent.core.event_router import EventRouter, EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.tool_manager import ToolManager
from app.agent.state.messages import (AssistantMessage, HumanMessage, ImageContent, ToolMessage, ToolMessage, ToolCall)
from app.agent.state import AgentState
from app.agent.models.llm_client import LLMClient, LLMConfig, StreamChunk
from app.agent.tools.core.base import BaseTool, ToolResult


# ==================== 测试替身 ====================

def _delta(content=None, tool_calls=None, finish_reason=None):
    """构造一个伪 openai 流式 chunk

    Args:
        content (Any): 当前处理的正文。
        tool_calls (Any): 同一模型步骤中请求的工具调用。
        finish_reason (Any): 脚本模型片段的结束原因。
    """
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice])


def _tc_delta(index, id=None, name=None, arguments=None):
    """生成指定工具索引、编号和参数片段的模型响应。

    Args:
        index (Any): 模型片段中的工具调用索引。
        id (Any): 本次测试对象的稳定编号。
        name (Any): 当前对象的名称。
        arguments (Any): 模型工具调用的参数文本或字典。
    """
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(index=index, id=id, function=fn)


class _FakeScreenshotTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "screenshot"
    description = "截屏"
    parameters_schema = {"type": "object", "properties": {}}

    async def execute(self, args, context):
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict[str, Any]): 当前工具调用或被包装函数的参数。
            context (Any): 执行管道提供的工具运行上下文。
        """
        return ToolResult.success(
            "截屏成功",
            image_url="data:image/png;base64,iVBORw0KGgpwYXlsb2Fk",
            media_kind="screenshot",
        )


class _FakeSearchTool(BaseTool):
    """提供用例指定的工具响应、中断或异常，验证执行管道的结果保存与恢复。"""
    name = "search_memory"
    description = "搜索记忆"
    parameters_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    async def execute(self, args, context):
        """管道或插件管理器调用时执行本用例预设的结果、状态修改或异常分支。

        Args:
            args (dict[str, Any]): 当前工具调用或被包装函数的参数。
            context (Any): 执行管道提供的工具运行上下文。
        """
        return ToolResult.success(f"检索结果: {args['query']}")


class _FakeLLM:
    """按调用次序回放脚本的伪 LLM 客户端。"""

    def __init__(self, scripts):
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        该定义仅用于外层用例：按调用次序回放脚本的伪 LLM 客户端。

        Args:
            scripts (Any): 按模型调用顺序准备的响应或片段列表。
        """
        self.scripts = scripts
        self.calls = 0
        self.messages = []

    async def astream(self, messages, tools=None):
        """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

        该定义仅用于外层用例：按调用次序回放脚本的伪 LLM 客户端。

        Args:
            messages (list[dict]): 按对话顺序排列的消息集合。
            tools (list[dict] | None): 当前向模型公开的工具定义。
        """
        messages = capture_input(messages)
        self.messages.append(messages)
        script = self.scripts[self.calls]
        self.calls += 1
        for chunk in script:
            yield chunk


class _PipelineHarness:
    """提供当前用例所需的 _PipelineHarness 测试替身，执行记录仅供外层断言使用。"""
    def __init__(self, llm, tools):
        """保存本用例的脚本输入和依赖，初始化测试对象的执行记录。

        Args:
            llm (Any): 当前用例提供的脚本模型客户端。
            tools (list[dict] | None): 当前向模型公开的工具定义。
        """
        self.config = AgentConfig(
            session_id="t", model_name="m", api_key="k",
            context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100), system_prompt="sys",
        )
        self.tool_manager = ToolManager()
        for t in tools:
            self.tool_manager.register(t)
        self.plugin_manager = PluginManager()
        self.event_router = EventRouter()
        self.llm_client = llm
        self.pipeline = ExecutionPipeline(
            prompt_source=self.config,
            llm_client=self.llm_client,
            tool_manager=self.tool_manager,
            plugin_manager=self.plugin_manager,
            model_input=ModelInputCoordinator(
                self.config.context_strategy,
                self.llm_client,
                accounting_key="test",
            ),
            emit_event=self.event_router.emit,
        )


async def _drain(agen):
    """完整消费异步事件流，供外层检查产出的事件。

    Args:
        agen (Any): 需要完整消费的异步事件生成器。
    """
    return [ev async for ev in agen]


# ==================== 流式工具调用聚合 ====================

def test_astream_emits_each_tool_call_once():
    """验证每次流式工具调用只产出一次。"""
    async def run():
        """由外层测试调用，完成“每次流式工具调用只产出一次”的异步步骤。"""
        client = LLMClient(LLMConfig(model="m", api_key="k", base_url="http://x/v1"))

        async def fake_create(**kwargs):
            """按脚本返回模型响应或模型流，替代 SDK 的远程创建请求。

            该定义仅用于外层用例：验证每次流式工具调用只产出一次。

            Args:
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            async def gen():
                """向外层测试或模型客户端逐项产出本用例准备的流式片段。

                该定义仅用于外层用例：验证每次流式工具调用只产出一次。
                """
                yield _delta(content="好的")
                yield _delta(tool_calls=[_tc_delta(0, id="call_1", name="screenshot", arguments="")])
                yield _delta(tool_calls=[_tc_delta(0, arguments="{}")])
                yield _delta(finish_reason="tool_calls")
            return gen()

        client._adapter._client.chat.completions.create = fake_create
        chunks = [c async for c in client.astream([HumanMessage(content=text_content("hi"))], tools=[])]
        await client.close()
        return chunks

    chunks = asyncio.run(run())
    tool_calls = [c.tool_call for c in chunks if c.tool_call is not None]
    texts = [c.content for c in chunks if c.content]
    assert texts == ["好的"]
    assert len(tool_calls) == 1, f"工具调用应只产出一次，实际 {len(tool_calls)}"
    assert tool_calls[0].name == "screenshot"
    assert tool_calls[0].id == "call_1"
    assert tool_calls[0].args == {}


def test_astream_accumulates_fragmented_arguments_and_signature():
    """验证流式碎片正确合并参数和签名。"""
    async def run():
        """由外层测试调用，完成“流式碎片正确合并参数和签名”的异步步骤。"""
        client = LLMClient(LLMConfig(model="m", api_key="k", base_url="http://x/v1"))

        async def fake_create(**kwargs):
            """按脚本返回模型响应或模型流，替代 SDK 的远程创建请求。

            该定义仅用于外层用例：验证流式碎片正确合并参数和签名。

            Args:
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            async def gen():
                """向外层测试或模型客户端逐项产出本用例准备的流式片段。

                该定义仅用于外层用例：验证流式碎片正确合并参数和签名。
                """
                first_delta = _tc_delta(
                    0,
                    id="call_1",
                    name="search_memory",
                    arguments='{"query":',
                )
                first_delta.extra_content = {
                    "google": {"thought_signature": "signature-value"}
                }
                yield _delta(tool_calls=[
                    first_delta
                ])
                yield _delta(tool_calls=[
                    _tc_delta(0, arguments='"代码优化"}')
                ])
                yield _delta(finish_reason="tool_calls")
            return gen()

        client._adapter._client.chat.completions.create = fake_create
        try:
            return [
                chunk async for chunk in client.astream(
                    [HumanMessage(content=text_content("hi"))], tools=[]
                )
            ]
        finally:
            await client.close()

    chunks = asyncio.run(run())
    tool_calls = [chunk.tool_call for chunk in chunks if chunk.tool_call is not None]
    assert len(tool_calls) == 1
    assert tool_calls[0].args == {"query": "代码优化"}
    assert tool_calls[0].provider_data.to_dict()["payload"]["extra_content"] == {
        "google": {"thought_signature": "signature-value"}
    }


def test_astream_separates_different_calls_that_reuse_index():
    """验证复用索引但编号不同的工具调用不会混淆。"""
    async def run():
        """由外层测试调用，完成“复用索引但编号不同的工具调用不会混淆”的异步步骤。"""
        client = LLMClient(LLMConfig(model="m", api_key="k", base_url="http://x/v1"))

        async def fake_create(**kwargs):
            """按脚本返回模型响应或模型流，替代 SDK 的远程创建请求。

            该定义仅用于外层用例：验证复用索引但编号不同的工具调用不会混淆。

            Args:
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            async def gen():
                """向外层测试或模型客户端逐项产出本用例准备的流式片段。

                该定义仅用于外层用例：验证复用索引但编号不同的工具调用不会混淆。
                """
                screenshot = _tc_delta(
                    0,
                    id="call_screenshot",
                    name="screenshot",
                    arguments="",
                )
                screenshot.extra_content = {
                    "google": {"thought_signature": "screenshot-signature"}
                }
                yield _delta(tool_calls=[screenshot])

                screenshot_args = _tc_delta(0, arguments="{}")
                screenshot_args.extra_content = {
                    "google": {"thought_signature": "screenshot-signature"}
                }
                yield _delta(tool_calls=[screenshot_args])

                yield _delta(tool_calls=[_tc_delta(
                    0,
                    id="call_diary",
                    name="search_diary",
                    arguments='{"end":"2026-06-19","start":"2026-06-15"}',
                )])
                yield _delta(finish_reason="tool_calls")
            return gen()

        client._adapter._client.chat.completions.create = fake_create
        try:
            return [
                chunk async for chunk in client.astream(
                    [HumanMessage(content=text_content("测试多个工具"))], tools=[]
                )
            ]
        finally:
            await client.close()

    chunks = asyncio.run(run())
    tool_calls = [chunk.tool_call for chunk in chunks if chunk.tool_call is not None]
    assert [(call.id, call.name, call.args) for call in tool_calls] == [
        ("call_screenshot", "screenshot", {}),
        (
            "call_diary",
            "search_diary",
            {"end": "2026-06-19", "start": "2026-06-15"},
        ),
    ]
    assert tool_calls[0].provider_data.to_dict()["payload"]["extra_content"] == {
        "google": {"thought_signature": "screenshot-signature"}
    }
    assert tool_calls[1].provider_data is None


@pytest.mark.parametrize(
    "raw_arguments",
    ['{"query":', "[]", '{} trailing', '{}{"query":"代码优化"}'],
)
def test_astream_rejects_invalid_tool_arguments(raw_arguments):
    """验证无效工具参数被拒绝。

    Args:
        raw_arguments (Any): 尚未解析的工具参数 JSON 文本。
    """
    async def run():
        """由外层测试调用，完成“无效工具参数被拒绝”的异步步骤。"""
        client = LLMClient(LLMConfig(model="m", api_key="k", base_url="http://x/v1"))

        async def fake_create(**kwargs):
            """按脚本返回模型响应或模型流，替代 SDK 的远程创建请求。

            该定义仅用于外层用例：验证无效工具参数被拒绝。

            Args:
                kwargs (dict[str, Any]): 传递给被调用函数的其他关键字参数。
            """
            async def gen():
                """向外层测试或模型客户端逐项产出本用例准备的流式片段。

                该定义仅用于外层用例：验证无效工具参数被拒绝。
                """
                yield _delta(tool_calls=[
                    _tc_delta(
                        0,
                        id="call_1",
                        name="search_memory",
                        arguments=raw_arguments,
                    )
                ])
                yield _delta(finish_reason="tool_calls")
            return gen()

        client._adapter._client.chat.completions.create = fake_create
        try:
            return [
                chunk async for chunk in client.astream(
                    [HumanMessage(content=text_content("hi"))], tools=[]
                )
            ]
        finally:
            await client.close()

    with pytest.raises(ValueError, match="工具参数解析失败"):
        asyncio.run(run())


# ==================== 助手工具调用序列化 ====================

def test_assistant_message_with_tools_serializes():
    """验证带工具请求的助手消息可序列化。"""
    msg = AssistantMessage(
        content=text_content(""),
        tool_calls=(ToolCall(id="c1", name="screenshot", args={}),),
    )
    d = msg.to_storage_format()
    assert d["message_type"] == "assistant"
    assert d["tool_calls"][0]["name"] == "screenshot"


def test_tool_call_extra_content_roundtrips_into_followup_request():
    """验证工具附加协议内容在后续请求中完整保留。"""
    async def run():
        """由外层测试调用，完成“工具附加协议内容在后续请求中完整保留”的异步步骤。"""
        signature = {"google": {"thought_signature": "signature-value"}}
        llm = _FakeLLM(scripts=[
            [StreamChunk(tool_call=ToolCall(
                id="c1",
                name="search_memory",
                args={"query": "测试"},
                provider_data=ProviderData("openai_chat_completions", "https://api.openai.com/v1", "test-model", {"extra_content": signature}),
            ))],
            [StreamChunk(content="搜索功能正常")],
        ])
        agent = _PipelineHarness(llm, tools=[_FakeSearchTool()])
        state = AgentState.create_new("t")
        state.add_user_message("测试搜索")
        events = await _drain(agent.pipeline.execute(state))
        return llm, state, events

    llm, state, events = asyncio.run(run())
    assert events[-1].type == EventType.TEXT_CHUNK
    assert events[-1].data == "搜索功能正常"
    assert llm.messages[0][0] == llm.messages[1][0]
    assert llm.messages[0][0]["content"] == "sys"
    latest_user = next(message for message in llm.messages[0] if message.get("role") == "user")
    assert "当前日期时间：" in latest_user["content"]

    assistant_call = next(
        message for message in llm.messages[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant_call["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": "signature-value"}
    }

    restored = AgentState.from_checkpoint(state.to_checkpoint())
    restored_call = next(
        message for message in restored.messages
        if isinstance(message, AssistantMessage) and message.tool_calls
    )
    assert restored_call.tool_calls[0].provider_data.to_dict()["payload"]["extra_content"] == {
        "google": {"thought_signature": "signature-value"}
    }


# ==================== 可恢复工具与图片注入 ====================

def test_direct_screenshot_roundtrip():
    """验证直接截图结果在一次模型工具循环中提交并形成截图消息。"""
    async def run():
        """从统一 Pipeline 入口消费截图与后续回复，确认不产生执行等待。"""
        llm = _FakeLLM(scripts=[
            # 第 1 次：模型请求截屏
            [StreamChunk(tool_call=ToolCall(id="c1", name="screenshot", args={}))],
            # 恢复后第 2 次：模型基于截图作答
            [StreamChunk(content="我看到一只猫")],
        ])
        agent = _PipelineHarness(llm, tools=[_FakeScreenshotTool()])
        state = AgentState.create_new("t")
        state.add_user_message("看看我的屏幕")

        events1 = await _drain(agent.pipeline.execute(state))
        assert not any(event.type == EventType.INTERRUPT for event in events1)
        assert state.interrupt_data is None
        assert events1[-1].type == EventType.TEXT_CHUNK
        assert events1[-1].data == "我看到一只猫"
        return state

    state = asyncio.run(run())
    msgs = state.messages
    # 工具结果同时保留文字与截图
    assert any(isinstance(m, ToolMessage) and m.text == "截屏成功" for m in msgs)
    # 截图类别保存在结果内容块中
    shot = [
        m for m in msgs
        if isinstance(m, ToolMessage) and any(isinstance(part, ImageContent) and part.media_kind == "screenshot" for part in m.content)
    ]
    assert shot, "应保留截图内容块"
    assert "image_url" in str(shot[0].to_storage_format()["content"])
    # 模型最终基于截图作答
    assistant_texts = [m.text for m in msgs if isinstance(m, AssistantMessage)]
    assert "我看到一只猫" in assistant_texts
    # 中断状态已清除
    assert state.interrupt_data is None


# ==================== 核心上下文策略 ====================

def test_pipeline_checkpoints_screenshot_compaction_before_llm_call():
    """验证截图压缩先保存 checkpoint 再调用模型。"""
    async def run():
        """由外层测试调用，完成“截图压缩先保存 checkpoint 再调用模型”的异步步骤。"""
        llm = _FakeLLM(scripts=[[StreamChunk(content="完成")]])
        agent = _PipelineHarness(llm, tools=[])
        state = AgentState.create_new("t")
        for index in range(3):
            call = ToolCall(id=f"shot-{index}", name="screenshot", args={})
            state.record_assistant_step("", (call,))
            state.record_tool_result(call, result_parts('完成', ('data:image/png;base64,c2hvdA==',), 'screenshot'))
        state.add_user_message("继续")
        snapshots: list[dict] = []

        async def checkpoint(current, *, checkpoint_type):
            """记录当前状态保存类型和执行顺序，供用例确认先保存再调用模型或发布事件。

            该定义仅用于外层用例：验证截图压缩先保存 checkpoint 再调用模型。

            Args:
                current (Any): 回调收到的当前 AgentState。
                checkpoint_type (Any): 本次保存中间执行进度或完整轮次的类型。
            """
            assert checkpoint_type == "intermediate"
            snapshots.append(deepcopy(current.to_checkpoint()))
            return len(snapshots)

        events = await _drain(agent.pipeline.execute(state, session_state_manager=checkpoint_probe(checkpoint)))
        return llm, snapshots, events

    llm, snapshots, events = asyncio.run(run())

    assert events[-1].data == "完成"
    assert len(snapshots) == 1
    checkpoint_messages = snapshots[0]["messages"]
    checkpoint_screenshots = [
        message for message in checkpoint_messages
        if message.get("message_type") == "tool" and any(part.get("context_state")=="compacted" for part in message.get("content",[]))
    ]
    assert checkpoint_screenshots[0]["content"][-1]["context_state"] == "compacted"
    assert "base64" not in str(checkpoint_screenshots[0]["content"])
    assert [
        message.get("name")
        for message in llm.messages[0]
        if message.get("name") in {
            SCREENSHOT_MESSAGE_NAME,
            SCREENSHOT_COMPRESSED_NAME,
        }
    ] == [
        SCREENSHOT_COMPRESSED_NAME,
        SCREENSHOT_MESSAGE_NAME,
        SCREENSHOT_MESSAGE_NAME,
    ]

def test_context_strategy_builds_without_temporary_state():
    """验证上下文策略不依赖临时 Agent 状态。"""
    async def run():
        """由外层测试调用，完成“上下文策略不依赖临时 Agent 状态”的异步步骤。"""
        llm = _FakeLLM(scripts=[[StreamChunk(content="你好呀")]])
        agent = _PipelineHarness(llm, tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("在吗")
        events = await _drain(agent.pipeline.execute(state))
        return state, events

    state, events = asyncio.run(run())
    assert events[-1].type == EventType.TEXT_CHUNK
    assert events[-1].data == "你好呀"
    assert state.extra == {}
    # state.messages 仍含完整对话
    assert [type(m).__name__ for m in state.messages] == [
        "HumanMessage",
        "AssistantMessage",
    ]


def test_pipeline_removes_streamed_assistant_time_prefix():
    """验证流式助手回复的时间前缀被删除。"""
    async def run():
        """由外层测试调用，完成“流式助手回复的时间前缀被删除”的异步步骤。"""
        llm = _FakeLLM(scripts=[[
            StreamChunk(content="[发送"),
            StreamChunk(content="时间：2026-09-10 10:11:12 +0800 星期四] "),
            StreamChunk(content="真正回复"),
            StreamChunk(finish_reason="stop"),
        ]])
        agent = _PipelineHarness(llm, tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("请回复")
        events = await _drain(agent.pipeline.execute(state))
        return state, events

    state, events = asyncio.run(run())
    streamed = "".join(
        event.data for event in events if event.type == EventType.TEXT_CHUNK
    )

    assert streamed == "真正回复"
    assert state.messages[-1].text == "真正回复"


def test_pipeline_keeps_expression_tag_before_removed_time_prefix():
    """验证删除时间前缀时保留前置表情标签。"""
    async def run():
        """由外层测试调用，完成“删除时间前缀时保留前置表情标签”的异步步骤。"""
        llm = _FakeLLM(scripts=[[
            StreamChunk(content="<开心>"),
            StreamChunk(content="[发送时间：2026-09-10 10:11:12 +0800 星期四]"),
            StreamChunk(content="真正回复"),
            StreamChunk(finish_reason="stop"),
        ]])
        agent = _PipelineHarness(llm, tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("请回复")
        events = await _drain(agent.pipeline.execute(state))
        return state, events

    state, events = asyncio.run(run())
    streamed = "".join(
        event.data for event in events if event.type == EventType.TEXT_CHUNK
    )

    assert streamed == "<开心>真正回复"
    assert state.messages[-1].text == "<开心>真正回复"


def test_pipeline_keeps_long_expression_tag_without_spending_prefix_buffer():
    """验证长表情标签不消耗时间前缀缓冲预算。"""
    expression_tag = "<" + "x" * 120 + ">"

    async def run():
        """由外层测试调用，完成“长表情标签不消耗时间前缀缓冲预算”的异步步骤。"""
        llm = _FakeLLM(scripts=[[
            StreamChunk(content=expression_tag),
            StreamChunk(content="[发送"),
            StreamChunk(content="时间：2026-09-10 10:11:12 +0800 星期四] "),
            StreamChunk(content="真正回复"),
            StreamChunk(finish_reason="stop"),
        ]])
        agent = _PipelineHarness(llm, tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("请回复")
        events = await _drain(agent.pipeline.execute(state))
        return state, events

    state, events = asyncio.run(run())
    streamed = "".join(
        event.data for event in events if event.type == EventType.TEXT_CHUNK
    )

    assert streamed == expression_tag + "真正回复"
    assert state.messages[-1].text == expression_tag + "真正回复"


def test_pipeline_handles_long_expression_tag_split_into_characters():
    """验证逐字到达的长表情标签仍正确保留。"""
    expression_tag = "<" + "x" * 120 + ">"

    async def run():
        """由外层测试调用，完成“逐字到达的长表情标签仍正确保留”的异步步骤。"""
        script = [StreamChunk(content=char) for char in expression_tag]
        script.extend([
            StreamChunk(content="[发送"),
            StreamChunk(content="时间：2026-09-10 10:11:12 +0800 星期四] "),
            StreamChunk(content="真正回复"),
            StreamChunk(finish_reason="stop"),
        ])
        agent = _PipelineHarness(_FakeLLM(scripts=[script]), tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("请回复")
        events = await _drain(agent.pipeline.execute(state))
        return state, events

    state, events = asyncio.run(run())
    streamed = "".join(
        event.data for event in events if event.type == EventType.TEXT_CHUNK
    )

    assert streamed == expression_tag + "真正回复"
    assert state.messages[-1].text == expression_tag + "真正回复"


def test_pipeline_preserves_formatting_after_removed_time_prefix():
    """验证删除时间前缀后保留正文格式。"""
    async def run():
        """由外层测试调用，完成“删除时间前缀后保留正文格式”的异步步骤。"""
        llm = _FakeLLM(scripts=[[
            StreamChunk(content="[发送时间：2026-09-10 10:11:12 +0800 星期四]"),
            StreamChunk(content="\n\n    缩进正文"),
            StreamChunk(finish_reason="stop"),
        ]])
        agent = _PipelineHarness(llm, tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("请回复")
        events = await _drain(agent.pipeline.execute(state))
        return state, events

    state, events = asyncio.run(run())
    streamed = "".join(
        event.data for event in events if event.type == EventType.TEXT_CHUNK
    )

    assert streamed == "\n    缩进正文"
    assert state.messages[-1].text == "\n    缩进正文"


def test_pipeline_handles_crlf_separator_split_across_chunks():
    """验证跨片段的 CRLF 分隔符只去除一次。"""
    async def run():
        """由外层测试调用，完成“跨片段的 CRLF 分隔符只去除一次”的异步步骤。"""
        llm = _FakeLLM(scripts=[[
            StreamChunk(content="[发送时间：2026-09-10 10:11:12 +0800 星期四]"),
            StreamChunk(content="\r"),
            StreamChunk(content="\n真正回复"),
            StreamChunk(finish_reason="stop"),
        ]])
        agent = _PipelineHarness(llm, tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("请回复")
        events = await _drain(agent.pipeline.execute(state))
        return state, events

    state, events = asyncio.run(run())
    streamed = "".join(
        event.data for event in events if event.type == EventType.TEXT_CHUNK
    )

    assert streamed == "真正回复"
    assert state.messages[-1].text == "真正回复"


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (["[TODO] ", "保留这段内容"], "[TODO] 保留这段内容"),
        (["[发送", "时间：明天] 保留"], "[发送时间：明天] 保留"),
        (
            ["[发送时间：2026-99-99 99:99:99 +9999 星期四] ", "保留"],
            "[发送时间：2026-99-99 99:99:99 +9999 星期四] 保留",
        ),
        (
            ["正文中的 ", "[发送时间：2026-09-10 10:11:12 +0800 星期四] 要保留"],
            "正文中的 [发送时间：2026-09-10 10:11:12 +0800 星期四] 要保留",
        ),
        (["<开心>", "普通回复"], "<开心>普通回复"),
    ],
)
def test_pipeline_preserves_non_prefix_assistant_text(chunks, expected):
    """验证普通助手正文不会误当时间前缀删除。

    Args:
        chunks (Any): 本次模型流按顺序提供的片段。
        expected (Any): 当前参数化用例预期的输出或状态。
    """
    async def run():
        """由外层测试调用，完成“普通助手正文不会误当时间前缀删除”的异步步骤。"""
        script = [StreamChunk(content=chunk) for chunk in chunks]
        script.append(StreamChunk(finish_reason="stop"))
        agent = _PipelineHarness(_FakeLLM(scripts=[script]), tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("请回复")
        events = await _drain(agent.pipeline.execute(state))
        return state, events

    state, events = asyncio.run(run())
    streamed = "".join(
        event.data for event in events if event.type == EventType.TEXT_CHUNK
    )

    assert streamed == expected
    assert state.messages[-1].text == expected


def test_pipeline_flushes_unmatched_prefix_candidate_before_stream_error():
    """验证模型流错误前释放未匹配成功的前缀文本。"""
    class FailingLLM:
        """提供按用例脚本输出的模型响应，验证流式处理、摘要或失败恢复，不调用真实模型。

        该定义仅用于外层用例：验证模型流错误前释放未匹配成功的前缀文本。
        """
        async def astream(self, messages, tools=None):
            """模型管道调用时按测试脚本顺序产出片段或异常，供外层检查消息和用量处理。

            该定义仅用于外层用例：验证模型流错误前释放未匹配成功的前缀文本。

            Args:
                messages (list[dict]): 按对话顺序排列的消息集合。
                tools (list[dict] | None): 当前向模型公开的工具定义。
            """
            messages = capture_input(messages)
            yield StreamChunk(content="[发送")
            raise RuntimeError("流式连接中断")

    async def run():
        """由外层测试调用，完成“模型流错误前释放未匹配成功的前缀文本”的异步步骤。"""
        agent = _PipelineHarness(FailingLLM(), tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("请回复")
        return await _drain(agent.pipeline.execute(state))

    events = asyncio.run(run())

    assert [
        event.data for event in events if event.type == EventType.TEXT_CHUNK
    ] == ["[发送"]
    assert events[-1].type == EventType.ERROR
    assert events[-1].data == "流式连接中断"


def test_pipeline_stops_buffering_overlong_unclosed_expression_tag():
    """验证超长未闭合表情标签停止缓冲。"""
    leading = "<" + "x" * 300

    async def run():
        """由外层测试调用，完成“超长未闭合表情标签停止缓冲”的异步步骤。"""
        llm = _FakeLLM(scripts=[[
            StreamChunk(content=leading),
            StreamChunk(content="正文"),
            StreamChunk(finish_reason="stop"),
        ]])
        agent = _PipelineHarness(llm, tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("请回复")
        return await _drain(agent.pipeline.execute(state))

    events = asyncio.run(run())

    assert [
        event.data for event in events if event.type == EventType.TEXT_CHUNK
    ] == [leading, "正文"]


@pytest.mark.parametrize("content", [None, "", "   \n\t"])
def test_pipeline_rejects_empty_final_response(content):
    """验证空最终回复明确报错。

    Args:
        content (Any): 当前处理的正文。
    """
    async def run():
        """由外层测试调用，完成“空最终回复明确报错”的异步步骤。"""
        script = [StreamChunk(finish_reason="stop")]
        if content is not None:
            script.insert(0, StreamChunk(content=content))
        agent = _PipelineHarness(_FakeLLM(scripts=[script]), tools=[])
        state = AgentState.create_new("t")
        state.add_user_message("在吗")
        return state, await _drain(agent.pipeline.execute(state))

    state, events = asyncio.run(run())
    assert events[-1].type == EventType.ERROR
    assert events[-1].data == "模型未返回有效文本（finish_reason=stop）"
    assert all(event.type != EventType.DONE for event in events)
    assert [type(message).__name__ for message in state.messages] == ["HumanMessage"]


def test_pipeline_keeps_content_filter_error():
    """验证内容过滤错误保持原语义。"""
    async def run():
        """由外层测试调用，完成“内容过滤错误保持原语义”的异步步骤。"""
        agent = _PipelineHarness(
            _FakeLLM(scripts=[[StreamChunk(finish_reason="refused")]]),
            tools=[],
        )
        state = AgentState.create_new("t")
        state.add_user_message("在吗")
        return await _drain(agent.pipeline.execute(state))

    events = asyncio.run(run())
    assert events[-1].type == EventType.ERROR
    assert events[-1].data == "触发 API 内容过滤"


# ==================== Skill 与 MCP ====================

def test_skill_loads_tools_and_prompt_fragment():
    """验证技能装配工具并追加提示词片段。"""
    from app.agent.agent import Agent, AgentConfig
    from app.agent.skills.base import BaseSkill
    from app.agent.skills.registry import SkillRegistry

    @SkillRegistry.register("test_skill")
    class _TestSkill(BaseSkill):
        """提供当前用例所需的 _TestSkill 测试替身，执行记录仅供外层断言使用。

        该定义仅用于外层用例：验证技能装配工具并追加提示词片段。
        """
        name = "test_skill"
        system_prompt_fragment = "你已获得读文件能力。"
        tools = ["read_file"]
        plugins = []

    async def run():
        """由外层测试调用，完成“技能装配工具并追加提示词片段”的异步步骤。"""
        config = AgentConfig(session_id="t", model_name="m", api_key="k",
                             context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100),
                             system_prompt="基础", skills=["test_skill"])
        agent = Agent(config)
        await agent.initialize()
        return agent

    try:
        agent = asyncio.run(run())
        assert agent.tool_manager.has("read_file"), "Skill 的工具应被注册"
        assert "你已获得读文件能力。" in agent.config.system_prompt
        assert agent.config.system_prompt.startswith("基础")
    finally:
        SkillRegistry.clear()
