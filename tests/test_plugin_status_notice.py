"""通过正式插件发布和会话订阅验证持续提示。"""

import asyncio
from pathlib import Path

import pytest

from app.agent.agent import Agent
from app.agent.plugins.runtime import PluginRuntime
from app.agent.runtime.agent_execution import AgentExecution
from app.agent.runtime.parent_session import ParentSession
from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.crud.sqlite_factory import SqliteFactory
from app.crud.task_record_dao import TaskRecordDao
from app.routes.agent import observe_runtime
from app.schemas.runtime_types import CommandStatus
from app.schemas.task_types import PluginRequest, PluginRequestKind
from tests.runtime_support import build_test_scheduler, close_test_runtime, configured_settings, runtime_hub


def test_status_notice_lifecycle_keeps_source_and_does_not_become_agent_input(tmp_path: Path) -> None:
    """插件可持续更新提示，事件保留可信来源且不写入对话或唤醒 Agent。

    Args:
        tmp_path (Path): 仅供本测试使用的临时存储目录。
    """

    async def scenario() -> None:
        """从真实观察入口消费发布结果，再通过状态和历史位置检查持久内容。"""
        sqlite = SqliteFactory(tmp_path / "state.sqlite3")
        queue: asyncio.Queue[RuntimeRequest] = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(sqlite))
        agent_calls: list[str] = []

        def create_agent(session_id: str, manager: SessionStateManager) -> Agent:
            """记录意外唤醒，持续提示本身没有模型执行需求。

            Args:
                session_id (str): 被唤醒的测试会话。
                manager (SessionStateManager): 应用准备借给 Agent 的会话状态。

            Raises:
                AssertionError: 纯展示事件意外触发 Agent 创建。
            """
            agent_calls.append(session_id)
            raise AssertionError("持续提示不能唤醒 Agent")

        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            chat_settings_dao=configured_settings("s"), agent_factory=create_agent,
            session_state_manager_factory=lambda session: SessionStateManager(session, sqlite))
        manager = SessionStateManager("s", sqlite)
        observer = observe_runtime("s", "desktop", runtime_hub(scheduler), scheduler)
        runtime = PluginRuntime("demo", "s", task_id="source-task",
            _supervisor=supervisor, _runtime_queue=queue)
        try:
            await scheduler.start()
            initial = await anext(observer)
            payload = {"id": "control", "active": True, "text": "连接中",
                "plugin_name": "forged", "session_id": "other", "run_id": "forged", "task_id": "forged"}
            assert await runtime.publish_event("status_notice", payload) is None
            payload["text"] = "发送后修改"
            await runtime.publish_event("status_notice", {"id": "control", "active": True, "text": "执行中"})
            await runtime.publish_event("status_notice", {"id": "control", "active": False})
            await runtime.publish_event("notice", {"text": "普通短暂提示"})
            received = [await asyncio.wait_for(anext(observer), 2) for _ in range(4)]
            assert [(event.kind, event.payload) for event in received] == [
                ("status_notice", {"plugin_name": "demo", "id": "control", "active": True, "text": "连接中"}),
                ("status_notice", {"plugin_name": "demo", "id": "control", "active": True, "text": "执行中"}),
                ("status_notice", {"plugin_name": "demo", "id": "control", "active": False}),
                ("notice", {"plugin_name": "demo", "text": "普通短暂提示"}),
            ]
            for event in received[:3]:
                assert event.session_id == "s" and event.run_id is None and event.task_id == "source-task"
                assert event.event_sequence > initial.event_sequence
            state = await manager.load()
            assert state.messages == () and state.temporary_context == {}
            assert await manager.committed_history_position() is None
            assert await manager.read_activation_batch() == ()
            assert agent_calls == []
            snapshot = await scheduler.snapshot("s")
            assert snapshot.payload == initial.payload
        finally:
            await observer.aclose()
            await manager.close()
            await close_test_runtime(scheduler)

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    {}, {"id": 1, "active": False}, {"id": "", "active": False}, {"id": "  ", "active": False},
    {"id": "control"}, {"id": "control", "active": 1}, {"id": "control", "active": "false"},
    {"id": "control", "active": True}, {"id": "control", "active": True, "text": 1},
    {"id": "control", "active": True, "text": ""}, {"id": "control", "active": True, "text": " \n"},
])
def test_status_notice_rejects_invalid_payload_without_emitting(tmp_path: Path, payload: dict[str, object]) -> None:
    """通过父会话公开受理入口拒绝不完整或类型错误的展示请求。

    Args:
        tmp_path (Path): 测试数据库目录。
        payload (dict[str, object]): 不符合持续提示契约的载荷。
    """

    async def scenario() -> None:
        """装配真实会话，读取业务回执并确认没有可投递事件。"""
        queue: asyncio.Queue[RuntimeRequest] = asyncio.Queue()
        manager = SessionStateManager("s", SqliteFactory(tmp_path / "state.sqlite3"))

        def create_agent(current: SessionStateManager) -> Agent:
            """在纯展示测试中报告意外执行。

            Args:
                current (SessionStateManager): 会话借给执行对象的状态。

            Raises:
                AssertionError: 不合法展示请求意外启动 Agent。
            """
            raise AssertionError("不合法展示请求不能启动 Agent")

        execution = AgentExecution("s", manager=manager, agent_factory=create_agent, events=queue)
        session = ParentSession("s", manager=manager, execution=execution, events=queue)
        try:
            result = await session.accept_plugin(PluginRequest(PluginRequestKind.PUBLISH_EVENT, "demo", "s",
                name="status_notice", payload=payload))
            assert result.status == CommandStatus.INVALID
            assert queue.empty()
        finally:
            await manager.close()

    asyncio.run(scenario())
