from dataclasses import asdict
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.dependencies import (
    get_chat_settings_dao, get_runtime_scheduler, get_task_supervisor,
    get_interaction_event_hub, get_channel_subscription_service,
)
from app.errors import InvalidOperationError, ResourceNotFoundError
from app.interaction.interaction_event_hub import InteractionEventHub
from app.services.channel_subscription_service import ChannelSubscriptionService
from app.schemas.result import Result
from app.schemas.interaction import (
    ChannelCommandRequest, ChannelSubscriptionRequest, ChannelListResponse, CommandReceiptResponse,
)
from app.schemas.subagent_responses import SubagentDetailResponse, SubagentListResponse
from app.schemas.system import HealthResponse
from app.schemas.task_responses import TaskListItem, TaskListResponse, TaskOutputResponse
from app.crud.chat_settings_dao import ChatSettingsDao
from app.agent.runtime.agent_runtime_scheduler import AgentRuntimeScheduler
from app.agent.runtime.tasks.task_supervisor import TaskSupervisor
from app.agent.tools.terminal.task import (
    TASK_OUTPUT_PAGE_DEFAULT_BYTES, TASK_OUTPUT_PAGE_MAX_BYTES, read_terminal_tail, read_terminal_output,
)
from app.interaction.interaction_contract import ChannelCommand
from app.schemas.runtime_types import DESKTOP_CHANNEL
from app.utils.sse_stream import SSE_HEARTBEAT_SECONDS, stream_sse_json


router = APIRouter(tags=["agent"])


