"""渠道 Adapter 的最小入口；命令入口与事件源以依赖注入连接。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from app.services.interaction_contract import ChannelCommand, CommandReceipt, InteractionEvent


class InteractionAdapter(Protocol):
    """定义渠道提交命令和主动消费事件的最小契约。

    Attributes:
        channel (str): 已注册渠道标识。
    """
    channel: str

    async def submit(self, command: ChannelCommand) -> CommandReceipt:
        """提交已归一化的渠道命令并取得受理结果。

        Args:
            command (ChannelCommand): 统一渠道命令或需要执行的命令正文，类型决定其含义。

        Returns:
            CommandReceipt: 命令受理结果，后续回复通过事件订阅取得。
        """
        ...

    def subscribe(self, session_id: str) -> AsyncIterator[InteractionEvent]:
        """取得指定会话中本渠道可见的事件迭代器。

        Args:
            session_id (str): 共享 Ayaya 会话标识。

        Returns:
            AsyncIterator[InteractionEvent]: 当前会话渠道的展示事件迭代器，不因建立订阅而启动 Agent。
        """
        ...


class InProcessInteractionAdapter:
    """通过注入命令入口和事件源实现进程内渠道，避免 EventHub 持有 Adapter。

    Attributes:
        channel (str): 已注册渠道标识。
        _command_handler (Callable[[ChannelCommand], Awaitable[CommandReceipt]]): 共享命令受理函数。
        _event_source (Callable[[str, str], AsyncIterator[InteractionEvent]]): 按会话和渠道返回事件迭代器的函数。
    """

    def __init__(
        self,
        channel: str,
        command_handler: Callable[[ChannelCommand], Awaitable[CommandReceipt]],
        event_source: Callable[[str, str], AsyncIterator[InteractionEvent]],
    ) -> None:
        """绑定稳定渠道身份及命令和事件端口。

        Args:
            channel (str): 已注册渠道标识。
            command_handler (Callable[[ChannelCommand], Awaitable[CommandReceipt]]): 共享命令受理函数。
            event_source (Callable[[str, str], AsyncIterator[InteractionEvent]]): 按会话和渠道返回事件迭代器的函数。
        """
        self.channel = channel
        self._command_handler = command_handler
        self._event_source = event_source

    async def submit(self, command: ChannelCommand) -> CommandReceipt:
        """拒绝冒用其他渠道的命令，再调用共享受理入口。

        Args:
            command (ChannelCommand): 统一渠道命令或需要执行的命令正文，类型决定其含义。

        Returns:
            CommandReceipt: 命令受理结果，后续回复通过事件订阅取得。
        """
        if command.origin_channel != self.channel:
            return CommandReceipt(False, "命令来源与 Adapter 渠道不匹配")
        return await self._command_handler(command)

    def subscribe(self, session_id: str) -> AsyncIterator[InteractionEvent]:
        """从共享事件源主动消费本渠道的会话视图。

        Args:
            session_id (str): 共享 Ayaya 会话标识。

        Returns:
            AsyncIterator[InteractionEvent]: 当前会话渠道的展示事件迭代器，不因建立订阅而启动 Agent。
        """
        return self._event_source(session_id, self.channel)
