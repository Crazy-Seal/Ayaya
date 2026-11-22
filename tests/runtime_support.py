"""为调度测试替身提供真实的输入交付和正常完成事务。"""
from app.crud.sqlite_factory import SqliteFactory

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
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.schemas.runtime_types import TaskStatus
import asyncio
from weakref import WeakKeyDictionary


_runtime_observers = WeakKeyDictionary()


def build_test_scheduler(*, runtime_queue, agent_factory, session_state_manager_factory,
                         chat_settings_dao, mutation_coordinator=None, task_supervisor=None,
                         subagent_factory=None, subagent_dao=None,
                         cleanup_session_connections=None, scheduler_type=None):
    """为场景装配真实共享持久化、Hub、父子工厂，模型由场景替换。

    Args:
        runtime_queue (asyncio.Queue): 此用例的唯一运行队列。
        agent_factory (Callable): 场景的父模型执行脚本。
        session_state_manager_factory (Callable): 使用隔离路径的真实 Manager 工厂。
        chat_settings_dao (ChatSettingsDao): 此用例的真实身份配置。
        mutation_coordinator (SettingsMutationCoordinator | None): 场景指定的配置协调对象。
        task_supervisor (TaskSupervisor | None): 场景指定的普通任务管理器。
        subagent_factory (Callable | None): 场景的子执行脚本。
        subagent_dao (SubagentDao | None): 场景指定的子存储。
        cleanup_session_connections (Callable | None): 场景观察的共享连接关闭。

    Returns:
        AgentRuntimeScheduler: 使用目标生产装配函数创建的真实调度对象。
    """
    from app.agent.runtime.runtime_factory import assemble_runtime_scheduler
    from app.crud.sqlite_factory import SqliteFactory
    from app.crud.task_record_dao import TaskRecordDao
    from app.crud.subagent_dao import SubagentDao
    from app.services.subagent_service import SubagentService
    from app.services.agent_identity_service import AgentIdentityService
    from app.services.settings_mutation import SettingsMutationCoordinator

    from app.schemas.agent_storage import AgentStorageScope
    from app.interaction.interaction_event_hub import InteractionEventHub

    probe = session_state_manager_factory("__test_factory__")
    sqlite_factory = SqliteFactory(probe.db_path)
    records = task_supervisor.records if task_supervisor is not None else TaskRecordDao(sqlite_factory)
    supervisor = task_supervisor or TaskSupervisor(runtime_queue=runtime_queue, records=records)
    definitions = subagent_dao or SubagentDao(sqlite_factory)
    identity = AgentIdentityService(chat_settings_dao, definitions)
    events = asyncio.Queue()

    class RecordingHub(InteractionEventHub):
        """观察实际 Hub 发布结果，业务事件仍只经生产队列进入 Hub。"""

        def publish(self, event):
            """保存本次已编号发布，供场景按可观察事件等待。"""
            published = super().publish(event)
            events.put_nowait(published)
            return published

        async def close(self):
            """结束测试观察流，保留应用负责关闭 Hub 的资源边界。"""
            await super().close()
            events.put_nowait(None)

    hub = RecordingHub()

    def child_manager(definition):
        """让子历史使用与本用例主表相同的隔离文件和独立连接。"""
        return SessionStateManager(definition.subagent_id, sqlite_factory,
            storage_scope=AgentStorageScope.SUBAGENT, parent_session_id=definition.session_id)

    def unavailable_child(definition, manager):
        """只测试父场景时，明确拒绝未提供脚本的子创建。"""
        raise RuntimeError("测试未配置子执行脚本")

    subagents = SubagentService(definitions, records, identity, manager_factory=child_manager)
    from contextlib import nullcontext
    from unittest.mock import patch
    from app.agent.runtime import runtime_factory
    replacement = patch.object(runtime_factory, "AgentRuntimeScheduler", scheduler_type) if scheduler_type else nullcontext()
    with replacement:
        scheduler = assemble_runtime_scheduler(runtime_queue=runtime_queue, agent_factory=agent_factory,
            subagent_factory=subagent_factory or unavailable_child, manager_factory=session_state_manager_factory,
            child_manager_factory=child_manager, identity=identity, subagents=subagents, task_supervisor=supervisor, mutation_coordinator=mutation_coordinator or SettingsMutationCoordinator(), hub=hub,
            cleanup_session_connections=cleanup_session_connections)
    _runtime_observers[scheduler] = (hub, events, definitions, records, supervisor)
    return scheduler


