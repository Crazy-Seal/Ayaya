"""上下文窗口插件。

把窗口管理拆成两个钩子：
- BEFORE_LLM：构造「送模型窗口」（截图压缩 + 保留最近 10 条人类消息 + 规范化），
  写入 state.extra["llm_messages"]，供 pipeline._build_messages 使用；不改 state.messages。
- BEFORE_RESPONSE：对 state.messages 做「checkpoint 裁剪」（截图压缩 + 保留最近 20 条人类消息），
  控制持久化体积。

不在送模型前直接改 state.messages——以免记忆抽取丢失完整历史。
"""

import logging

from app.agent.context import BasePlugin, PluginHook, HookContext
from app.agent.utils.domain.window import (
    compress_screenshot_messages,
    slice_recent_messages_by_human,
)
from app.agent.utils.infra.constants import (
    MAX_HUMAN_MESSAGES_IN_CHECKPOINT,
    RECENT_CONTEXT_HUMAN_MESSAGES,
)
from app.agent.utils.domain.text import normalize_messages_for_model

logger = logging.getLogger(__name__)


class ContextWindowPlugin(BasePlugin):
    name = "context_window"
    version = "1.0.0"
    priority = 200  # 晚于 MemoryPlugin(100)，确保 BEFORE_RESPONSE 时记忆已读到完整历史

    @property
    def hooks(self) -> list[PluginHook]:
        return [PluginHook.BEFORE_LLM, PluginHook.BEFORE_RESPONSE]

    async def execute(self, context: HookContext) -> HookContext:
        state = context.agent_state
        if context.hook == PluginHook.BEFORE_LLM:
            msgs = compress_screenshot_messages(state.messages)
            msgs = slice_recent_messages_by_human(msgs, RECENT_CONTEXT_HUMAN_MESSAGES)
            msgs = normalize_messages_for_model(msgs)
            state.extra["llm_messages"] = msgs
        elif context.hook == PluginHook.BEFORE_RESPONSE:
            msgs = compress_screenshot_messages(state.messages)
            msgs = slice_recent_messages_by_human(msgs, MAX_HUMAN_MESSAGES_IN_CHECKPOINT)
            state.messages = msgs
            state.extra.pop("llm_messages", None)
        return context
