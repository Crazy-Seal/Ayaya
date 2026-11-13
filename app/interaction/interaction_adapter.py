"""定义渠道提交命令和订阅事件的公共接口。"""

from __future__ import annotations
from app.schemas.runtime_types import CommandReceipt

from collections.abc import AsyncIterator
from typing import Protocol

from app.interaction.interaction_contract import ChannelCommand, InteractionEvent


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
