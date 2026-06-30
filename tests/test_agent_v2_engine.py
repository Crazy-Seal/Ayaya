"""agent 引擎回归测试（离线，无需网络）。

覆盖重构计划中点名的 4 个核心 bug：
- Bug #1：助手消息带 tool_calls 时用枚举 role，不再崩溃
- Bug #2：流式工具调用在流末仅产出一次（不重复执行）
- Bug #3：StateManager 回滚保留 watermark（最后一个良好状态）
- Bug #4：中断恢复能用持久化路由信息重入同一工具，并注入截屏图片
"""

import asyncio
import types

from app.agent.agent import AgentConfig
from app.agent.context import BaseTool, ToolResult
from app.agent.core.event_router import EventRouter, EventType
from app.agent.core.pipeline import ExecutionPipeline, SCREENSHOT_MESSAGE_NAME
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.state_manager import StateManager
from app.agent.core.tool_manager import ToolManager
from app.agent.message import ToolCall, MessageRole, AssistantMessageWithTools
from app.agent.state import AgentState
from app.agent.models.llm_client import LLMClient, LLMConfig, StreamChunk


# ==================== 测试替身 ====================

def _delta(content=None, tool_calls=None, finish_reason=None):
    """构造一个伪 openai 流式 chunk"""
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = types.SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return types.SimpleNamespace(choices=[choice])


def _tc_delta(index, id=None, name=None, arguments=None):
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(index=index, id=id, function=fn)


class _FakeScreenshotTool(BaseTool):
    name = "screenshot"
    description = "截屏"
    parameters_schema = {"type": "object", "properties": {}}
    is_resumable = True

    async def execute(self, args, context):
        if context.resume_data is None:
            return ToolResult.needs_input("screenshot_request", "允许截屏？")
        if not context.resume_data.get("approved"):
            return ToolResult.success("用户拒绝截屏")
        return ToolResult.success("截屏成功", image_url=context.resume_data.get("screenshot_data"))


class _FakeLLM:
    """按调用次序回放脚本的伪 LLM 客户端。"""

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = 0

    async def astream(self, messages, tools=None):
        script = self.scripts[self.calls]
        self.calls += 1
        for chunk in script:
            yield chunk


class _FakeAgent:
    def __init__(self, llm, tools):
        self.config = AgentConfig(session_id="t", model_name="m", api_key="k", system_prompt="sys")
        self.tool_manager = ToolManager()
        for t in tools:
            self.tool_manager.register(t)
        self.plugin_manager = PluginManager()
        self.event_router = EventRouter()
        self.llm_client = llm
        self.pipeline = ExecutionPipeline(self)


async def _drain(agen):
    return [ev async for ev in agen]


# ==================== Bug #2：流式工具调用仅产出一次 ====================

def test_astream_emits_each_tool_call_once():
    async def run():
        client = LLMClient(LLMConfig(model="m", api_key="k", base_url="http://x/v1"))

        async def fake_create(**kwargs):
            async def gen():
                yield _delta(content="好的")
                yield _delta(tool_calls=[_tc_delta(0, id="call_1", name="screenshot", arguments="")])
                yield _delta(tool_calls=[_tc_delta(0, arguments="{}")])
                yield _delta(finish_reason="tool_calls")
            return gen()

        client._client.chat.completions.create = fake_create
        chunks = [c async for c in client.astream([{"role": "user", "content": "hi"}], tools=[{}])]
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


# ==================== Bug #1：带工具调用的助手消息不崩溃 ====================

def test_assistant_message_with_tools_serializes():
    msg = AssistantMessageWithTools(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id="c1", name="screenshot", args={})],
    )
    d = msg.to_openai_format()  # 旧代码传 role="assistant" 字符串会在此 .value 崩溃
    assert d["role"] == "assistant"
    assert d["tool_calls"][0]["function"]["name"] == "screenshot"


# ==================== Bug #3：回滚保留 watermark ====================

def test_rollback_keeps_watermark(tmp_path):
    async def run():
        sm = StateManager("s1", db_path=str(tmp_path / "ck.sqlite3"))
        a = AgentState.create_new("s1"); a.add_assistant_message("A")
        id_a = await sm.save(a)
        b = AgentState.create_new("s1"); b.add_assistant_message("B")
        await sm.save(b)
        # 模拟「本轮开始前 load 记录的良好水位线」指向 A
        sm._watermark = id_a
        ok = await sm.rollback()        # 删除 id > id_a（即 B），保留 watermark A
        loaded = await sm.load()
        await sm.close()
        return ok, loaded

    ok, loaded = asyncio.run(run())
    assert ok is True
    # 旧的 `id >= watermark` bug 会把 A 也删掉；修复后必须保留 A
    last = loaded.messages[-1]
    assert last["content"] == "A", f"回滚后应保留良好状态 A，实得 {last}"


