from fastapi import Depends
from functools import lru_cache
import os

from app.crud.chat_history_dao import ChatHistoryDao
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.crud.desktop_settings_dao import DesktopSettingsDao
from app.services.desktop_settings_service import DesktopSettingsService
from app.schemas.runtime_types import RuntimePhase
from app.desktop.desktop_controller import DesktopController
from app.desktop.driver import DesktopDriver
from app.runtime import get_data_dir
from app.orchestration.agent_runtime_scheduler import AgentRuntimeScheduler
from app.interaction.interaction_event_hub import InteractionEventHub
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.orchestration.task_supervisor import TaskSupervisor
from app.orchestration.agent_factory import build_agent
from app.agent.state.manager import SessionStateManager
from app.mcp.mcp_connection_manager import MCPConnectionManager
from app.services.mcp_service import MCPService
from app.services.chat_settings_service import ChatSettingsService
from app.services.chat_history_service import ChatHistoryService
from app.services.tool_permission_service import ToolPermissionService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.agent.tools.authorization.manager import ToolAuthorization
from app.agent.tools.authorization.catalog import (
    LocalToolCatalogAdapter,
    MCPToolCatalogAdapter,
    UnifiedToolCatalog,
)


@lru_cache(maxsize=1)
def get_desktop_settings_dao() -> DesktopSettingsDao:
    """提供进程内共享桌面配置 DAO，测试路径由 runtime 校验。

    Returns:
        DesktopSettingsDao: 当前进程唯一的 YAML 原子存储对象。
    """
    return DesktopSettingsDao()


@lru_cache(maxsize=1)
def get_desktop_controller() -> DesktopController:
    """装配应用唯一的桌面模块，不在工具执行时临时创建线程或锁。

    Returns:
        DesktopController: 由应用生命周期统一启动与关闭的共享实例。
    """
    def create_driver() -> DesktopDriver:
        """由控制器工作线程调用，按当前平台创建一次原生驱动。

        Returns:
            DesktopDriver: 使用当前环境数据目录及 Electron 进程身份的驱动。
        Raises:
            RuntimeError: 当前平台尚未提供桌面驱动。
        """
        if os.name != "nt":
            raise RuntimeError("屏幕操作仅支持 Windows")
        from app.desktop.windows_desktop_driver import WindowsDesktopDriver

        return WindowsDesktopDriver(
            restore_path=get_data_dir() / "desktop" / "window_restore.json",
            owner_pid=int(os.getenv("AYAYA_PARENT_PID") or 0),
        )

    return DesktopController(get_desktop_settings_dao().load, driver_factory=create_driver)


@lru_cache(maxsize=1)
def get_desktop_settings_service() -> DesktopSettingsService:
    """装配桌面设置接口，使用现有 Scheduler 的关闭状态作为门禁。

    Returns:
        DesktopSettingsService: 使用共享 DAO 和退出状态的全局配置服务。
    """
    return DesktopSettingsService(
        get_desktop_settings_dao(),
        lambda: get_runtime_scheduler().phase not in {RuntimePhase.STOPPING, RuntimePhase.STOPPED},
    )


@lru_cache(maxsize=1)
def get_chat_history_dao() -> ChatHistoryDao:
    """向路由和服务提供进程内缓存的历史访问对象。

    Returns:
        ChatHistoryDao: 共享 ChatHistoryDao。
    """
    return ChatHistoryDao()


@lru_cache(maxsize=1)
def get_chat_settings_dao() -> ChatSettingsDao:
    """向路由、工具授权和 Agent 工厂提供共享设置访问对象。

    Returns:
        ChatSettingsDao: 进程内缓存的 ChatSettingsDao。
    """
    return ChatSettingsDao()


@lru_cache(maxsize=1)
def get_mcp_settings_dao() -> MCPSettingsDao:
    """获取进程级 MCP 配置数据访问对象。

    Returns:
        MCPSettingsDao: 缓存的 MCP 配置数据访问对象。
    """
    return MCPSettingsDao()


@lru_cache(maxsize=1)
def get_settings_mutation_coordinator() -> SettingsMutationCoordinator:
    """获取跨配置写入协调器。

    Returns:
        SettingsMutationCoordinator: 进程级共享的写入协调器。
    """
    return SettingsMutationCoordinator()


@lru_cache(maxsize=1)
def get_mcp_connection_manager() -> MCPConnectionManager:
    """构造并缓存进程级 MCP 连接管理器。

    Returns:
        MCPConnectionManager: 进程级 MCP 连接管理器。
    """
    dao = get_mcp_settings_dao()
    return MCPConnectionManager(dao.load)


@lru_cache(maxsize=1)
def get_tool_authorization() -> ToolAuthorization:
    """装配执行时使用的工具授权对象，每次判断只读取当前会话配置。

    Returns:
        ToolAuthorization: 多次 Agent 执行共享的权限与审批判断对象。
    """
    return ToolAuthorization(get_chat_settings_dao().get_chat_settings)


@lru_cache(maxsize=1)
def get_task_supervisor() -> TaskSupervisor:
    """取得进程级任务管理器，所有 Agent 工具共享同一进程所有者。

    Returns:
        TaskSupervisor: 进程内缓存的共享后台进程管理器。
    """
    return TaskSupervisor()


@lru_cache(maxsize=1)
def get_channel_subscription_service() -> ChannelSubscriptionService:
    """取得命令、分发和设置页共用的配置对象。

    Returns:
        ChannelSubscriptionService: 统一的描述、持久设置和缓存入口。
    """
    return ChannelSubscriptionService()


