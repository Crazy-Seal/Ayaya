from tests.runtime_support import runtime_hub, close_test_runtime
from app.routes.agent import observe_runtime
from tests.runtime_support import build_test_scheduler
from app.crud.sqlite_factory import SqliteFactory
from app.crud.task_record_dao import TaskRecordDao
from app.services.settings_mutation import SettingsMutationCoordinator
import asyncio
from functools import lru_cache
from tests.runtime_support import configured_settings
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from main import app


def test_backend_lifespan_initializes_runtime_and_serves_health() -> None:
    """验证应用生命周期初始化运行时并可提供健康信息。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200


def test_shutdown_request_ends_existing_sse_before_lifespan_cleanup(tmp_path):
    """后端收到关闭请求即结束已有 SSE，不依赖客户端先断开才能进入清理。"""
    import main
    from fastapi import FastAPI, Request
    from app.agent.state.manager import SessionStateManager
    from app.interaction.interaction_event_hub import InteractionEventHub
    from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
    from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
    from app.routes.agent import subscribe_events
    from app.services.channel_subscription_service import ChannelSubscriptionService

    async def scenario():
        """从真实 SSE 响应读取快照和结束，保持运行时消费者到最终清理。"""
        queue = asyncio.Queue()
        subscriptions = ChannelSubscriptionService(sqlite_factory=SqliteFactory())
        supervisor_settings = configured_settings()
        supervisor_mutations = SettingsMutationCoordinator()
        supervisor = TaskSupervisor(runtime_queue=queue, records=TaskRecordDao(SqliteFactory(str(tmp_path / "state.sqlite3"))))
        scheduler = build_test_scheduler(runtime_queue=queue, task_supervisor=supervisor,
            subscriptions=subscriptions, agent_factory=lambda session, manager: None,
            session_state_manager_factory=lambda session: SessionStateManager(session, SqliteFactory(str(tmp_path / "state.sqlite3"))), chat_settings_dao=supervisor_settings, mutation_coordinator=supervisor_mutations)
        hub = runtime_hub(scheduler)
        application = FastAPI()
        application.state.runtime_scheduler = scheduler
        application.state.task_supervisor = supervisor
        application.state.interaction_event_hub = hub
        application.state.desktop_controller = SimpleNamespace(begin_shutdown=lambda: None)
        application.state.uvicorn_server = SimpleNamespace(should_exit=False)
        stream = None
        try:
            await scheduler.start()
            pass
            response = await subscribe_events("s", "desktop", hub, subscriptions, scheduler)
            stream = response.body_iterator
            assert "session_snapshot" in await anext(stream)
            assert await main.internal_shutdown(Request({"type": "http", "app": application})) == {"shutting_down": True}
            assert application.state.uvicorn_server.should_exit
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), 0.2)
            # 1. 网络订阅已结束；共享队列仍由原消费者负责处理到应用最终释放。
            await queue.put(None)
            await asyncio.wait_for(queue.join(), 0.2)
        finally:
            if stream is not None:
                await stream.aclose()
            await close_test_runtime(scheduler)
            await supervisor.close()
            await supervisor.records.close()
            await hub.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [None, "desktop", "supervisor", "startup"])
def test_application_owns_resource_order_and_continues_cleanup(tmp_path, monkeypatch, failure) -> None:
    """真实消费者在应用拥有的队列上运行，关闭失败仍继续清理其他资源。"""
    import main
    from fastapi import FastAPI
    from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
    from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
    from app.agent.state.manager import SessionStateManager
    calls = []

    class Desktop:
        """替代外部桌面资源，仅记录应用生命周期动作。"""
        def start(self):
            """记录桌面启动。"""
            calls.append("desktop.start")

        def begin_shutdown(self):
            """记录桌面停止受理。"""
            calls.append("desktop.stop")

        async def close(self):
            """报告指定的资源关闭故障。"""
            calls.append("desktop.close")
            if failure == "desktop":
                raise RuntimeError("关闭失败")

    class MCP:
        """替代外部 MCP 连接。"""
        async def preconnect_enabled(self):
            """测试不连接外部服务器。"""

        async def close(self):
            """记录共享连接最终释放。"""
            calls.append("mcp.close")

    class Supervisor(TaskSupervisor):
        """保留真实任务实现，只观察资源关闭。"""
        async def close(self):
            """关闭真实存储后按用例报告错误。"""
            calls.append("supervisor.close")
            await super().close()
            if failure == "supervisor":
                raise RuntimeError("关闭失败")

    class Scheduler(AgentRuntimeScheduler):
        """运行真实队列消费者和持久化初始化。"""
        async def start(self):
            """启动前按指定场景注入故障。"""
            if failure == "startup":
                raise RuntimeError("启动失败")
            await super().start()

        async def begin_shutdown(self):
            """记录关闭入口和已有 worker 的顺序。"""
            calls.append("scheduler.stop")
            await super().begin_shutdown()

        async def close(self):
            """回收真实消费者和临时输出。"""
            calls.append("scheduler.close")
            await super().close()

    @lru_cache(maxsize=1)
    def desktop():
        """提供本次测试唯一桌面资源。"""
        return Desktop()

    from app.agent.runtime import runtime_factory
    monkeypatch.setattr(runtime_factory, "AgentRuntimeScheduler", Scheduler)
    original_factory = main.create_runtime_scheduler

    def create_scheduler(supervisor, queue, **dependencies):
        """使用实际应用装配函数，将生命周期观察限定在 Scheduler 子类。"""
        return original_factory(supervisor, queue, **dependencies)

    monkeypatch.setattr(main, "SqliteFactory", lambda: SqliteFactory(tmp_path / "state.sqlite3"))
    monkeypatch.setattr(main, "TaskSupervisor", Supervisor)
    monkeypatch.setattr(main, "create_runtime_scheduler", create_scheduler)
    monkeypatch.setattr(main, "get_desktop_controller", desktop)
    monkeypatch.setattr(main, "get_mcp_connection_manager", lambda: MCP())
    monkeypatch.setattr(main, "detect_shell", lambda: None)

    async def scenario():
        """应用状态保存同一实例，退出清理不重新调用工厂创建资源。"""
        application = FastAPI()
        async with main.lifespan(application):
            assert application.state.runtime_scheduler.task_supervisor is application.state.task_supervisor
            assert application.state.runtime_scheduler.runtime_queue is application.state.task_supervisor.runtime_queue
            calls.append("requests.open")

    if failure:
        with pytest.raises(RuntimeError, match="失败"):
            asyncio.run(scenario())
    else:
        asyncio.run(scenario())
    assert calls.index("scheduler.stop") < calls.index("desktop.close") < calls.index("supervisor.close")
    assert calls.index("supervisor.close") < calls.index("scheduler.close") < calls.index("mcp.close")
    assert calls.count("supervisor.close") == 1
    assert desktop.cache_info().currsize == 0


def test_application_lifespan_reaps_repl_process_tree(tmp_path, monkeypatch) -> None:
    """真实应用退出清理会回收已启动的 REPL 及其派生进程。

    Args:
        tmp_path (Path): 代码初始目录和进程报告位置。
        monkeypatch (pytest.MonkeyPatch): 将应用工作区指向临时目录。
    """
    import main
    from fastapi import FastAPI
    from app.repl.contracts import ReplCallContext, ReplText
    from tests.test_repl_service import process_is_running

    monkeypatch.setattr(main, "WORKDIR", tmp_path)

    async def scenario() -> None:
        """借用应用资源创建真实进程，退出 lifespan 后检查操作系统。"""
        application = FastAPI()
        pids = []
        async with main.lifespan(application):
            result = await application.state.repl_service.execute(
                "import subprocess, sys, os\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=subprocess.CREATE_NO_WINDOW)\nprint(os.getpid(), child.pid)",
                ReplCallContext("lifespan", "run", "call", ()))
            text = "".join(item.text for item in result.outputs if isinstance(item, ReplText))
            pids = [int(value) for value in text.split()]
            assert len(pids) == 2 and all(process_is_running(pid) for pid in pids)
        assert not any(process_is_running(pid) for pid in pids)
    asyncio.run(scenario())