def test_rollback_to_watermark_deletes_after_only(tmp_path):
    async def run():
        sm = StateManager("s1", db_path=str(tmp_path / "ck.sqlite3"))
        a = AgentState.create_new("s1"); a.add_assistant_message("A")
        id_a = await sm.save(a)
        b = AgentState.create_new("s1"); b.add_assistant_message("B")
        await sm.save(b)
        deleted = await sm.rollback_to_watermark(id_a)  # 删除 > id_a，即 B
        loaded = await sm.load()
        await sm.close()
        return deleted, loaded

    deleted, loaded = asyncio.run(run())
    assert deleted == 1
    assert loaded.messages[-1]["content"] == "A"


# ==================== Bug #4：可恢复工具 + 图片注入 完整往返 ====================

def test_resumable_screenshot_roundtrip():
    async def run():
        llm = _FakeLLM(scripts=[
            # 第 1 次：模型请求截屏
            [StreamChunk(tool_call=ToolCall(id="c1", name="screenshot", args={}))],
            # 恢复后第 2 次：模型基于截图作答
            [StreamChunk(content="我看到一只猫")],
        ])
        agent = _FakeAgent(llm, tools=[_FakeScreenshotTool()])
        state = AgentState.create_new("t")
        state.add_user_message("看看我的屏幕")

        events1 = await _drain(agent.pipeline.execute(state))
        assert events1[-1].type == EventType.INTERRUPT
        # 中断路由信息已持久化
        assert state.interrupt_data["tool_name"] == "screenshot"
        assert state.interrupt_data["tool_call_id"] == "c1"
        assert "resume_state" in state.interrupt_data
        # 发给前端的只有最小字段
        client_payload = events1[-1].data
        assert set(client_payload.keys()) == {"type", "request_id", "message"}

        events2 = await _drain(agent.pipeline.resume_tools(
            state, {"approved": True, "screenshot_data": "data:image/png;base64,AAA"}
        ))
        assert events2[-1].type == EventType.DONE
        return state

    state = asyncio.run(run())
    msgs = state.messages
    # 工具槽位只留文本
    assert any(m.get("role") == "tool" and m.get("content") == "截屏成功" for m in msgs)
    # 截图作为 user 消息注入，带 system_screenshot 名
    shot = [m for m in msgs if m.get("role") == "user" and m.get("name") == SCREENSHOT_MESSAGE_NAME]
    assert shot, "应注入一条 system_screenshot 用户消息"
    assert any(p.get("type") == "image_url" for p in shot[0]["content"])
    # 模型最终基于截图作答
    assistant_texts = [m["content"] for m in msgs
                       if m.get("role") == "assistant" and isinstance(m.get("content"), str)]
    assert "我看到一只猫" in assistant_texts
    # 中断状态已清除
    assert state.interrupt_data is None


# ==================== Phase 5：ContextWindowPlugin 接入 pipeline ====================

def test_context_window_plugin_builds_and_pops_window():
    from app.agent.plugins.context_window import ContextWindowPlugin

    async def run():
        llm = _FakeLLM(scripts=[[StreamChunk(content="你好呀")]])
        agent = _FakeAgent(llm, tools=[])
        await agent.plugin_manager.register(ContextWindowPlugin(), agent)
        state = AgentState.create_new("t")
        state.add_user_message("在吗")
        events = await _drain(agent.pipeline.execute(state))
        return state, events

    state, events = asyncio.run(run())
    assert events[-1].type == EventType.DONE
    # 送模型窗口用完即弃，不残留进 state（否则会被持久化）
    assert "llm_messages" not in state.extra
    # state.messages 仍含完整对话
    roles = [m.get("role") for m in state.messages]
    assert roles == ["user", "assistant"]


def test_extract_new_ai_messages_dict():
    from app.agent.plugins.memory import MemoryPlugin

    msgs = [
        {"role": "user", "content": "看屏幕"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "screenshot", "arguments": "{}"}}]},
        {"role": "tool", "content": "截屏成功", "name": "screenshot", "tool_call_id": "c1"},
        {"role": "user", "content": [{"type": "text", "text": "[系统消息]屏幕截图: "},
                                     {"type": "image_url", "image_url": {"url": "data:.."}}],
         "name": "system_screenshot"},
        {"role": "assistant", "content": "我看到一只猫"},
    ]
    ai = MemoryPlugin()._extract_new_ai_messages(msgs)
    # 截图是 system_screenshot（非真实人类），最后真实人类是 "看屏幕"，其后两条 assistant
    assert [m["content"] for m in ai] == ["", "我看到一只猫"]
    assert ai[0]["tool_calls"] == [{"name": "screenshot"}]
    assert ai[1]["tool_calls"] == []


# ==================== Phase 7：SSE 字节兼容 + v2 service ====================