@lru_cache(maxsize=1)
def get_runtime_scheduler() -> AgentRuntimeScheduler:
    """装配执行模块，任务状态写入其临时输出，不创建 Hub。

    Returns:
        AgentRuntimeScheduler: 共享执行模块。
    """
    settings_dao = get_chat_settings_dao()
    mcp_manager = get_mcp_connection_manager()
    authorization = get_tool_authorization()
    supervisor = get_task_supervisor()
    desktop = get_desktop_controller()
    def create_agent(session_id: str, session_state_manager: SessionStateManager):
        """真正执行时读取会话设置，创建本次 Agent。

        Args:
            session_id (str): 当前执行会话。
            session_state_manager (SessionStateManager): worker 借给本次执行的业务对象。

        Returns:
            Agent: 注入共享工具依赖的本次执行对象。
        """
        return build_agent(
            settings_dao.get_chat_settings(session_id),
            mcp_manager,
            authorization,
            supervisor,
            desktop,
            session_state_manager=session_state_manager,
        )

    scheduler = AgentRuntimeScheduler(
        agent_factory=create_agent,
        subscriptions=get_channel_subscription_service(),
        task_supervisor=supervisor,
    )
    return scheduler


@lru_cache(maxsize=1)
def get_interaction_event_hub() -> InteractionEventHub:
    """装配唯一 Hub，依赖方向仅从 Hub 指向 Scheduler。

    Returns:
        InteractionEventHub: 由应用生命周期启动的共享分发对象。
    """
    return InteractionEventHub(get_runtime_scheduler(), subscriptions=get_channel_subscription_service())


@lru_cache(maxsize=1)
def get_chat_settings_service(
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
    mcp_settings_dao: MCPSettingsDao = Depends(get_mcp_settings_dao),
    connection_manager: MCPConnectionManager = Depends(get_mcp_connection_manager),
    mutation_coordinator: SettingsMutationCoordinator = Depends(get_settings_mutation_coordinator),
) -> ChatSettingsService:
    """FastAPI 解析依赖后组装并缓存设置服务，共用 MCP 连接和配置写入协调器。

    Args:
        chat_settings_dao (ChatSettingsDao): 按会话读取和保存模型配置的数据访问对象。
        mcp_settings_dao (MCPSettingsDao): 读取和保存 MCP 服务配置的数据访问对象。
        connection_manager (MCPConnectionManager): 进程共用的 MCP 连接及工具目录管理器。
        mutation_coordinator (SettingsMutationCoordinator): 保证关联配置修改按顺序完成的协调器。

    Returns:
        ChatSettingsService: 已装配依赖的会话设置服务。
    """
    return ChatSettingsService(
        chat_settings_dao=chat_settings_dao,
        mcp_settings_dao=mcp_settings_dao,
        connection_manager=connection_manager,
        mutation_coordinator=mutation_coordinator,
    )


def get_mcp_service(
    mcp_settings_dao: MCPSettingsDao = Depends(get_mcp_settings_dao),
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
    connection_manager: MCPConnectionManager = Depends(get_mcp_connection_manager),
    mutation_coordinator: SettingsMutationCoordinator = Depends(get_settings_mutation_coordinator),
) -> MCPService:
    """组装 MCP 应用服务。

    Args:
        mcp_settings_dao (MCPSettingsDao): MCP Server 配置数据访问对象。
        chat_settings_dao (ChatSettingsDao): 模型配置数据访问对象。
        connection_manager (MCPConnectionManager): 进程级 MCP 连接管理器。
        mutation_coordinator (SettingsMutationCoordinator): 跨配置写入协调器。

    Returns:
        MCPService: 已完成依赖注入的 MCP 应用服务。
    """
    return MCPService(
        mcp_settings_dao,
        chat_settings_dao,
        connection_manager,
        mutation_coordinator,
    )


def get_tool_permission_service(
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
    mcp_settings_dao: MCPSettingsDao = Depends(get_mcp_settings_dao),
    connection_manager: MCPConnectionManager = Depends(get_mcp_connection_manager),
    mutation_coordinator: SettingsMutationCoordinator = Depends(get_settings_mutation_coordinator),
) -> ToolPermissionService:
    """为权限设置页装配配置存储、工具目录及共用写入锁。

    Args:
        chat_settings_dao (ChatSettingsDao): 当前应用共享的会话配置存储。
        mcp_settings_dao (MCPSettingsDao): 工具目录查询所需的 MCP 服务配置。
        connection_manager (MCPConnectionManager): 提供当前服务身份和工具目录的共享连接管理器。
        mutation_coordinator (SettingsMutationCoordinator): 与会话设置、MCP 服务共用的配置写入锁。

    Returns:
        ToolPermissionService: 独立处理权限配置的应用服务，不持有执行时授权对象。
    """
    return ToolPermissionService(
        chat_settings_dao,
        UnifiedToolCatalog(
            LocalToolCatalogAdapter(),
            MCPToolCatalogAdapter(mcp_settings_dao, connection_manager),
        ),
        mutation_coordinator,
    )


@lru_cache(maxsize=1)
def get_chat_history_service(chat_history_dao: ChatHistoryDao = Depends(get_chat_history_dao)) -> ChatHistoryService:
    """FastAPI 解析历史访问依赖后组装并缓存历史服务。

    Args:
        chat_history_dao (ChatHistoryDao): 把协议日志转换为界面聊天历史的数据访问对象。

    Returns:
        ChatHistoryService: 共享聊天历史查询服务。
    """
    return ChatHistoryService(chat_history_dao=chat_history_dao)
