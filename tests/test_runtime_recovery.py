"""验证启动期间孤儿任务只产生事实通知，不激活旧会话。"""

import asyncio

from app.agent.state.manager import SessionStateManager
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler
from app.orchestration.task_supervisor import TaskSupervisor


def test_restart_preserves_orphan_notice_without_activating_agent(tmp_path) -> None:
    """验证孤儿任务恢复记录不会构造执行对象，事实通知仍可交付。

    Args:
        tmp_path (Path): 隔离任务存储目录。
    """
    async def scenario() -> None:
        """保存遗留进程记录并通过正式启动检查通知内容。"""
        path = str(tmp_path / "checkpoint.sqlite3")
        supervisor = TaskSupervisor(db_path=path, spool_dir=tmp_path / "spool")
        await supervisor.records.insert_running("old-task", "s", "旧进程", 1, str(tmp_path / "out"))

        def unexpected_agent(_session, _manager):
            """启动期间任何执行对象构造都应失败。

            Args:
                _manager (SessionStateManager): worker 拥有并借给本次执行的会话状态业务对象。
                _session (str): 请求激活的会话。
            """
            raise AssertionError("旧任务通知不能自动激活 Agent")

        scheduler = AgentRuntimeScheduler(
           agent_factory=unexpected_agent,
            session_state_manager_factory=lambda session: SessionStateManager(session, path),
            task_supervisor=supervisor,
        )
        try:
            await scheduler.start()
            await supervisor.recover_orphaned()
            event = await asyncio.wait_for(scheduler.next_event(), 1)
            assert event.kind == "task_status" and event.payload["status"] == "orphaned"
            assert (await supervisor.get("old-task")).status == "orphaned"
            assert ((await scheduler.snapshot("s")).payload)["phase"] == "idle"
            inbox = SessionStateManager("s", path)
            try:
                notices = await inbox.read_task_terminals()
                assert len(notices) == 1
                assert notices[0].payload["status"] == "orphaned"
            finally:
                await inbox.close()
        finally:
            await supervisor.close()
            await scheduler.close()
    asyncio.run(scenario())
