from app.crud.sqlite_factory import SqliteFactory
import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import AsyncIterator

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
from app.agent.runtime.runtime_factory import create_runtime_scheduler
from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.crud.task_record_dao import TaskRecordDao
from app.crud.subagent_dao import SubagentDao
from app.crud.chat_history_dao import ChatHistoryDao
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.schemas.agent_storage import AgentStorageScope
from app.agent.state.manager import SessionStateManager
from app.services.agent_identity_service import AgentIdentityService
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.services.subagent_service import SubagentService
from app.schemas.chat_settings import ChatSettings
from app.schemas.mcp import MCPConnectionConfig
from app.schemas.subagent_types import SubagentDefinition
from app.agent.tools.authorization.catalog import LocalToolCatalogAdapter, MCPToolCatalogAdapter, UnifiedToolCatalog
from app.agent.tools.authorization.manager import ToolAuthorization
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.services.chat_history_service import ChatHistoryService
from app.services.chat_settings_service import ChatSettingsService
from app.services.mcp_service import MCPService
from app.services.plugin_service import PluginService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.tool_permission_service import ToolPermissionService
from app.interaction.interaction_event_hub import InteractionEventHub
from app.security.local_api import install_local_api_security, load_api_token
from app.agent.tools.terminal.shell import detect_shell
from app.agent.utils.infra.safe_path import WORKDIR
from app.repl.repl_service import ReplService
from app.services.repl_plugin_events import create_repl_plugin_event_handler

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




