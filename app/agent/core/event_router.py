"""
事件路由器

负责 Agent 内部事件通知和事件类型定义。
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """定义 Agent 对外报告的输入提交、文字、工具、中断、错误和轮次结束事实。"""
    RUN_STARTED = "run_started"  # 已加载并核验本次执行，由 Scheduler 发布既有开始事件
    HUMAN_INPUT_COMMITTED = "human_input_committed"  # 真人输入及其交付状态已保存
    TEXT_CHUNK = "text_chunk"       # 文本分片
    TOOL_CALL = "tool_call"         # 工具调用
    INTERRUPT = "interrupt"         # 中断
    ERROR = "error"                 # 错误
    DONE = "done"                   # 完成


@dataclass
class AgentEvent:
    """携带 Agent 内部事件类型和内容，由 Scheduler 转成渠道可见事件。

    Attributes:
        type (EventType): Agent 内部事件类型。
        data (Any): 待还原的领域存储值。
    """
    type: EventType
    data: Any


class EventRouter:
    """事件路由器 - 管理 Agent 内部事件通知"""

    def __init__(self):
        """创建空事件订阅列表，订阅者由 Agent 或调用方随后注册。"""
        self._subscribers: list[Callable[[AgentEvent], Awaitable[None]]] = []

    def subscribe(self, callback: Callable[[AgentEvent], Awaitable[None]]) -> None:
        """登记异步事件接收函数，后续 Agent 事件按订阅列表交付。

        Args:
            callback (Callable[[AgentEvent], Awaitable[None]]): Agent 事件到达时接收该事件的异步回调。
        """
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[AgentEvent], Awaitable[None]]) -> None:
        """移除指定接收函数，后续事件不再交给该订阅者。

        Args:
            callback (Callable[[AgentEvent], Awaitable[None]]): Agent 事件到达时接收该事件的异步回调。
        """
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    async def emit(self, event_type: EventType, data: Any) -> None:
        """把当前 Agent 事件交给已登记订阅者，并记录单个订阅者的处理失败。

        Args:
            event_type (EventType): 当前发布的 Agent 事件类别。
            data (Any): 需要解析或保存的结构化数据。
        """
        event = AgentEvent(type=event_type, data=data)
        for callback in self._subscribers:
            try:
                await callback(event)
            except Exception as e:
                logger.error(f"事件订阅者回调失败: {e}")

    async def emit_text(self, text: str) -> None:
        """将模型刚生成的文字片段包装为文字事件并交给订阅者。

        Args:
            text (str): 当前处理的文本。
        """
        await self.emit(EventType.TEXT_CHUNK, text)

    async def emit_tool_call(self, tool_name: str) -> None:
        """将本次模型请求的工具名称和参数包装为工具提示事件。

        Args:
            tool_name (str): 注册表中使用的工具名称。
        """
        await self.emit(EventType.TOOL_CALL, tool_name)

    async def emit_interrupt(self, interrupt_data: dict) -> None:
        """向订阅者交付等待审批或桌面执行的中断信息。

        Args:
            interrupt_data (dict): 当前等待审批或桌面执行的恢复信息。
        """
        await self.emit(EventType.INTERRUPT, interrupt_data)

    async def emit_error(self, error: str | Exception) -> None:
        """将当前执行失败转换为错误事件，交给会话调度器或界面处理。

        Args:
            error (str | Exception): 当前捕获的异常。
        """
        await self.emit(EventType.ERROR, str(error))

    async def emit_done(self) -> None:
        """告知订阅者当前 Agent 轮次已经结束，不表示整个会话被关闭。"""
        await self.emit(EventType.DONE, None)
