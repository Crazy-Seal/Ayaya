"""把应用提供的依赖装配为独立父子会话，保持运行对象构造显式。"""

import asyncio
from collections.abc import Awaitable, Callable

from app.agent.agent import Agent
from app.agent.runtime.agent_execution import AgentExecution
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.parent_session import ParentSession
from app.agent.runtime.subagent_session import SubagentSession
from app.agent.runtime.requests import RuntimeRequest
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.state.manager import SessionStateManager
from app.interaction.interaction_event_hub import InteractionEventHub
from app.schemas.subagent_types import SubagentDefinition
from app.services.agent_identity_service import AgentIdentityService
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.services.settings_mutation import SettingsMutationCoordinator
from app.services.subagent_service import SubagentService


def assemble_runtime_scheduler(*, runtime_queue: asyncio.Queue[RuntimeRequest],
    agent_factory: Callable[[str, SessionStateManager], Agent],
    subagent_factory: Callable[[SubagentDefinition, SessionStateManager], Agent],
    manager_factory: Callable[[str], SessionStateManager],
    child_manager_factory: Callable[[SubagentDefinition], SessionStateManager],
    identity: AgentIdentityService, subagents: SubagentService,
    task_supervisor: TaskSupervisor, subscriptions: ChannelSubscriptionService,
    mutation_coordinator: SettingsMutationCoordinator, hub: InteractionEventHub,
    cleanup_session_connections: Callable[[str], Awaitable[None]] | None = None) -> AgentRuntimeScheduler:
    """为应用或隔离实例装配父子会话工厂及 Scheduler。

    Args:
        runtime_queue (asyncio.Queue): 本应用唯一运行队列。
        agent_factory (Callable): 按当前父配置创建 Agent。
        subagent_factory (Callable): 按子定义创建 Agent。
        manager_factory (Callable): 创建父会话或启动整理的持久状态对象。
        child_manager_factory (Callable): 创建使用子历史表的状态对象。
        identity (AgentIdentityService): 显式命令身份规则。
        subagents (SubagentService): 子持久业务。
        task_supervisor (TaskSupervisor): 普通任务管理器。
        subscriptions (ChannelSubscriptionService): 渠道设置。
        mutation_coordinator (SettingsMutationCoordinator): 应用短操作协调。
        hub (InteractionEventHub): 应用拥有的发布和缓冲对象。
        cleanup_session_connections (Callable | None): 父删除后释放其共享连接。

    Returns:
        AgentRuntimeScheduler: 依赖全部明确、尚未启动的调度对象。
    """
    def create_parent(context_id: str) -> ParentSession:
        """为此父上下文创建独立 Manager、执行状态和会话政策。

        Args:
            context_id (str): 父实际上下文编号。
        """
        manager = manager_factory(context_id)
        execution = AgentExecution(context_id, manager=manager,
            agent_factory=lambda state: agent_factory(context_id, state), events=runtime_queue)
        return ParentSession(context_id, manager=manager, execution=execution, events=runtime_queue)

    def create_child(definition: SubagentDefinition) -> SubagentSession:
        """为实际子编号创建独立运行对象，资源和状态不会与父上下文合并。

        Args:
            definition (SubagentDefinition): 已保存的子定义。
        """
        manager = child_manager_factory(definition)
        execution = AgentExecution(definition.subagent_id, manager=manager,
            agent_factory=lambda state: subagent_factory(definition, state), events=runtime_queue)
        return SubagentSession(definition, manager=manager, execution=execution, events=runtime_queue,
                               store=subagents, task_supervisor=task_supervisor)

    return AgentRuntimeScheduler(runtime_queue=runtime_queue, parent_factory=create_parent, child_factory=create_child,
        manager_factory=manager_factory, identity=identity, subagents=subagents, task_supervisor=task_supervisor,
        subscriptions=subscriptions, mutation_coordinator=mutation_coordinator, publish=hub.publish,
        watermark=hub.watermark, cleanup_session_connections=cleanup_session_connections)
