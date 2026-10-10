from fastapi import Depends
from functools import lru_cache

from app.crud.chat_history_dao import ChatHistoryDao
from app.crud.chat_settings_dao import ChatSettingsDao
from app.crud.mcp_settings_dao import MCPSettingsDao
from app.services.agent_runtime_scheduler import AgentRuntimeScheduler
from app.services.interaction_event_hub import InteractionEventHub
from app.services.interaction_contract import InteractionEvent
from app.schemas.runtime_types import InteractionEventKind
from app.services.task_supervisor import TaskSupervisor
from app.services.agent_factory import build_agent
from app.services.mcp_connection_manager import MCPConnectionManager
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
def get_chat_history_dao() -> ChatHistoryDao:
    return ChatHistoryDao()


@lru_cache(maxsize=1)
def get_chat_settings_dao() -> ChatSettingsDao:
    return ChatSettingsDao()


@lru_cache(maxsize=1)
def get_mcp_settings_dao() -> MCPSettingsDao:
    """获取进程级 MCP 配置数据访问对象。

    Returns:
        缓存的 MCP 配置数据访问对象。
    """
    return MCPSettingsDao()


@lru_cache(maxsize=1)
def get_settings_mutation_coordinator() -> SettingsMutationCoordinator:
    """获取跨配置写入协调器。

    Returns:
        进程级共享的写入协调器。
    """
    return SettingsMutationCoordinator()


@lru_cache(maxsize=1)
def get_mcp_connection_manager() -> MCPConnectionManager:
    """构造并缓存进程级 MCP 连接管理器。

    Returns:
        进程级 MCP 连接管理器。
    """
    dao = get_mcp_settings_dao()
    return MCPConnectionManager(dao.load)


@lru_cache(maxsize=1)
def get_tool_authorization() -> ToolAuthorization:
    """提供按调用读取最新会话配置的统一工具授权层。"""
    chat_dao = get_chat_settings_dao()
    return ToolAuthorization(
        chat_dao.get_chat_settings,
        catalog=UnifiedToolCatalog(
            LocalToolCatalogAdapter(),
            MCPToolCatalogAdapter(
                get_mcp_settings_dao(),
                get_mcp_connection_manager(),
            ),
        ),
        settings_writer=chat_dao.update_chat_settings,
        mutation_coordinator=get_settings_mutation_coordinator(),
    )


@lru_cache(maxsize=1)
def get_task_supervisor() -> TaskSupervisor:
    """取得进程级任务管理器，所有 Agent 工具共享同一进程所有者。

    Returns:
        TaskSupervisor: 当前查询或执行结果。
    """
    return TaskSupervisor()


@lru_cache(maxsize=1)
def get_runtime_scheduler() -> AgentRuntimeScheduler:
    """装配进程级 Scheduler，并连接任务状态发布和终态唤醒回调。

    Returns:
        AgentRuntimeScheduler: 当前查询或执行结果。
    """
    settings_dao = get_chat_settings_dao()
    mcp_manager = get_mcp_connection_manager()
    authorization = get_tool_authorization()
    supervisor = get_task_supervisor()
    # 1. 进程级缓存持有唯一调度器，Agent 工厂每次运行才读取会话配置。
    scheduler = AgentRuntimeScheduler(
        InteractionEventHub(),
        agent_factory=lambda session_id: build_agent(
            settings_dao.get_chat_settings(session_id),
            mcp_manager,
            authorization,
            supervisor,
        ),
        task_supervisor=supervisor,
    )
    # 2. 任务终态只通知已激活会话，初始化和关闭期间保留事实，不自行启动 Agent。
    supervisor.on_terminal = scheduler.notify_task_terminal

    async def publish_task(record):
        """将任务可见状态发布到桌面，不把持续输出广播到聊天事件流。

        Args:
            record (TaskRecord): 持久任务记录。
        """
        await scheduler.events.publish(InteractionEvent(
            record.session_id, InteractionEventKind.TASK_STATUS,
            {"task_id": record.task_id, "status": record.status,
             "exit_code": record.exit_code},
            task_id=record.task_id,
        ))

    supervisor.on_status = publish_task
    return scheduler


@lru_cache(maxsize=1)
def get_chat_settings_service(
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
    mcp_settings_dao: MCPSettingsDao = Depends(get_mcp_settings_dao),
    connection_manager: MCPConnectionManager = Depends(get_mcp_connection_manager),
    mutation_coordinator: SettingsMutationCoordinator = Depends(get_settings_mutation_coordinator),
) -> ChatSettingsService:
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
        mcp_settings_dao: MCP Server 配置数据访问对象。
        chat_settings_dao: 模型配置数据访问对象。
        connection_manager: 进程级 MCP 连接管理器。
        mutation_coordinator: 跨配置写入协调器。

    Returns:
        已完成依赖注入的 MCP 应用服务。
    """
    return MCPService(
        mcp_settings_dao,
        chat_settings_dao,
        connection_manager,
        mutation_coordinator,
    )


def get_tool_permission_service(
    authorization: ToolAuthorization = Depends(get_tool_authorization),
) -> ToolPermissionService:
    return ToolPermissionService(authorization)


@lru_cache(maxsize=1)
def get_chat_history_service(chat_history_dao: ChatHistoryDao = Depends(get_chat_history_dao)) -> ChatHistoryService:
    return ChatHistoryService(chat_history_dao=chat_history_dao)