@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """创建应用共享服务和运行时，生产者收尾完成后再结束消费。

    Args:
        application (FastAPI): 本次启动的 FastAPI 应用及其共享对象。

    Yields:
        None: 共享依赖和运行时就绪后，允许应用处理 HTTP 请求。
    """
    from app.environment import PROJECT_ROOT
    # 1. 设置、插件发现和运行时共用本次应用的配置存储与写入协调器。
    settings_dao = ChatSettingsDao()
    application.state.chat_settings_dao = settings_dao
    mcp_settings_dao = MCPSettingsDao()
    application.state.mcp_settings_dao = mcp_settings_dao
    mutations = SettingsMutationCoordinator()
    application.state.settings_mutation_coordinator = mutations
    plugins = PluginService(settings_dao, mutations)
    application.state.plugin_service = plugins
    await plugins.discover(PROJECT_ROOT / "plugins")
    runtime_queue: asyncio.Queue[RuntimeRequest] = asyncio.Queue()
    async with AsyncExitStack() as cleanup:
        # 2. 先登记连接清理，再装配借用连接和配置的应用服务。
        mcp_manager = MCPConnectionManager(mcp_settings_dao.load, plugin_config_loader=plugins.connection_config)
        cleanup.push_async_callback(mcp_manager.close)
        application.state.mcp_connection_manager = mcp_manager
        settings_service = ChatSettingsService(
            chat_settings_dao=settings_dao, mcp_settings_dao=mcp_settings_dao,
            connection_manager=mcp_manager, mutation_coordinator=mutations, plugin_service=plugins,
        )
        application.state.chat_settings_service = settings_service
        authorization = ToolAuthorization(settings_dao.get_chat_settings)
        application.state.mcp_service = MCPService(
            settings_dao=mcp_settings_dao, chat_settings_dao=settings_dao,
            connection_manager=mcp_manager, plugin_service=plugins, mutation_coordinator=mutations,
        )

        def plugin_servers(settings: ChatSettings, plugin_name: str) -> tuple[MCPConnectionConfig, ...]:
            """为权限目录查询指定启用插件在本次会话的 MCP 服务声明。

            Args:
                settings (ChatSettings): 权限页当前查询的会话设置。
                plugin_name (str): 工具目录项所属的插件名称。

            Returns:
                tuple[MCPConnectionConfig, ...]: 启用插件的服务声明，禁用时为空。
            """
            item = settings.agent_plugins[plugin_name]
            return plugins.prepare_plugin(plugin_name, item).mcp_servers if item.enabled else ()

        catalog = UnifiedToolCatalog(
            LocalToolCatalogAdapter(plugins.local_tools),
            MCPToolCatalogAdapter(mcp_settings_dao, mcp_manager, plugin_servers),
        )
        application.state.tool_permission_service = ToolPermissionService(settings_dao, catalog, mutations)
        sqlite_factory = SqliteFactory()
        application.state.sqlite_factory = sqlite_factory
        subscriptions = ChannelSubscriptionService(sqlite_factory)
        application.state.channel_subscriptions = subscriptions
        history_dao = ChatHistoryDao(sqlite_factory)
        application.state.chat_history_dao = history_dao
        application.state.chat_history_service = ChatHistoryService(history_dao)
        definitions = SubagentDao(sqlite_factory)
        application.state.subagent_dao = definitions
        cleanup.push_async_callback(definitions.close)
        identity = AgentIdentityService(settings_dao, definitions)
        application.state.agent_identity = identity
        task_records = TaskRecordDao(sqlite_factory)
        cleanup.push_async_callback(task_records.close)
        supervisor = TaskSupervisor(runtime_queue=runtime_queue, records=task_records)
        application.state.task_supervisor = supervisor
        def child_manager(definition: SubagentDefinition) -> SessionStateManager:
            """启动整理借用独立子历史连接，由使用该连接的业务负责关闭。

            Args:
                definition (SubagentDefinition): 已保存的子身份、所属父会话及保留政策。

            Returns:
                SessionStateManager: 由使用它的启动整理操作负责关闭的状态对象。
            """
            return SessionStateManager(definition.subagent_id, sqlite_factory,
                storage_scope=AgentStorageScope.SUBAGENT, parent_session_id=definition.session_id)

        subagents = SubagentService(definitions, task_records, identity, manager_factory=child_manager)
        application.state.subagent_service = subagents
        hub = InteractionEventHub(subscriptions=subscriptions)
        application.state.interaction_event_hub = hub
        # 2.1. 准备 Python 和终端工具共用的工作目录，再装配执行入口。
        WORKDIR.mkdir(exist_ok=True)
        repl = ReplService(workdir=WORKDIR, on_event=create_repl_plugin_event_handler(supervisor, runtime_queue))
        application.state.repl_service = repl
        scheduler = create_runtime_scheduler(supervisor, runtime_queue, sqlite_factory=sqlite_factory,
            hub=hub, subscriptions=subscriptions, identity=identity, subagents=subagents, repl_service=repl,
            settings_dao=settings_dao, settings_service=settings_service, plugin_service=plugins,
            mcp_manager=mcp_manager, authorization=authorization, mutation_coordinator=mutations)
        application.state.runtime_scheduler = scheduler

        # 3. 逆序执行：封入口、等 worker、停任务、读完请求、关 Scheduler 和输出。
        cleanup.push_async_callback(hub.close)
        cleanup.push_async_callback(scheduler.close)
        cleanup.push_async_callback(supervisor.close)
        cleanup.push_async_callback(repl.close)
        cleanup.push_async_callback(scheduler.wait_for_workers)
        cleanup.push_async_callback(begin_runtime_shutdown, application)
        try:
            await asyncio.to_thread(detect_shell)
        except RuntimeError:
            logging.getLogger(__name__).warning("默认 Shell 不可用，终端工具将返回环境错误")
        await scheduler.start()
        await supervisor.recover_orphaned()
        preconnect_task = asyncio.create_task(mcp_manager.preconnect_enabled())
        try:
            yield
        finally:
            # 4. 预连接任务结束后，执行已登记的逆序资源清理。
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
async def internal_ready() -> dict[str, bool | int]:
    """向 Electron 交付就绪状态及当前进程编号，用于断线后的退出核对。

    Returns:
        dict[str, bool | int]: 就绪标记和当前后端 PID。
    """
    return {"ready": True, "pid": os.getpid()}


@app.get("/internal/tasks/active-summary")
async def active_task_summary(request: Request):
    """退出确认前查询业务或结果保存尚未结束的任务，包含静默任务。

    Args:
        request (Request): 本次 HTTP 请求或绑定来源的插件请求。
    """
    return {"active_count": request.app.state.task_supervisor.active_task_count()}


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
