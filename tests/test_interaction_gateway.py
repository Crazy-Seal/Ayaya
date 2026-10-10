import asyncio

from app.services.interaction_contract import InteractionEvent
from app.services.interaction_event_hub import InteractionEventHub
from app.services.interaction_adapter import InProcessInteractionAdapter
from app.services.interaction_contract import ChannelCommand, CommandReceipt


def test_event_hub_routes_only_to_desktop_and_origin() -> None:
    """验证事件只投递桌面及来源渠道。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：事件只投递桌面及来源渠道。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        hub = InteractionEventHub()
        hub.register_channel("qq")
        hub.register_channel("telegram")
        desktop = hub.subscribe("shared", "desktop")
        qq = hub.subscribe("shared", "qq")
        telegram = hub.subscribe("shared", "telegram")
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert (await anext(desktop)).kind == "session_snapshot"
        assert (await anext(qq)).kind == "session_snapshot"
        assert (await anext(telegram)).kind == "session_snapshot"

        await hub.publish(InteractionEvent(
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

    asyncio.run(scenario())


def test_in_process_adapter_uses_shared_command_and_event_contract() -> None:
    """验证进程内 Adapter 与桌面使用同一命令事件契约。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：进程内 Adapter 与桌面使用同一命令事件契约。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        hub = InteractionEventHub()
        hub.register_channel("test")
        commands: list[ChannelCommand] = []

        async def submit(command: ChannelCommand) -> CommandReceipt:
            """记录测试命令并返回受理回执。

            Args:
                command (ChannelCommand): 统一渠道命令或需要执行的命令正文，类型决定其含义。

            Returns:
                CommandReceipt: 命令受理结果，后续回复通过事件订阅取得。
            """
            commands.append(command)
            await hub.publish(InteractionEvent(
                command.session_id, "human_message", command.payload,
                origin_channel=command.origin_channel,
            ))
            return CommandReceipt(True)

        adapter = InProcessInteractionAdapter("test", submit, hub.subscribe)
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

    asyncio.run(scenario())


def test_slow_subscriber_reconnects_from_snapshot_watermark() -> None:
    """验证慢订阅者断开后可按快照水位恢复。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    async def scenario() -> None:
        """在隔离异步环境中安排并验证：慢订阅者断开后可按快照水位恢复。"""
        # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
        hub = InteractionEventHub(queue_size=1)
        stream = hub.subscribe("s", "desktop")
        initial = await anext(stream)
        # 2. 核对公开结果和持久状态，确保场景中的输入、通知或副作用未重复。
        assert initial.event_sequence == 0
        await hub.publish(InteractionEvent("s", "text_chunk", {"value": "a"}))
        await hub.publish(InteractionEvent("s", "text_chunk", {"value": "b"}))
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

    asyncio.run(scenario())
