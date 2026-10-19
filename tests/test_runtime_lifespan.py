import asyncio
from functools import lru_cache
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from main import app


def test_backend_lifespan_initializes_runtime_and_serves_health() -> None:
    """验证应用生命周期初始化运行时并可提供健康信息。"""
    # 1. 安排隔离存储、替身与必要屏障，构造该回归场景的初始条件。
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200


@pytest.mark.parametrize("failure", [None, "desktop", "supervisor"])
def test_application_owns_resource_order_and_continues_cleanup(monkeypatch, failure) -> None:
    """共享资源由应用按序启动关闭，单个关闭失败也继续持久收尾和缓存清理。

    Args:
        monkeypatch (pytest.MonkeyPatch): 替换应用装配，避免真实桌面和进程。
        failure (str | None): 指定失败的资源关闭步骤。
    """
    import main
    calls = []

    def action(name):
        """创建记录执行顺序的异步操作。

        Args:
            name (str): 此操作的独立名称。

        Returns:
            Callable: 应用将等待的测试操作。
        """
        async def run():
            """记录调用，按测试配置注入关闭失败。"""
            calls.append(name)
            if name == f"{failure}.close":
                raise RuntimeError("关闭失败")
        return run

    def sync_action(name):
        """创建桌面模块使用的同步启动或封入口操作。

        Args:
            name (str): 操作名称。

        Returns:
            Callable: 不创建后台任务的同步操作。
        """
        def run():
            """记录同步操作的顺序。"""
            calls.append(name)
        return run

    def factory(value):
        """模拟真实装配函数的进程缓存。

        Args:
            value (object): 此装配函数返回的资源。

        Returns:
            Callable: 支持 cache_info 和 cache_clear 的工厂。
        """
        @lru_cache(maxsize=1)
        def get():
            """取得测试资源。

            Returns:
                object: 当前测试资源。
            """
            return value
        return get

    scheduler = SimpleNamespace(start=action("scheduler.start"), begin_shutdown=action("scheduler.stop"),
        wait_for_workers=action("workers.wait"), close=action("scheduler.finalize"))
    supervisor = SimpleNamespace(recover_orphaned=action("supervisor.recover"), close=action("supervisor.close"))
    desktop = SimpleNamespace(start=sync_action("desktop.start"), begin_shutdown=sync_action("desktop.stop"),
        close=action("desktop.close"))
    hub = SimpleNamespace(start=action("hub.start"), close=action("hub.close"))
    mcp = SimpleNamespace(preconnect_enabled=action("mcp.preconnect"), close=action("mcp.close"))
    factories = {}
    for name, value in {
        "get_runtime_scheduler": scheduler, "get_task_supervisor": supervisor,
        "get_desktop_controller": desktop, "get_interaction_event_hub": hub,
        "get_mcp_connection_manager": mcp,
        "get_channel_subscription_service": object(), "get_desktop_settings_service": object(),
        "get_desktop_settings_dao": object(),
    }.items():
        factories[name] = factory(value)
        monkeypatch.setattr(main, name, factories[name])
    monkeypatch.setattr(main, "detect_shell", sync_action("shell.detect"))

    async def scenario():
        """执行真实生命周期，外部资源全部来自可观察替身。"""
        async with main.lifespan(None):
            calls.append("requests.open")

    if failure is None:
        asyncio.run(scenario())
    else:
        with pytest.raises(RuntimeError, match="关闭失败"):
            asyncio.run(scenario())
    assert [name for name in calls if name != "mcp.preconnect"] == [
        "shell.detect", "scheduler.start", "supervisor.recover", "desktop.start", "hub.start", "requests.open",
        "scheduler.stop", "desktop.stop", "hub.close", "workers.wait", "desktop.close", "supervisor.close",
        "scheduler.finalize", "mcp.close",
    ]
    assert all(get.cache_info().currsize == 0 for name, get in factories.items() if name != "get_mcp_connection_manager")