def runtime_hub(scheduler):
    """取得测试装配持有的真实 Hub，供订阅协议场景使用。"""
    return _runtime_observers[scheduler][0]


async def next_runtime_event(scheduler):
    """等待生产路径已发布的下一个事件，保持观察行为与执行控制独立。"""
    return await _runtime_observers[scheduler][1].get()


async def close_test_runtime(scheduler):
    """按实际应用顺序关闭测试拥有的生产者、消费者及共享持久连接。"""
    hub, events, definitions, records, supervisor = _runtime_observers[scheduler]
    await scheduler.begin_shutdown()
    await scheduler.wait_for_workers()
    await supervisor.close()
    await scheduler.close()
    await hub.close()
    await definitions.close()
    await records.close()


async def collect_agent_events(operation, *args, **kwargs):
    """直接调用新的 Agent 结果接口，收集此测试需要观察的过程事件。

    Args:
        operation (Callable): Agent 的运行或恢复入口。
        args (tuple): 输入正文等位置参数。
        kwargs (dict): 输入批次、控制等具名参数。

    Returns:
        list[AgentEvent]: 本次执行实际发布的有序事件。
    """
    _outcome, events = await collect_agent_result(operation, *args, **kwargs)
    return events


async def collect_agent_result(operation, *args, **kwargs):
    """直接调用 Agent 并分别返回最终结果与过程事件，供保存故障场景验证。

    Args:
        operation (Callable): 新的 Agent 运行或恢复入口。
        args (tuple): 入口的位置输入。
        kwargs (dict): 当前批次和控制能力。

    Returns:
        tuple: 结构化执行结果及按顺序观察到的事件。
    """
    events = []

    async def receive(event):
        """记录业务过程，最终结果通过调用返回值取得。"""
        events.append(event)

    outcome = await operation(*args, emit=receive, **kwargs)
    return outcome, events


def make_execution_control(run_id):
    """为直接 Agent／Pipeline 测试显式创建执行状态和借用控制能力。

    Args:
        run_id (str): 本次测试的固定执行身份。

    Returns:
        tuple: 执行状态及本轮模型工具控制对象。
    """
    from app.agent.runtime.agent_run_state_machine import AgentRunStateMachine
    machine = AgentRunStateMachine("test-context")
    machine.begin_run(run_id)
    return machine, TurnControl(run_id, machine)


def pause_test_turn(control: TurnControl, request_id: str) -> None:
    """审批替身保存状态后，通过与 Pipeline 相同的接口确认暂停。"""
    control.start_tool()
    control.wait_saved(InterruptKind.AUTHORIZATION_REQUEST, request_id)


class RuntimeAgentStub:
    """标识返回明确结果的脚本 Agent，具体场景自行保存输入和最终结果。"""


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


async def finish_task_record(supervisor: TaskSupervisor, task_id: str, status: TaskStatus | str) -> bool:
    """为消费端场景提交真实任务结果，并投递提交后的记录。

    Args:
        supervisor (TaskSupervisor): 测试持有的任务管理器，提供共享存储和队列。
        task_id (str): 夹具已经登记的任务编号。
        status (TaskStatus | str): 场景要求的业务终态。
    Returns:
        bool: 是否首次提交，重复完成保持唯一通知。
    """
    record = await supervisor.records.finish_once(task_id, TaskStatus(status))
    if record is not None:
        supervisor.runtime_queue.put_nowait(record)
    return record is not None


async def finish_test_turn(
    path, inputs, *, message=None, runtime_inputs=(), release_queued=False,
) -> None:
    """模拟 Agent 的最终保存，使测试不依赖 Scheduler 重复放行。

    Args:
        path (str): 测试数据库路径。
        inputs (tuple[InboxEnvelope, ...]): 本次已经读取的输入。
        message (str | None): 真人消息正文，内部通知轮次为空。
        runtime_inputs (tuple): 要加入当前状态的内部通知。
        release_queued (bool): 仅正常真人轮次完成时放行排队输入。
    """
    manager = SessionStateManager(inputs[0].session_id, SqliteFactory(path))
    try:
        state = await manager.load()
        for item in runtime_inputs or task_result_messages(tuple(item for item in inputs if item.kind == InboxKind.TASK_TERMINAL)):
            state.append_input(item)
        if message is not None:
            state.add_user_message(message)
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