@router.post("/agent/commands")
async def submit_command(
    command: ChannelCommandRequest,
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> CommandReceiptResponse:
    """把 HTTP 命令交给 Scheduler 并直接返回受理回执。

    Args:
        command (ChannelCommandRequest): 统一渠道命令或需要执行的命令正文，类型决定其含义。
        scheduler (AgentRuntimeScheduler): 进程级会话调度器。

    Returns:
        CommandReceiptResponse: 包含受理标记、实际目标和失败说明的命令回执。
    """
    receipt = await scheduler.submit(ChannelCommand(**command.model_dump()))
    return CommandReceiptResponse(**asdict(receipt), accepted=receipt.accepted)


async def observe_runtime(session_id: str, channel: str, hub: InteractionEventHub,
                          scheduler: AgentRuntimeScheduler) -> AsyncIterator:
    """先登记连接再查询运行快照，消费水位之后的事件并在所有退出路径注销。

    Args:
        session_id (str): 本次观察的父会话。
        channel (str): 已注册接收渠道。
        hub (InteractionEventHub): 应用输出缓冲。
        scheduler (AgentRuntimeScheduler): 业务快照查询入口。

    Yields:
        InteractionEvent: 当前快照和与之对应的后续增量。
    """
    subscriber = await hub.register(session_id, channel)
    if subscriber is None:
        return
    try:
        snapshot = await scheduler.snapshot(session_id)
        if not hub.contains(subscriber):
            return
        yield hub.visible_snapshot(snapshot, channel)
        sequence = snapshot.event_sequence
        while True:
            event = await subscriber.queue.get()
            if event is None:
                return
            if event.event_sequence > sequence:
                sequence = event.event_sequence
                yield event
    finally:
        hub.unsubscribe(subscriber)


@router.get("/agent/events/{session_id}", responses={400: {"description": "渠道未注册或会话未订阅此渠道"}})
async def subscribe_events(
    session_id: str,
    channel: str = DESKTOP_CHANNEL,
    hub: InteractionEventHub = Depends(get_interaction_event_hub),
    subscriptions: ChannelSubscriptionService = Depends(get_channel_subscription_service),
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> StreamingResponse:
    """校验渠道订阅后建立会话观察流，连接结束不取消 Agent。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        channel (str): 已注册渠道标识。
        hub (InteractionEventHub): 保存序号、订阅和输出缓冲的共享分发对象。
        subscriptions (ChannelSubscriptionService): 共享渠道配置。
        scheduler (AgentRuntimeScheduler): 应用注入的运行快照查询入口。

    Returns:
        StreamingResponse: 独立的会话 SSE，先发送接收方快照再发送增量。

    Raises:
        InvalidOperationError: 渠道未注册或在该会话未启用。
    """
    subscriptions.validate_source(channel)
    if not await subscriptions.is_enabled(session_id, channel):
        raise InvalidOperationError("该会话未订阅此渠道")

    # 1. 会话长期订阅共用分钟心跳和 JSON 投影，心跳不占业务事件序号。
    return StreamingResponse(stream_sse_json(
        observe_runtime(session_id, channel, hub, scheduler),
        heartbeat_interval=SSE_HEARTBEAT_SECONDS,
    ), media_type="text/event-stream")


@router.get("/agent/channels/{session_id}")
async def list_channels(
    session_id: str,
    subscriptions: ChannelSubscriptionService = Depends(get_channel_subscription_service),
) -> ChannelListResponse:
    """返回当前会话已注册渠道的配置与可用状态。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        subscriptions (ChannelSubscriptionService): 共享渠道描述和启用配置。

    Returns:
        ChannelListResponse: 以 channels 键包装的会话渠道列表。
    """
    return ChannelListResponse.model_validate({"channels": await subscriptions.list(session_id)})


@router.put("/agent/channels/{session_id}/{channel}", responses={400: {"description": "渠道未注册或试图修改桌面固定订阅"}})
async def update_channel_subscription(
    session_id: str,
    channel: str,
    payload: ChannelSubscriptionRequest,
    subscriptions: ChannelSubscriptionService = Depends(get_channel_subscription_service),
) -> ChannelListResponse:
    """持久更新可选渠道订阅并返回更新后的列表。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        channel (str): 已注册渠道标识。
        payload (ChannelSubscriptionRequest): HTTP 请求内容。
        subscriptions (ChannelSubscriptionService): 命令与分发共用的配置。

    Returns:
        ChannelListResponse: 保存后以 channels 键包装的渠道列表。

    Raises:
        InvalidOperationError: 修改桌面固定订阅或渠道未注册。
    """
    await subscriptions.set_enabled(session_id, channel, payload.enabled)
    return ChannelListResponse.model_validate({"channels": await subscriptions.list(session_id)})


@router.get("/agent/subagents/{session_id}")
async def list_subagents(
    session_id: str, scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> SubagentListResponse:
    """读取当前父会话下的子 Agent 名单。

    Args:
        session_id (str): 子 Agent 所属的父会话编号。
        scheduler (AgentRuntimeScheduler): 提供子身份及当前执行状态的调度器。

    Returns:
        SubagentListResponse: 子身份信息及当前公开状态。
    """
    return SubagentListResponse(subagents=list(await scheduler.snapshot_subagents(session_id)))


@router.get("/agent/subagents/{session_id}/{subagent_id}", responses={404: {"description": "子 Agent 不存在或不属于当前会话"}})
async def subagent_details(
    session_id: str, subagent_id: str, interaction_limit: Annotated[int, Query(ge=1)] = 10,
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> SubagentDetailResponse:
    """读取子身份定义、最近交互及已保存的执行结果。

    Args:
        session_id (str): 子 Agent 所属的父会话编号。
        subagent_id (str): 需要查看的子身份编号。
        interaction_limit (int): 最多返回的显式交互数量，至少为 1。
        scheduler (AgentRuntimeScheduler): 提供子身份详情的调度器。

    Returns:
        SubagentDetailResponse: 子身份详情和上下文可用状态。

    Raises:
        ResourceNotFoundError: 子身份不存在或属于其他会话。
    """
    detail = await scheduler.get_subagent_details(session_id, subagent_id, interaction_limit)
    return SubagentDetailResponse.model_validate(detail)


@router.get("/tasks/{session_id}/records")
async def list_background_tasks(
    session_id: str,
    supervisor: TaskSupervisor = Depends(get_task_supervisor),
) -> TaskListResponse:
    """返回会话任务记录和最近输出片段。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        supervisor (TaskSupervisor): 应用共享的通用任务管理器。

    Returns:
        TaskListResponse: 完整任务快照及每项有限的最近输出。
    """
    records = await supervisor.list_tasks(session_id)
    return TaskListResponse(tasks=[
        TaskListItem(**asdict(item), recent_output=await read_terminal_tail(item) if item.output_path else "")
        for item in records
    ])


@router.get("/tasks/{session_id}/{task_id}/output", responses={400: {"description": "任务没有终端输出"}, 404: {"description": "任务不存在或不属于当前会话"}})
async def background_task_output(
    session_id: str, task_id: str, offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=TASK_OUTPUT_PAGE_MAX_BYTES)] = TASK_OUTPUT_PAGE_DEFAULT_BYTES,
    supervisor: TaskSupervisor = Depends(get_task_supervisor),
) -> TaskOutputResponse:
    """校验任务归属后按字节游标返回一页完整输出。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        task_id (str): 受管进程任务标识。
        offset (int): 完整输出文件的起始字节游标，至少为 0。
        limit (int): 读取的最大字节预算，范围为 1 到 64 KiB。
        supervisor (TaskSupervisor): 应用共享的通用任务管理器。

    Returns:
        TaskOutputResponse: 当前页文本、下次读取位置及剩余内容标记。

    Raises:
        ResourceNotFoundError: 任务不存在或不属于当前会话。
        InvalidOperationError: 任务没有终端输出。
    """
    record = await supervisor.get_task_snapshot(task_id)
    if record is None or record.session_id != session_id:
        raise ResourceNotFoundError("任务不存在")
    page = await read_terminal_output(record, offset, limit)
    return TaskOutputResponse.model_validate(page)


@router.get("/health", responses={404: {"description": "会话不存在"}})
def health_check(
    session_id: str,
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
) -> Result[HealthResponse]:
    """读取会话模型配置并报告本地后端健康信息。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        chat_settings_dao (ChatSettingsDao): 当前环境中的会话设置 DAO。

    Returns:
        Result[HealthResponse]: 以 Result 包装的健康状态和当前模型名称。
    """
    settings = chat_settings_dao.get_chat_settings(session_id)
    return Result[HealthResponse](data=HealthResponse(status="ok", model=settings.model_name), msg="success", code=200)
