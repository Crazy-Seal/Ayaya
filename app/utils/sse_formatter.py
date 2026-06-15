"""SSE 事件格式化器

将流式事件转换为 SSE 格式字符串，统一处理事件格式化逻辑。
"""

import json

from app.agent.core.event_router import AgentEvent, EventType


class SSEFormatter:
    """SSE 事件格式化器"""

    @staticmethod
    def format(event: AgentEvent) -> str | None:
        """将事件转换为 SSE 格式字符串。

        Args:
            event: 流式事件对象（agent 的 AgentEvent）

        Returns:
            SSE 格式字符串，如果事件不需要输出则返回 None
        """
        # DONE 由路由 done() 收尾，这里过滤；其余直接复用 AgentEvent.to_sse()。
        if event.type == EventType.DONE:
            return None
        return event.to_sse()

    @staticmethod
    def done() -> str:
        """流结束标记"""
        return "data: [DONE]\n\n"

    @staticmethod
    def error(message: str) -> str:
        """错误事件"""
        data = json.dumps({"detail": message}, ensure_ascii=False)
        return f"event: error\ndata: {data}\n\n"
