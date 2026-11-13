"""通过插件发现、钩子和真实模型执行流程验证桌面环境截图。"""
from app.crud.sqlite_factory import SqliteFactory

import base64
from contextlib import closing
from io import BytesIO
from pathlib import Path
import sqlite3
import threading

from PIL import Image, ImageGrab
import pytest

from app.agent.agent import AgentConfig
from app.agent.context import ContextStrategyConfig, ModelInputCoordinator
from app.agent.core.events import EventType
from app.agent.core.pipeline import ExecutionPipeline
from app.agent.core.plugin_manager import PluginManager
from app.agent.core.tool_manager import ToolManager
from app.agent.models.contracts import StreamChunk
from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.plugins.registry import PluginRegistry
from app.agent.state import AgentState, ImageContent, TemporaryContextItem, ToolCall, ToolMessage, text_content
from app.agent.state.manager import SessionStateManager
from app.agent.tools.core.base import BaseTool, ToolResult
from app.schemas.message_types import ImageContextState, MediaKind
from tests.model_capture import capture_input


@pytest.fixture
def desktop_plugin(monkeypatch: pytest.MonkeyPatch) -> type[BasePlugin]:
    """通过项目自带插件的自动发现记录取得插件类，发现过程不会获取桌面内容。

    Args:
        monkeypatch (pytest.MonkeyPatch): 隔离测试使用的插件目录记录。

    Returns:
        type[BasePlugin]: 包入口导出的桌面环境插件类。
    """
    monkeypatch.setattr(PluginRegistry, "_plugins", {})
    assert PluginRegistry.has("desktop_perception")
    plugin = PluginRegistry.get("desktop_perception")
    assert plugin is not None
    return plugin


