"""为调度测试替身提供真实的输入交付和正常完成事务。"""

from app.agent.state.manager import StateManager


async def finish_test_turn(
    path, inputs, *, message=None, origin="desktop", runtime_inputs=(), release_queued=False,
) -> None:
    """模拟 Agent 的最终保存，使测试不依赖 Scheduler 重复放行。

    Args:
        path (str): 测试数据库路径。
        inputs (tuple[InboxEnvelope, ...]): 本次已经读取的输入。
        message (str | None): 真人消息正文，内部通知轮次为空。
        origin (str): 真人输入来源。
        runtime_inputs (tuple): 要加入当前状态的内部通知。
        release_queued (bool): 仅正常真人轮次完成时放行排队输入。
    """
    manager = StateManager(inputs[0].session_id, path)
    try:
        state = await manager.load()
        for item in runtime_inputs:
            state.append_input(item)
        if message is not None:
            state.add_user_message(message, origin_channel=origin)
        await manager.save(state, checkpoint_type="completed", inbox_inputs=inputs, release_queued=release_queued)
    finally:
        await manager.close()
