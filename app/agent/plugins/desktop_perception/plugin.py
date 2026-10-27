"""在每次主模型请求前更新桌面环境，并移除可发送的工具截图。"""

import asyncio

from app.agent.plugins.base import BasePlugin, HookContext, PluginHook
from app.agent.state import TemporaryContextItem

from .capture import capture_desktop
from .compression import compact_tool_screenshots


PLUGIN_NAME = "desktop_perception"
DESKTOP_CONTEXT_KEY = f"{PLUGIN_NAME}.screen"


class DesktopPerceptionPlugin(BasePlugin):
    """向模型提供当前主屏幕，按零保留策略压缩工具截图。

    插件通过公开状态方法更新本轮消息，框架负责模型消息组装及 checkpoint 保存。
    截图直接读取屏幕，桌宠和聊天窗口保持当前显隐状态。
    """

    name = PLUGIN_NAME
    description = "每次请求模型前提供当前主屏幕截图，保留前端窗口，并压缩工具返回的截图。"

    @property
    def hooks(self) -> list[PluginHook]:
        """订阅每次模型请求前的执行时机，工具续答时同样更新画面。

        Returns:
            list[PluginHook]: 每次准备主模型输入前触发的钩子。
        """
        return [PluginHook.BEFORE_LLM]

    async def execute(self, context: HookContext) -> None:
        """取得当前桌面后压缩工具截图，再更新同一个环境条目。

        Args:
            context (HookContext): 当前主模型请求对应的 Agent 状态。

        Raises:
            OSError: 桌面捕获或图片编码失败，由插件管理器记录并继续本轮执行。
        """
        state = context.agent_state
        # 1. 先移除上次画面；截图失败时保留已有工具观察，由框架记录捕获错误。
        state.remove_temporary_context(DESKTOP_CONTEXT_KEY)
        image = await asyncio.to_thread(capture_desktop)
        # 2. 当前桌面已取得，工具截图按零保留策略压缩，原消息通过公开方法统一替换。
        messages = state.messages
        candidate = compact_tool_screenshots(messages)
        if candidate != messages:
            state.commit_compaction(candidate)
        # 3. 同一键只保存最新桌面，框架随后组装 ENVIRONMENT 图片对应的用户消息。
        state.set_temporary_context(DESKTOP_CONTEXT_KEY, TemporaryContextItem(
            title="当前桌面", text="当前主屏幕画面。", images=(image,),
        ))