@pytest.mark.asyncio
async def test_each_model_request_refreshes_desktop_and_compacts_tool_screenshots(
    desktop_plugin: type[BasePlugin], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """真实工具续答再次截图，工具截图不再发送，环境图片只保存在 checkpoint。

    Args:
        desktop_plugin (type[BasePlugin]): 通过包发现取得的插件类。
        monkeypatch (pytest.MonkeyPatch): 用测试画面替代系统屏幕捕获。
        tmp_path (Path): 当前测试的隔离数据库目录。
    """
    captures = []
    main_thread = threading.get_ident()

    def grab(**kwargs) -> Image.Image:
        """每次产生不同的桌面画面，并记录捕获参数和执行线程。

        Args:
            **kwargs (dict): 插件交给 Pillow 的截图参数。

        Returns:
            Image.Image: 当前模型请求对应的测试画面。
        """
        captures.append((kwargs, threading.get_ident()))
        return Image.new("RGB", (80, 45), "red" if len(captures) == 1 else "blue")

    monkeypatch.setattr(ImageGrab, "grab", grab)
    with Image.new("RGB", (1, 1), "green") as image, BytesIO() as output:
        image.save(output, format="PNG")
        tool_image = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    class ScreenshotTool(BaseTool):
        """提供需要在下一次模型请求前压缩的工具截图。"""

        name = "observe_screen"
        description = "返回工具观察"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, args, context) -> ToolResult:
            """返回截图和需要继续交给模型的动作文字。

            Args:
                args (dict): 本次工具调用的空参数。
                context (ToolContext): 管道提供的工具上下文。

            Returns:
                ToolResult: 带 SCREENSHOT 图片的成功结果。
            """
            return ToolResult.success("动作已完成", image_url=tool_image, media_kind=MediaKind.SCREENSHOT)

    class Model:
        """记录实际请求，先请求工具，再结束回答。"""

        def __init__(self) -> None:
            """建立本次模型调用的请求记录。"""
            self.requests = []

        async def astream(self, messages, tools=None):
            """回放两次模型响应，保留经过真实适配器编码的请求。

            Args:
                messages (Sequence[BaseMessage]): 管道准备的完整模型消息。
                tools (Sequence[ToolDefinition] | None): 模型可选的工具定义。

            Yields:
                StreamChunk: 工具调用或最终回答。
            """
            self.requests.append(capture_input(messages))
            if len(self.requests) == 1:
                yield StreamChunk(tool_call=ToolCall(id="observe", name=ScreenshotTool.name, args={}))
                yield StreamChunk(finish_reason="tool_calls")
            else:
                yield StreamChunk(content="已看到桌面", finish_reason="stop")

    plugins = PluginManager()
    plugins.register(desktop_plugin())
    tools = ToolManager()
    tools.register(ScreenshotTool())
    config = AgentConfig(session_id="desktop-perception", model_name="test", api_key="test",
                         context_strategy=ContextStrategyConfig(compression_trigger_tokens_k=100))
    original_config = config.context_strategy.model_dump()
    model = Model()
    pipeline = ExecutionPipeline(prompt_source=config, llm_client=model, tool_manager=tools,
                                 plugin_manager=plugins,
                                 model_input=ModelInputCoordinator(config.context_strategy, model, accounting_key="test"))
    state = AgentState.create_new(config.session_id)
    state.add_user_message("看一下桌面")
    database = tmp_path / "state.sqlite3"
    manager = SessionStateManager(config.session_id, SqliteFactory(str(database)))
    try:
        events = [event async for event in pipeline.execute(state, session_state_manager=manager)]
        assert not [event for event in events if event.type == EventType.ERROR]
        assert len(model.requests) == len(captures) == 2
        urls = []
        for request in model.requests:
            environment = next(message for message in request if message.get("name") == "runtime_environment_perception")
            urls.append(next(part["image_url"]["url"] for part in environment["content"] if part["type"] == "image_url"))
        assert urls[0] != urls[1]
        assert all(parameters == {"include_layered_windows": True, "all_screens": False}
                   and thread_id != main_thread for parameters, thread_id in captures)
        assert tool_image not in str(model.requests[1])
        assert "动作已完成" in str(model.requests[1])
        tool = next(message for message in state.messages if isinstance(message, ToolMessage))
        screenshot = next(part for part in tool.content if isinstance(part, ImageContent))
        assert screenshot.context_state is ImageContextState.COMPACTED and screenshot.url == ""
        assert config.context_strategy.model_dump() == original_config
        await manager.save_progress(state)
        restored = await manager.load()
        assert next(item for item in restored.temporary_context.values() if item.images).images[0].url == urls[1]
        with closing(sqlite3.connect(database)) as connection:
            history = connection.execute("SELECT message_json FROM conversation_messages").fetchall()
        assert all(url not in str(history) for url in urls)
    finally:
        plugins.clear()
        await manager.close()


@pytest.mark.asyncio
async def test_only_tool_screenshots_are_compacted_and_large_capture_is_scaled(
    desktop_plugin: type[BasePlugin], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """零保留策略压缩最新及历史截图，保留其他图片、原消息身份和历史文件引用。

    Args:
        desktop_plugin (type[BasePlugin]): 包入口提供的插件类。
        monkeypatch (pytest.MonkeyPatch): 注入超出输出尺寸的测试桌面。
    """
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (2560, 1440), "white"))
    state = AgentState.create_new("desktop-compaction")
    human = state.add_user_message("附件", image_urls=("data:image/png;base64,AA==",))
    for index in range(2):
        call = ToolCall(id=f"call-{index}", name="tool", args={})
        state.record_assistant_step("", (call,))
        state.record_tool_result(call, (*text_content("操作说明"),
            ImageContent(f"data:image/png;base64,shot{index}", media_kind=MediaKind.SCREENSHOT,
                         storage_ref=f"screenshots/{index}.png"),
            ImageContent("data:image/png;base64,tool", media_kind=MediaKind.TOOL),
            ImageContent("data:image/png;base64,mcp", media_kind=MediaKind.MCP)))
    other = TemporaryContextItem(title="其他插件", images=(ImageContent(
        "data:image/png;base64,environment", media_kind=MediaKind.ENVIRONMENT),))
    state.set_temporary_context("other.plugin", other)
    before = state.messages
    await desktop_plugin().execute(HookContext.create(PluginHook.BEFORE_LLM, state))
    assert state.messages[0] == human
    assert state.temporary_context["other.plugin"] == other
    for original, current in zip(before, state.messages):
        assert current.message_id == original.message_id and current.created_at == original.created_at
        if isinstance(current, ToolMessage):
            assert current.content[0] == original.content[0]
            assert current.content[2:] == original.content[2:]
            screenshot = current.content[1]
            assert screenshot.url == "" and screenshot.context_state is ImageContextState.COMPACTED
            assert screenshot.storage_ref == original.content[1].storage_ref
            assert original.content[1].url
    image = next(item.images[0] for key, item in state.temporary_context.items() if key != "other.plugin")
    assert image.media_kind is MediaKind.ENVIRONMENT
    with Image.open(BytesIO(base64.b64decode(image.url.split(",", 1)[1]))) as captured:
        assert captured.size == (1920, 1080)


