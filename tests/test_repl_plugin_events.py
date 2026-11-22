"""从正式 REPL 能力到真实会话订阅验证插件事件及收尾。"""

import asyncio

import pytest

from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.crud.sqlite_factory import SqliteFactory
from app.crud.task_record_dao import TaskRecordDao
from app.repl.repl_service import ReplService
from app.routes.agent import observe_runtime
from app.services.repl_plugin_events import create_repl_plugin_event_handler
from tests.runtime_support import build_test_scheduler, close_test_runtime, configured_settings, runtime_hub
from tests.test_repl_service import capability_context


@pytest.mark.parametrize("ending", ["complete", "error", "interrupt", "crash", "timeout"])
def test_repl_capability_notices_keep_source_and_close_on_every_exit(tmp_path, ending) -> None:
    """能力开启和更新提示，各类执行结束都先关闭提示再交还调用者。

    Args:
        tmp_path (Path): 本次数据库和进程工作目录。
        ending (str): 正常、异常、中断、崩溃或超时结束。
    """
    async def scenario() -> None:
        """从真实事件订阅读取三条提示，确认提示没有成为模型输入。"""
        sqlite = SqliteFactory(tmp_path / "state.sqlite3")
        queue: asyncio.Queue[RuntimeRequest] = asyncio.Queue()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(sqlite))
        calls = []

        def unexpected_agent(session, manager):
            """记录纯展示事件意外触发的 Agent 创建。

            Args:
                session (str): 被错误唤醒的会话。
                manager (SessionStateManager): 被借用的持久状态。
            """
            calls.append(session)
            raise AssertionError("展示事件不应启动 Agent")

        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            chat_settings_dao=configured_settings("capability"), agent_factory=unexpected_agent,
            session_state_manager_factory=lambda session: SessionStateManager(session, sqlite))
        observer = observe_runtime("capability", "desktop", runtime_hub(scheduler), scheduler)
        service = ReplService(workdir=tmp_path, execution_timeout=1,
                              on_event=create_repl_plugin_event_handler(supervisor, queue))
        manager = SessionStateManager("capability", sqlite)
        suffix = {
            "complete": "",
            "error": "raise ValueError('错误')",
            "interrupt": "sample.interrupt_later(0.05)\nimport time\ntime.sleep(60)",
            "crash": "import os, time\nfrom pathlib import Path\nwhile not Path('crash-now').exists(): time.sleep(0.01)\nos._exit(9)",
            "timeout": "while True: pass",
        }[ending]
        try:
            await scheduler.start()
            await anext(observer)
            running = asyncio.create_task(service.execute("sample.notice()\nsample.notice(text='更新后')\n" + suffix, capability_context()))
            events = [await asyncio.wait_for(anext(observer), 3) for _ in range(2)]
            if ending == "crash":
                (tmp_path / "crash-now").touch()
            await running
            events.append(await asyncio.wait_for(anext(observer), 3))
            assert [event.kind for event in events] == ["status_notice"] * 3
            assert [event.payload["active"] for event in events] == [True, True, False]
            assert [event.payload.get("text") for event in events] == ["测试控制中", "更新后", None]
            assert len({event.payload["id"] for event in events}) == 1
            assert all(event.payload["plugin_name"] == "test_plugin" for event in events)
            assert all(event.session_id == "capability" and event.run_id is None and event.task_id is None for event in events)
            state = await manager.load()
            assert state.messages == () and state.temporary_context == {}
            assert calls == []
        finally:
            await service.close()
            await observer.aclose()
            await manager.close()
            await close_test_runtime(scheduler)
    asyncio.run(scenario())
