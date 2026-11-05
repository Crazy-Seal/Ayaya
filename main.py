import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager

from dotenv import load_dotenv

from app.environment import get_images_dir, is_test_environment

# 测试环境不得从生产 .env 文件继承凭据。
if not is_test_environment():
    load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from app.routes.agent import router as agent_router
from app.routes.chat_settings import router as chat_settings_router
from app.routes.chat_history import router as memory_router
from app.routes.tools import router as tools_router
from app.routes.plugins import router as plugins_router
from app.routes.mcp import router as mcp_router
from app.routes.desktop import router as desktop_router
from app.dependencies import get_mcp_connection_manager, create_runtime_scheduler, get_chat_settings_dao, get_settings_mutation_coordinator
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.services.desktop_settings_service import DesktopSettingsService
from app.schemas.runtime_types import RuntimePhase
from app.interaction.interaction_event_hub import InteractionEventHub
from app.dependencies import get_desktop_controller, get_desktop_settings_service, get_desktop_settings_dao
from app.dependencies import get_channel_subscription_service
from app.security.local_api import install_local_api_security, load_api_token
from app.agent.tools.terminal.shell import detect_shell

# 控制台日志基础配置：让 Agent 的收发日志在本地启动时可见
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

async def begin_runtime_shutdown(application: FastAPI) -> None:
    """封住应用的执行和桌面入口，重复调用沿用已创建的对象。

    Args:
        application (FastAPI): 本次启动的 FastAPI 应用及其共享对象。
    """
    await application.state.runtime_scheduler.begin_shutdown()
    # 1. 先结束网络订阅，服务器才能进入 lifespan 清理；内部消费者仍继续读取。
    application.state.interaction_event_hub.begin_shutdown()
    await application.state.task_supervisor.begin_shutdown()
    application.state.desktop_controller.begin_shutdown()


def clear_runtime_dependencies() -> None:
    """共享对象关闭后释放其余配置缓存，运行时实例由 app.state 保存。"""
    get_channel_subscription_service.cache_clear()
    get_desktop_controller.cache_clear()
    get_desktop_settings_dao.cache_clear()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """装配唯一队列及运行时对象，生产者收尾完成后再结束消费。

    Args:
        application (FastAPI): 本次启动的 FastAPI 应用及其共享对象。
    """
    from app.dependencies import get_plugin_service
    from app.environment import PROJECT_ROOT
    # 1. 在接收请求前完成插件发现；发现失败保留配置并通过目录接口公开错误。
    await get_plugin_service().discover(PROJECT_ROOT / "plugins")
    runtime_queue = asyncio.Queue()
    mcp_manager = get_mcp_connection_manager()
    consumer_started = False
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(mcp_manager.close)
        cleanup.callback(clear_runtime_dependencies)
        supervisor = TaskSupervisor(runtime_queue=runtime_queue, chat_settings_dao=get_chat_settings_dao(),
                                    mutation_coordinator=get_settings_mutation_coordinator())
        application.state.task_supervisor = supervisor
        scheduler = create_runtime_scheduler(supervisor, runtime_queue)
        application.state.runtime_scheduler = scheduler
        application.state.desktop_settings_service = DesktopSettingsService(get_desktop_settings_dao(),
            lambda: scheduler.phase not in {RuntimePhase.STOPPING, RuntimePhase.STOPPED})
        desktop = get_desktop_controller()
        application.state.desktop_controller = desktop
        hub = InteractionEventHub(scheduler, subscriptions=get_channel_subscription_service())
        application.state.interaction_event_hub = hub

        async def finish_runtime_requests() -> None:
            """所有生产者结束后，由应用发出唯一结束标记并等待积压请求处理。"""
            if consumer_started:
                await runtime_queue.put(None)
                await runtime_queue.join()

        # 1. 逆序执行：封入口、等 worker、停任务、读完请求、关 Scheduler 和输出。
        cleanup.push_async_callback(hub.close)
        cleanup.push_async_callback(scheduler.close)
        cleanup.push_async_callback(finish_runtime_requests)
        cleanup.push_async_callback(supervisor.close)
        cleanup.push_async_callback(desktop.close)
        cleanup.push_async_callback(scheduler.wait_for_workers)
        cleanup.push_async_callback(begin_runtime_shutdown, application)
        try:
            await asyncio.to_thread(detect_shell)
        except RuntimeError:
            logging.getLogger(__name__).warning("默认 Shell 不可用，终端工具将返回环境错误")
        await scheduler.start()
        consumer_started = True
        await supervisor.recover_orphaned()
        desktop.start()
        await hub.start()
        preconnect_task = asyncio.create_task(mcp_manager.preconnect_enabled())
        try:
            yield
        finally:
            # 2. 预连接不再生产新连接，随后执行已登记的逆序资源清理。
            preconnect_task.cancel()
            await asyncio.gather(preconnect_task, return_exceptions=True)


# FastAPI 应用入口：仅负责启动和挂载路由
app = FastAPI(title="Ayaya server", version="0.1.0", lifespan=lifespan)
install_local_api_security(app, load_api_token())
# 挂载 Agent 相关 API
app.include_router(agent_router)
app.include_router(chat_settings_router)
app.include_router(memory_router)
app.include_router(tools_router)
app.include_router(plugins_router)
app.include_router(mcp_router)
app.include_router(desktop_router)

# 静态文件服务：用于访问保存的图片
IMAGES_DIR = get_images_dir()
os.makedirs(IMAGES_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")


@app.get("/")
async def root():
    # 根路径用于快速确认服务是否启动
    """提供根路径简单响应，便于确认后端 HTTP 服务可访问。

    Returns:
        dict[str, str]: 根路径响应中声明的服务信息。
    """
    return {"message": "Ayaya server is running"}


@app.get("/internal/ready")
async def internal_ready():
    """返回仅供 Electron 主进程使用的后端就绪状态。"""
    return {"ready": True}


@app.get("/internal/tasks/active-summary")
async def active_task_summary(request: Request):
    """退出确认前查询全应用活动任务，包含静默任务和清理中的任务。

    Args:
        request (Request): 本次 HTTP 请求或绑定来源的插件请求。
    """
    return {"active_count": request.app.state.task_supervisor.active_count()}


@app.post("/internal/shutdown")
async def internal_shutdown(request: Request):
    """请求当前 Uvicorn 实例执行优雅关闭。

    Args:
        request (Request): FastAPI 当前请求，提供应用运行时和受控服务器实例。
    """
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        raise HTTPException(status_code=503, detail="后端不由受控启动器管理")
    # 1. 长连接与任务回调先失去执行资格，服务器等待请求退出期间也不能启动新轮次。
    await begin_runtime_shutdown(request.app)
    server.should_exit = True
    return {"shutting_down": True}


if __name__ == "__main__":
    from app.server import run

    run()
