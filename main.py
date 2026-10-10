import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

from app.runtime import get_images_dir, is_test_environment

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
from app.dependencies import get_mcp_connection_manager, get_runtime_scheduler, get_task_supervisor
from app.security.local_api import install_local_api_security, load_api_token

# 控制台日志基础配置：让 Agent 的收发日志在本地启动时可见
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """先结束旧轮次再开放请求，应用退出时封住执行并保留任务事实。

    Args:
        _app (FastAPI): 正在启动或关闭的 FastAPI 应用。

    Yields:
        None: 启动完成后交还应用控制，退出时执行持久收尾。
    """
    mcp_manager = get_mcp_connection_manager()
    preconnect_task: asyncio.Task | None = None
    try:
        # 1. Scheduler 拥有存储初始化和旧意图收尾，完成前不开放执行。
        await get_runtime_scheduler().start()
        preconnect_task = asyncio.create_task(mcp_manager.preconnect_enabled())
        yield
    finally:
        # 2. 停止预连接后，由 Scheduler 封住激活入口并保存任务终态、关闭执行资源。
        if preconnect_task is not None:
            if not preconnect_task.done():
                preconnect_task.cancel()
            await asyncio.gather(preconnect_task, return_exceptions=True)
        try:
            if get_runtime_scheduler.cache_info().currsize:
                await get_runtime_scheduler().close()
        finally:
            # 3. 生命周期结束后不能复用旧对象；收尾失败也必须释放 MCP 连接。
            get_runtime_scheduler.cache_clear()
            get_task_supervisor.cache_clear()
            await mcp_manager.close()



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
    await get_runtime_scheduler().begin_shutdown()
    server.should_exit = True
    return {"shutting_down": True}


if __name__ == "__main__":
    from app.server import run

    run()