@pytest.mark.asyncio
async def test_failed_capture_drops_stale_desktop_and_keeps_tool_observation(
    desktop_plugin: type[BasePlugin], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """捕获失败时不重复发送旧桌面，并保留已有工具观察供框架继续处理。

    Args:
        desktop_plugin (type[BasePlugin]): 项目自带插件包加载的插件类。
        monkeypatch (pytest.MonkeyPatch): 让下一次系统截图报告失败。
    """
    state = AgentState.create_new("desktop-failure")
    state.add_user_message("查看桌面")
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: Image.new("RGB", (80, 45)))
    plugin = desktop_plugin()
    context = HookContext.create(PluginHook.BEFORE_LLM, state)
    await plugin.execute(context)
    assert len(state.temporary_context) == 1
    call = ToolCall(id="fallback", name="screen", args={})
    state.record_assistant_step("", (call,))
    state.record_tool_result(call, (ImageContent("data:image/png;base64,fallback", media_kind=MediaKind.SCREENSHOT),))
    original = state.messages

    def failed_grab(**kwargs):
        """模拟操作系统无法取得当前桌面。

        Args:
            **kwargs (dict): 本次截图参数。

        Raises:
            OSError: 当前桌面无法捕获。
        """
        raise OSError("桌面不可用")

    monkeypatch.setattr(ImageGrab, "grab", failed_grab)
    with pytest.raises(OSError, match="桌面不可用"):
        await plugin.execute(context)
    assert state.temporary_context == {}
    assert state.messages == original


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.asyncio
async def test_plugin_switch_controls_capture(
    desktop_plugin: type[BasePlugin], monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    """真实 Agent 装配仅在会话启用插件时获取桌面。

    Args:
        desktop_plugin (type[BasePlugin]): 通过项目自带插件目录发现并登记的类。
        monkeypatch (pytest.MonkeyPatch): 替换截图和记忆依赖。
        enabled (bool): 当前会话的插件开关。
    """
    from app.agent.runtime import agent_factory
    from app.schemas.chat_settings import ChatSettings

    calls = []

    def grab(**kwargs) -> Image.Image:
        """记录是否实际获取桌面图像。

        Args:
            **kwargs (dict): 本次捕获参数。

        Returns:
            Image.Image: 无需读取真实桌面的测试图像。
        """
        calls.append(kwargs)
        return Image.new("RGB", (80, 45))

    monkeypatch.setattr(ImageGrab, "grab", grab)
    settings = ChatSettings(session_id="desktop-switch", model_name="test", openai_api_key="test",
                            openai_base_url="http://localhost", temperature=0.7, system_prompt="test",
                            context_strategy={}, agent_plugins={"memory": {"enabled": False},
                            desktop_plugin.name: {"enabled": enabled}})
    agent = agent_factory.build_agent(settings, sqlite_factory=SqliteFactory())
    state = AgentState.create_new(settings.session_id)
    try:
        await agent.initialize()
        await agent.plugin_manager.run_hooks(PluginHook.BEFORE_LLM, state)
        assert len(calls) == int(enabled)
        assert bool(state.temporary_context) is enabled
    finally:
        await agent.close()
