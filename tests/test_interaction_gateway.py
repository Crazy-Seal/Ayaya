from tests.runtime_event_source import EventSource
from app.services.channel_subscription_service import ChannelSubscriptionService
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from app.interaction.interaction_contract import InteractionEvent
from app.interaction.interaction_event_hub import InteractionEventHub
from app.interaction.interaction_adapter import InteractionAdapter
from app.interaction.interaction_contract import ChannelCommand, CommandReceipt


class InProcessInteractionAdapter:
    """供本文件测试使用，将测试命令处理函数与 EventHub 订阅连接起来。

    Attributes:
        channel (str): 当前测试渠道的固定标识。
        _command_handler (Callable[[ChannelCommand], Awaitable[CommandReceipt]]): 测试提供的命令处理函数。
        _event_source (Callable[[str, str], AsyncIterator[InteractionEvent]]): 测试 EventHub 的订阅方法。
    """

    def __init__(
        self,
        channel: str,
        command_handler: Callable[[ChannelCommand], Awaitable[CommandReceipt]],
        event_source: Callable[[str, str], AsyncIterator[InteractionEvent]],
    ) -> None:
        """保存测试渠道身份、命令处理函数和事件订阅方法。

        Args:
            channel (str): 当前测试渠道的固定标识。
            command_handler (Callable[[ChannelCommand], Awaitable[CommandReceipt]]): 测试提供的命令处理函数。
            event_source (Callable[[str, str], AsyncIterator[InteractionEvent]]): 测试 EventHub 的订阅方法。
        """
        self.channel = channel
        self._command_handler = command_handler
        self._event_source = event_source

    async def submit(self, command: ChannelCommand) -> CommandReceipt:
        """拒绝其他渠道的命令，通过检查后调用测试提供的处理函数。

        Args:
            command (ChannelCommand): 测试提交的渠道命令。

        Returns:
            CommandReceipt: 来源不匹配时的拒绝回执，或测试处理函数返回的回执。
        """
        if command.origin_channel != self.channel:
            return CommandReceipt(False, "命令来源与 Adapter 渠道不匹配")
        return await self._command_handler(command)

    def subscribe(self, session_id: str) -> AsyncIterator[InteractionEvent]:
        """将会话编号和固定渠道身份交给测试 EventHub，取得事件迭代器。

        Args:
            session_id (str): 本次测试订阅的会话编号。

        Returns:
            AsyncIterator[InteractionEvent]: 测试 EventHub 提供的当前状态及后续事件。
        """
        return self._event_source(session_id, self.channel)


def test_event_hub_routes_only_to_desktop_and_origin() -> None:
    """验证事件只投递桌面及来源渠道。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：事件只投递桌面及来源渠道。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        source = EventSource()
        subscriptions = ChannelSubscriptionService()
        hub = InteractionEventHub(source, subscriptions=subscriptions)
        await hub.start()
        subscriptions.register_channel("qq")
        subscriptions.register_channel("telegram")
        desktop = hub.subscribe("shared", "desktop")
        qq = hub.subscribe("shared", "qq")
        telegram = hub.subscribe("shared", "telegram")
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert (await anext(desktop)).kind == "session_snapshot"
        assert (await anext(qq)).kind == "session_snapshot"
        assert (await anext(telegram)).kind == "session_snapshot"

        await source.publish(InteractionEvent(
            session_id="shared", kind="human_message_committed",
            origin_channel="qq", payload={"message_id": "m1", "text": "你好"},
        ))

        assert (await asyncio.wait_for(anext(desktop), 1)).payload["message_id"] == "m1"
        assert (await asyncio.wait_for(anext(qq), 1)).payload["message_id"] == "m1"
        try:
            await asyncio.wait_for(anext(telegram), 0.05)
        except TimeoutError:
            pass
        else:
            raise AssertionError("无关渠道不应收到 QQ 消息")
        await desktop.aclose()
        await qq.aclose()
        await telegram.aclose()
        await hub.close()

    asyncio.run(scenario())


def test_in_process_adapter_uses_shared_command_and_event_contract() -> None:
    """验证进程内 Adapter 与桌面使用同一命令事件契约。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：进程内 Adapter 与桌面使用同一命令事件契约。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        source = EventSource()
        subscriptions = ChannelSubscriptionService()
        hub = InteractionEventHub(source, subscriptions=subscriptions)
        await hub.start()
        subscriptions.register_channel("test")
        commands: list[ChannelCommand] = []

        async def submit(command: ChannelCommand) -> CommandReceipt:
            """记录测试命令并返回受理回执。

            Args:
                command (ChannelCommand): 统一渠道命令或需要执行的命令正文，类型决定其含义。

            Returns:
                CommandReceipt: 命令受理结果，后续回复通过事件订阅取得。
            """
            commands.append(command)
            await source.publish(InteractionEvent(
                command.session_id, "human_message", command.payload,
                origin_channel=command.origin_channel,
            ))
            return CommandReceipt(True)

        adapter: InteractionAdapter = InProcessInteractionAdapter("test", submit, hub.subscribe)
        events = adapter.subscribe("s")
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert (await anext(events)).kind == "session_snapshot"
        wrong = await adapter.submit(ChannelCommand("s", "desktop", "human_turn", {"message": "x"}))
        assert not wrong.accepted and not commands
        accepted = await adapter.submit(ChannelCommand("s", "test", "human_turn", {"message": "你好"}))
        assert accepted.accepted
        event = await asyncio.wait_for(anext(events), 1)
        assert event.origin_channel == "test" and event.payload["message"] == "你好"
        await events.aclose()
        await hub.close()

    asyncio.run(scenario())


def test_slow_subscriber_reconnects_from_snapshot_watermark() -> None:
    """验证慢订阅者断开后可按快照水位恢复。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：慢订阅者断开后可按快照水位恢复。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        source = EventSource()
        subscriptions = ChannelSubscriptionService()
        hub = InteractionEventHub(source, subscriptions=subscriptions, queue_size=1)
        await hub.start()
        stream = hub.subscribe("s", "desktop")
        initial = await anext(stream)
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert initial.event_sequence == 0
        await source.publish(InteractionEvent("s", "text_chunk", {"value": "a"}))
        await source.publish(InteractionEvent("s", "text_chunk", {"value": "b"}))
        try:
            await anext(stream)
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("慢订阅者应断开并从快照恢复")
        replacement = hub.subscribe("s", "desktop")
        snapshot = await anext(replacement)
        assert snapshot.kind == "session_snapshot"
        assert snapshot.stream_epoch == initial.stream_epoch
        assert snapshot.event_sequence == 2
        await replacement.aclose()
        await hub.close()

    asyncio.run(scenario())
