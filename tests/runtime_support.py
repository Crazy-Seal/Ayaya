"""为调度测试替身提供真实的输入交付和正常完成事务。"""

from pathlib import Path
import tempfile
import yaml

from app.crud.chat_settings_dao import ChatSettingsDao
from app.environment import get_data_dir
from app.agent.state.manager import SessionStateManager, task_result_messages
from app.schemas.runtime_types import InboxKind
from app.schemas.runtime_types import AgentRunOutcome
from app.schemas.runtime_types import InterruptKind
from app.agent.runtime.turn_control import TurnControl


def pause_test_turn(control: TurnControl, request_id: str) -> None:
    """审批替身保存状态后，通过与 Pipeline 相同的接口确认暂停。"""
    control.start_tool()
    control.run_state.interrupt_saved(control.run_id, InterruptKind.AUTHORIZATION_REQUEST, request_id)


class RuntimeAgentStub:
    """为只替换事件流的调度测试提供结果保存接口，不持有待提交的真实回答。"""

    last_outcome: AgentRunOutcome | None = None

    async def finish_pending_response(self) -> AgentRunOutcome | None:
        """测试场景自行保存输入和结果，此替身没有需要重试的中断材料。"""
        return None


def configured_settings(*session_ids: str) -> ChatSettingsDao:
    """为既有执行测试建立真实、独立的父配置，删除测试可直接操作返回的 DAO。

    Args:
        session_ids (str): 测试父身份；省略时使用既有执行用例的固定身份。

    Returns:
        ChatSettingsDao: 指向测试目录配置文件的真实数据访问对象。
    """
    identities = session_ids or ("chat", "checkpoint-test", "child", "environment-checkpoint", "first",
        "other", "parent", "s", "same", "second", "session", "shared", "test-session")
    directory = Path(tempfile.mkdtemp(prefix="runtime-settings-", dir=get_data_dir()))
    path = directory / "chat_settings.yaml"
    path.write_text(yaml.safe_dump({"chat_models": [{"session_id": identity} for identity in identities]}), encoding="utf-8")
    return ChatSettingsDao(path)


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

    async def ensure_execution_available() -> None:
        """管道记录替身没有运行时身份，允许测试脚本模型开始。"""

    return None if callback is None else SimpleNamespace(
        save_progress=partial(callback, checkpoint_type="intermediate"), ensure_execution_available=ensure_execution_available)
