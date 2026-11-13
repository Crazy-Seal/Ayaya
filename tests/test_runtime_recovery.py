"""验证启动期间孤儿任务只产生事实通知，不激活旧会话。"""
from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from tests.runtime_support import next_runtime_event
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory

from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator

import asyncio

from app.agent.state.manager import SessionStateManager
from tests.runtime_support import configured_settings
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor


def test_restart_preserves_orphan_notice_without_activating_agent(tmp_path) -> None:
    """验证孤儿任务恢复记录不会构造执行对象，事实通知仍可交付。

    Args:
        tmp_path (Path): 隔离任务存储目录。
    """
    async def scenario() -> None:
        """保存遗留进程记录并通过正式启动检查通知内容。"""
        path = str(tmp_path / "checkpoint.sqlite3")
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=asyncio.Queue(), records=TaskRecordDao(SqliteFactory(path)))
        await supervisor.records.insert_running("old-task", "s", "旧进程", 1, str(tmp_path / "out"))

        def unexpected_agent(_session, _manager):
            """启动期间任何执行对象构造都应失败。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                _session (str): 请求激活的会话。
            """
            raise AssertionError("旧任务通知不能自动激活 Agent")

        scheduler = build_test_scheduler(runtime_queue=supervisor.runtime_queue,
           agent_factory=unexpected_agent,
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(path)),
            task_supervisor=supervisor,
         chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        try:
            await scheduler.start()
            await supervisor.recover_orphaned()
            event = await asyncio.wait_for(next_runtime_event(scheduler), 1)
            assert event.kind == "task_status" and event.payload["status"] == "orphaned"
            assert (await supervisor.get_task_snapshot("old-task")).status == "orphaned"
            assert ((await scheduler.snapshot("s")).payload)["phase"] == "idle"
            inbox = SessionStateManager("s", SqliteFactory(path))
            try:
                notices = await inbox.read_task_terminals()
                assert len(notices) == 1
                assert notices[0].payload["status"] == "orphaned"
            finally:
                await inbox.close()
        finally:
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
    asyncio.run(scenario())
