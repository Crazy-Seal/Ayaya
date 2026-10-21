"""为调度测试替身提供真实的输入交付和正常完成事务。"""

from app.agent.state.manager import SessionStateManager, task_result_messages
from app.schemas.runtime_types import InboxKind


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
    manager = SessionStateManager(inputs[0].session_id, path)
    try:
        state = await manager.load()
        for item in runtime_inputs or task_result_messages(tuple(item for item in inputs if item.kind == InboxKind.TASK_TERMINAL)):
            state.append_input(item)
        if message is not None:
            state.add_user_message(message, origin_channel=origin)
        await manager.save(state, checkpoint_type="completed", inbox_inputs=inputs, release_queued=release_queued)
    finally:
        await manager.close()


def checkpoint_probe(callback):
    """把测试记录函数作为 Pipeline 的状态业务替身，不访问真实数据库。

    Args:
        callback (Callable | None): 记录或拒绝中间保存的测试函数。

    Returns:
        SimpleNamespace | None: 只实现 save_progress 的测试对象，未启用存储时为空。
    """
    from functools import partial
    from types import SimpleNamespace

    return None if callback is None else SimpleNamespace(save_progress=partial(callback, checkpoint_type="intermediate"))
