from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.dependencies import get_chat_settings_dao, get_runtime_scheduler, get_task_supervisor
from app.schemas.result import Result
from app.schemas.interaction import (
    ChannelCommandRequest, ChannelSubscriptionRequest,
    DesktopExecutionIdentity, DesktopExecutionResult,
)
from app.crud.chat_settings_dao import ChatSettingsDao
from app.services.agent_runtime_scheduler import AgentRuntimeScheduler
from app.services.task_supervisor import TASK_OUTPUT_PAGE_DEFAULT_BYTES, TaskSupervisor
from app.services.interaction_contract import ChannelCommand
from app.schemas.runtime_types import DESKTOP_CHANNEL
from app.utils.sse_stream import SSE_HEARTBEAT_SECONDS, stream_sse_json


router = APIRouter(tags=["agent"])


@router.post("/agent/commands")
async def submit_command(
    command: ChannelCommandRequest,
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> dict:
    """把 HTTP 命令交给 Scheduler 并直接返回受理回执。

    Args:
        command (ChannelCommandRequest): 统一渠道命令或需要执行的命令正文，类型决定其含义。
        scheduler (AgentRuntimeScheduler): 进程级会话调度器。

    Returns:
        dict: 直接序列化的 CommandReceipt，不使用 Result 包装。
    """
    receipt = await scheduler.submit(ChannelCommand(**command.model_dump()))
    return asdict(receipt)


@router.get("/agent/events/{session_id}")
async def subscribe_events(
    session_id: str,
    channel: str = DESKTOP_CHANNEL,
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> StreamingResponse:
    """校验渠道订阅后建立会话观察流，连接结束不取消 Agent。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        channel (str): 已注册渠道标识。
        scheduler (AgentRuntimeScheduler): 进程级会话调度器。

    Returns:
        StreamingResponse: 独立的会话 SSE，先发送接收方快照再发送增量。

    Raises:
        HTTPException: 渠道未注册或在该会话未启用时返回 HTTP 400。
    """
    try:
        scheduler.events.validate_source(channel)
        if not await scheduler.events.channel_enabled(session_id, channel):
            raise ValueError("该会话未订阅此渠道")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 1. 两条长期订阅共用分钟心跳和 JSON 投影，心跳不占业务事件序号。
    return StreamingResponse(stream_sse_json(
        scheduler.events.subscribe(session_id, channel),
        heartbeat_interval=SSE_HEARTBEAT_SECONDS,
    ), media_type="text/event-stream")


@router.get("/agent/channels/{session_id}")
async def list_channels(
    session_id: str,
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> dict:
    """返回当前会话已注册渠道的配置与可用状态。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        scheduler (AgentRuntimeScheduler): 进程级会话调度器。

    Returns:
        dict: 以 channels 键包装的会话渠道列表。
    """
    return {"channels": await scheduler.events.list_subscriptions(session_id)}


@router.put("/agent/channels/{session_id}/{channel}")
async def update_channel_subscription(
    session_id: str,
    channel: str,
    payload: ChannelSubscriptionRequest,
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> dict:
    """持久更新可选渠道订阅并返回更新后的列表。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        channel (str): 已注册渠道标识。
        payload (ChannelSubscriptionRequest): HTTP 请求内容。
        scheduler (AgentRuntimeScheduler): 进程级会话调度器。

    Returns:
        dict: 保存后以 channels 键包装的渠道列表。

    Raises:
        HTTPException: 修改桌面固定订阅或未注册渠道时返回 HTTP 400。
    """
    try:
        await scheduler.events.set_session_enabled(session_id, channel, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"channels": await scheduler.events.list_subscriptions(session_id)}


@router.get("/desktop-executor/requests")
async def desktop_execution_requests(
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> StreamingResponse:
    """提供独立于聊天视图的桌面主进程执行请求流。

    Args:
        scheduler (AgentRuntimeScheduler): 进程级会话调度器。

    Returns:
        StreamingResponse: 供 Electron 读取待执行桌面请求的事件流响应。
    """
    # 1. 空闲执行器同样需要响应体心跳，断线仍只释放订阅，不重放已领取动作。
    return StreamingResponse(stream_sse_json(
        scheduler.desktop_executor.subscribe(),
        heartbeat_interval=SSE_HEARTBEAT_SECONDS,
    ), media_type="text/event-stream")


@router.post("/desktop-executor/claim")
async def desktop_execution_claim(
    identity: DesktopExecutionIdentity,
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> dict:
    """执行前复核并持久领取当前屏幕请求。

    Args:
        identity (DesktopExecutionIdentity): 屏幕执行请求身份。
        scheduler (AgentRuntimeScheduler): 进程级会话调度器。

    Returns:
        dict: claimed 布尔值，不表示屏幕动作已经执行。
    """
    return {"claimed": await scheduler.desktop_executor.claim(
        identity.session_id, identity.request_id,
    )}


@router.post("/desktop-executor/result")
async def desktop_execution_result(
    payload: DesktopExecutionResult,
    scheduler: AgentRuntimeScheduler = Depends(get_runtime_scheduler),
) -> dict:
    """受理已领取屏幕请求的真实结果并触发后端恢复。

    Args:
        payload (DesktopExecutionResult): HTTP 请求内容。
        scheduler (AgentRuntimeScheduler): 进程级会话调度器。

    Returns:
        dict: accepted 布尔值，表示后端已保存首个有效结果。
    """
    return {"accepted": await scheduler.desktop_executor.submit_result(
        payload.session_id, payload.request_id, payload.result,
    )}


@router.get("/tasks/{session_id}/records")
async def list_background_tasks(
    session_id: str,
    supervisor: TaskSupervisor = Depends(get_task_supervisor),
) -> dict:
    """返回会话任务记录和最近输出片段。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        supervisor (TaskSupervisor): 进程级后台任务管理器。

    Returns:
        dict: 以 tasks 键包装的任务列表，每项附有限最近输出。
    """
    records = await supervisor.list(session_id)
    return {"tasks": [
        {**asdict(item), "recent_output": await supervisor.read_tail(item.task_id)}
        for item in records
    ]}


@router.get("/tasks/{session_id}/{task_id}/output")
async def background_task_output(
    session_id: str, task_id: str, offset: int = 0, limit: int = TASK_OUTPUT_PAGE_DEFAULT_BYTES,
    supervisor: TaskSupervisor = Depends(get_task_supervisor),
) -> dict:
    """校验任务归属后按字节游标返回一页完整输出。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        task_id (str): 受管进程任务标识。
        offset (int): 完整输出文件的起始字节游标。
        limit (int): 读取的最大字节预算。
        supervisor (TaskSupervisor): 进程级后台任务管理器。

    Returns:
        dict: 任务输出页的 text、next_offset 和 has_more。

    Raises:
        HTTPException: 任务不属于该会话时返回 HTTP 404，分页参数无效时返回 HTTP 400。
    """
    record = await supervisor.get(task_id)
    if record is None or record.session_id != session_id:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        return await supervisor.read_output(task_id, offset, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/health")
def health_check(
    session_id: str,
    chat_settings_dao: ChatSettingsDao = Depends(get_chat_settings_dao),
) -> Result:
    """读取会话模型配置并报告本地后端健康信息。

    Args:
        session_id (str): 共享 Ayaya 会话标识。
        chat_settings_dao (ChatSettingsDao): 当前环境中的会话设置 DAO。

    Returns:
        Result: 以 Result 包装的健康状态和当前模型名称。
    """
    settings = chat_settings_dao.get_chat_settings(session_id)
    return Result(data={"status": "ok", "model": settings.model_name}, msg="success", code=200)
