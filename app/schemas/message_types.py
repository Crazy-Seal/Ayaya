"""消息、图片与聊天历史使用的固定业务取值。"""

from enum import StrEnum


class MessageType(StrEnum):
    """区分持久会话中的业务消息类别。

    Attributes:
        HUMAN (MessageType): 真人输入。
        SYSTEM (MessageType): 系统指令。
        ASSISTANT (MessageType): Agent 回复。
        TOOL (MessageType): 工具执行结果。
        CONTEXT_SUMMARY (MessageType): 压缩后的前情摘要。
        TASK_RESULT (MessageType): 后台任务结束通知。
        PLUGIN_NOTIFICATION (MessageType): 带插件来源和通知名称的持久内容。
        ENVIRONMENT_PERCEPTION (MessageType): 环境变化说明。
        SUBAGENT (MessageType): 子 Agent 消息。
    """

    HUMAN = "human"
    SYSTEM = "system"
    ASSISTANT = "assistant"
    TOOL = "tool"
    CONTEXT_SUMMARY = "context_summary"
    TASK_RESULT = "task_result"
    PLUGIN_NOTIFICATION = "plugin_notification"
    ENVIRONMENT_PERCEPTION = "environment_perception"
    SUBAGENT = "subagent"


class MediaKind(StrEnum):
    """区分图片来源，供保存、压缩和模型输入转换共同使用。

    Attributes:
        HUMAN (MediaKind): 真人发送的附件。
        TOOL (MediaKind): 本地工具返回的图片。
        SCREENSHOT (MediaKind): 可从活跃上下文压缩的截图。
        MCP (MediaKind): MCP 工具返回的图片。
    """

    HUMAN = "human"
    TOOL = "tool"
    SCREENSHOT = "screenshot"
    MCP = "mcp"


class ImageContextState(StrEnum):
    """表示当前上下文能否向模型发送图片。

    Attributes:
        AVAILABLE (ImageContextState): 保留可发送的图片地址。
        COMPACTED (ImageContextState): 旧截图已经压缩，模型只读取占位说明。
    """

    AVAILABLE = "available"
    COMPACTED = "compacted"


class HistoryRole(StrEnum):
    """区分聊天历史向桌面界面输出的展示角色。

    Attributes:
        HUMAN (HistoryRole): 真人消息。
        AI (HistoryRole): Agent 回复或工具图片。
        AI_TOOL_CALLING (HistoryRole): 工具调用提示。
        PLUGIN (HistoryRole): 已持久化的插件通知。
    """

    HUMAN = "Human"
    AI = "AI"
    AI_TOOL_CALLING = "AI_Tool_Calling"
    PLUGIN = "Plugin"


LEGACY_TOOL_NOTICE_KIND = "legacy_tool_notice"
COMPRESSED_SCREENSHOT_PLACEHOLDER = "[系统消息]已被压缩的旧截图"