def test_sse_formatter_v2_events_byte_compatible():
    from app.agent.core.event_router import AgentEvent, EventType as ET
    from app.utils.sse_formatter import SSEFormatter

    f = SSEFormatter.format
    assert f(AgentEvent(ET.TEXT_CHUNK, "你好")) == 'data: {"response": "你好"}\n\n'
    assert f(AgentEvent(ET.TOOL_CALL, "screenshot")) == \
        'event: tool_call\ndata: {"tool_name": "screenshot"}\n\n'
    interrupt_val = {"type": "screenshot_request", "request_id": "r1", "message": "允许？"}
    assert f(AgentEvent(ET.INTERRUPT, interrupt_val)) == \
        'event: interrupt\ndata: {"value": {"type": "screenshot_request", "request_id": "r1", "message": "允许？"}}\n\n'
    # DONE 交给路由 done()，format 返回 None
    assert f(AgentEvent(ET.DONE, None)) is None


def test_agent_service_v2_stream_and_close():
    from app.agent.core.event_router import AgentEvent, EventType as ET
    from app.services.agent_service import AgentService
    from app.utils.sse_formatter import SSEFormatter

    class _StubAgent:
        def __init__(self):
            self.closed = False

        async def run(self, message, images=None):
            yield AgentEvent(ET.TEXT_CHUNK, "在")
            yield AgentEvent(ET.TOOL_CALL, "search_memory")
            yield AgentEvent(ET.TEXT_CHUNK, "的")
            yield AgentEvent(ET.DONE, None)

        async def close(self):
            self.closed = True

    stub = _StubAgent()

    class _CS:
        model_name = "m"

    svc = AgentService(
        chat_history_dao=None,
        chat_settings_loader=lambda sid: _CS(),
        agent_factory=lambda cs: stub,
    )

    async def run():
        out = []
        async for ev in svc.stream_chat(_AInput("在吗"), "s"):
            sse = SSEFormatter.format(ev)
            if sse:
                out.append(sse)
        return out

    sse_list = asyncio.run(run())
    assert sse_list == [
        'data: {"response": "在"}\n\n',
        'event: tool_call\ndata: {"tool_name": "search_memory"}\n\n',
        'data: {"response": "的"}\n\n',
    ]
    # 正常结束（无中断）后 agent 被关闭
    assert stub.closed is True


class _AInput:
    """最小化的 AgentInput 替身（避免引入 pydantic 校验）。"""
    def __init__(self, message, images=None):
        self.message = message
        self.images = images


def test_agent_service_v2_error_raises_and_closes():
    from app.agent.core.event_router import AgentEvent, EventType as ET
    from app.services.agent_service import AgentService

    class _StubAgent:
        def __init__(self):
            self.closed = False

        async def run(self, message, images=None):
            yield AgentEvent(ET.TEXT_CHUNK, "x")
            yield AgentEvent(ET.ERROR, "boom")

        async def close(self):
            self.closed = True

    stub = _StubAgent()

    class _CS:
        model_name = "m"

    svc = AgentService(
        chat_history_dao=None,
        chat_settings_loader=lambda sid: _CS(),
        agent_factory=lambda cs: stub,
    )

    async def run():
        got_error = False
        try:
            async for _ev in svc.stream_chat(_AInput("hi"), "s"):
                pass
        except RuntimeError:
            got_error = True
        return got_error

    got_error = asyncio.run(run())
    assert got_error is True
    assert stub.closed is True


# ==================== Phase 8：Skill + MCP ====================

def test_skill_loads_tools_and_prompt_fragment():
    from app.agent.agent import Agent, AgentConfig
    from app.agent.skills.base import BaseSkill
    from app.agent.skills.registry import SkillRegistry

    @SkillRegistry.register("test_skill")
    class _TestSkill(BaseSkill):
        name = "test_skill"
        system_prompt_fragment = "你已获得读文件能力。"
        tools = ["read_file"]
        plugins = []

    async def run():
        config = AgentConfig(session_id="t", model_name="m", api_key="k",
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


def test_mcp_plugin_graceful_without_sdk():
    """未安装 mcp SDK 时，on_register 应优雅降级（不抛异常、不注册工具）。"""
    from app.agent.mcp.plugin import MCPPlugin
    from app.agent.core.tool_manager import ToolManager

    class _FakeAgent:
        def __init__(self):
            self.tool_manager = ToolManager()

    async def run():
        plugin = MCPPlugin({"name": "x", "transport": "stdio", "command": "noop"})
        agent = _FakeAgent()
        await plugin.on_register(agent)  # mcp 未安装 → 直接返回
        await plugin.on_unregister()
        return plugin, agent

    plugin, agent = asyncio.run(run())
    assert plugin.tools == []
    assert len(agent.tool_manager) == 0
